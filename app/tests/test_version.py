"""PlainBoard knows which commit it is and says when a newer one exists."""

import pytest
from plainboard_protocol import UpstreamState

from plainboard.services import version

SHA = "d967155c6fe7d97dd72adf361effbfd8c88814b4"
OTHER = "0123456789abcdef0123456789abcdef01234567"


def test_the_build_reads_its_commit_from_the_checkout(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text(SHA + "\n")
    assert version.read_commit(git) == SHA

    # After `git gc` the branch is in packed-refs only.
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text(f"# pack-refs with: peeled fully-peeled sorted\n{OTHER} refs/heads/chore\n{SHA} refs/heads/main\n")
    assert version.read_commit(git) == SHA

    (git / "HEAD").write_text(OTHER + "\n")  # a detached checkout
    assert version.read_commit(git) == OTHER

    # No git data in the build context, a worktree, something broken: no commit, and no error.
    assert version.read_commit(tmp_path / "missing") == ""
    (git / "HEAD").write_text("ref: refs/heads/gone\n")
    assert version.read_commit(git) == ""
    (git / "HEAD").write_text("gitdir: /somewhere/else\n")
    assert version.read_commit(git) == ""

    (tmp_path / "commit").write_text(SHA + "\n")
    assert version.read_commit_file(tmp_path / "commit") == SHA
    (tmp_path / "commit").write_text("\n")  # what the image holds when the build could not tell
    assert version.read_commit_file(tmp_path / "commit") == "" and version.read_commit_file(tmp_path / "none") == ""


@pytest.fixture
def built_from(monkeypatch):
    version.forget()
    monkeypatch.setattr(version, "running_commit", lambda: SHA)
    monkeypatch.setattr(version, "running_version", lambda: "")
    yield
    version.forget()


def test_a_newer_version_shows_up_in_the_meta_data(client, built_from):
    fake = client.app_ref.state.runner
    tick = client.app_ref.state.updates.tick
    assert client.get("/api/meta").json()["version"] == {"commit": SHA[:10], "name": f"edge-{SHA[:7]}", "release": False, "behind": None, "latest": "", "url": ""}  # not asked yet

    fake.upstream_state = UpstreamState(behind=4, url="https://github.com/hthoene/plainboard/compare/a...main")
    client.portal.call(tick)
    assert fake.upstream_calls == [("hthoene/plainboard", "main", SHA)]  # nothing but the commit goes out
    assert client.get("/api/meta").json()["version"] == {"commit": SHA[:10], "name": f"edge-{SHA[:7]}", "release": False, "behind": 4, "latest": "", "url": "https://github.com/hthoene/plainboard/compare/a...main"}

    # Offline or rate limited: what was known stays, and nobody sees an error.
    fake.upstream_error = "GitHub is not reachable"
    client.portal.call(tick)
    assert client.get("/api/meta").json()["version"]["behind"] == 4

    # GitHub does not know this commit (a local build, a fork): no notice.
    fake.upstream_error, fake.upstream_state = "", None
    client.portal.call(tick)
    assert client.get("/api/meta").json()["version"]["behind"] is None


def test_a_release_is_compared_with_newer_releases(client, built_from, monkeypatch):
    monkeypatch.setattr(version, "running_version", lambda: "1.2.0")
    fake = client.app_ref.state.runner
    seen = []
    real = fake.upstream

    async def upstream(credentials, repository, branch, commit, version=""):
        seen.append(version)
        return await real(credentials, repository, branch, commit, version)

    monkeypatch.setattr(fake, "upstream", upstream)
    fake.upstream_state = UpstreamState(behind=2, url="https://github.com/hthoene/plainboard/compare/v1.2.0...v1.3.0", latest="1.3.0")
    client.portal.call(client.app_ref.state.updates.tick)
    assert seen == ["1.2.0"]
    shown = client.get("/api/meta").json()["version"]
    assert (shown["name"], shown["release"], shown["behind"], shown["latest"]) == ("1.2.0", True, 2, "1.3.0")


def test_the_version_file_names_a_release(tmp_path, monkeypatch):
    from plainboard.config import get_config

    for written, expected in (("v1.4.2\n", "1.4.2"), ("\n", ""), ("nonsense", "")):
        (tmp_path / "version").write_text(written)
        monkeypatch.setattr(get_config(), "version_file", tmp_path / "version")
        monkeypatch.setattr(version, "_version", None)
        assert version.running_version() == expected


def test_the_check_can_be_turned_off(client, built_from):
    fake = client.app_ref.state.runner
    fake.upstream_state = UpstreamState(behind=2)
    client.portal.call(client.app_ref.state.updates.tick)
    assert client.get("/api/meta").json()["version"]["behind"] == 2

    assert client.put("/api/settings", json={"values": {"update_check": False}}).status_code == 200
    assert client.get("/api/meta").json()["version"] is None
    calls = len(fake.upstream_calls)
    client.portal.call(client.app_ref.state.updates.tick)
    assert len(fake.upstream_calls) == calls  # nothing is asked any more

    # Turned on again: asked at once, not hours later.
    assert client.put("/api/settings", json={"values": {"update_check": True}}).status_code == 200
    assert len(fake.upstream_calls) == calls + 1 and client.get("/api/meta").json()["version"]["behind"] == 2


def test_a_build_that_does_not_know_its_commit_asks_nothing(client, monkeypatch):
    monkeypatch.setattr(version, "running_commit", lambda: "")
    fake = client.app_ref.state.runner
    client.portal.call(client.app_ref.state.updates.tick)
    assert fake.upstream_calls == [] and client.get("/api/meta").json()["version"] is None


def test_the_statistics_mark_the_days_plainboard_was_updated(client, project, monkeypatch):
    from plainboard.db import sessionmaker
    from plainboard.models import utcnow

    async def started(commit: str) -> None:
        monkeypatch.setattr(version, "running_commit", lambda: commit)
        async with sessionmaker()() as s:
            await version.note_start(s, utcnow())

    for commit in (SHA, SHA, OTHER, ""):  # a restart with the same version is no update; a build that does not know itself is none either
        client.portal.call(started, commit)
    stats = client.get(f"/api/projects/{project['id']}/stats?days=7").json()
    assert [v["commit"] for v in stats["versions"]][-2:] == [SHA[:10], OTHER[:10]] and stats["days"][-1]["updates"] == len(stats["versions"])
