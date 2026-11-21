"""Trains with real git: several branches merged into one, checked once, pushed to the base branch."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from plainboard_protocol import Credentials, RepoSpec, TrainCheckSpec, TrainLandSpec, TrainSpec

from plainboard_runner import github, jobs, main, trains
from plainboard_runner.workspace import WorkspaceError

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(work: Path, name: str, content: str, message: str) -> None:
    (work / name).write_text(content)
    git(work, "add", name)
    git(work, "commit", "-q", "-m", message)


@pytest.fixture
def remote(tmp_path, monkeypatch) -> Path:
    """A remote with main and four branches: a and b are independent, c conflicts with a, d builds on b."""
    monkeypatch.setattr(trains, "trains_root", lambda: tmp_path / "trains")
    monkeypatch.setattr(jobs, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(jobs, "_jobs", {})
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "main")
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", str(bare))
    commit(work, "a.txt", "one\n", "start")
    git(work, "push", "-q", "origin", "main")
    for branch, name, content, start in (("feat-a", "a.txt", "from a\n", "main"), ("feat-b", "b.txt", "b\n", "main"), ("feat-c", "a.txt", "from c\n", "main"), ("feat-d", "d.txt", "d\n", "feat-b")):
        git(work, "checkout", "-q", "-b", branch, start)
        commit(work, name, content, f"work on {branch}")
        git(work, "push", "-q", "origin", branch)
    git(work, "checkout", "-q", "main")
    return bare


def spec(remote: Path, members: list[str], **extra) -> TrainSpec:
    return TrainSpec(credentials=Credentials(), key="p1-r1", remote=RepoSpec(name="api", url=str(remote)), branch="plainboard/train-1", members=members, **extra)


def heads(remote: Path) -> dict[str, str]:
    lines = git(remote, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines()
    return dict(line.split(" ") for line in lines)


async def test_a_train_holds_every_branch_that_merges_and_leaves_out_the_rest(remote):
    result = await trains.assemble(spec(remote, ["feat-a", "feat-b", "feat-c", "gone"]))
    assert result.base == "main"
    assert set(result.merged) == {"feat-a", "feat-b"} and result.merged["feat-a"] == heads(remote)["feat-a"]
    # A conflict keeps one branch back, not the train. c merges onto main but not with a, which is before it on this
    # train: nothing its author could resolve yet, so it is not called a conflict.
    assert result.waits == ["feat-c"] and not result.conflicts and result.missing == ["gone"]
    assert result.conflict_files == {"feat-c": ["a.txt"]}
    clone = trains.folder("p1-r1")
    assert (clone / "a.txt").read_text() == "from a\n" and (clone / "b.txt").exists()
    assert git(clone, "rev-parse", "HEAD") == result.head_sha
    assert "plainboard/train-1" not in heads(remote)  # checked by a command: nothing is pushed until it has passed


async def test_what_conflicts_with_the_base_branch_goes_to_its_author_and_what_touches_a_file_being_resolved_is_held(remote, tmp_path):
    """Seen in production: a branch came back seven times in an hour. Twice its author could do nothing (the
    conflict was with a branch on the same train), and each time it resolved the lock file, another branch
    that changed the lock file had landed in between."""
    work = tmp_path / "work"
    git(work, "merge", "-q", "--no-ff", "-m", "land a", "feat-a")
    git(work, "push", "-q", "origin", "main")
    # c conflicts with main itself now: that is its author's to resolve, and the file is named.
    result = await trains.assemble(spec(remote, ["feat-b", "feat-c"]))
    assert result.conflicts == ["feat-c"] and not result.waits and set(result.merged) == {"feat-b"}
    assert result.conflict_files == {"feat-c": ["a.txt"]}
    # While c's author resolves a.txt, a branch that changes a.txt is held; one that does not goes.
    git(work, "checkout", "-q", "-b", "feat-e", "main")
    commit(work, "a.txt", "from a\nand e\n", "work on feat-e")
    git(work, "push", "-q", "origin", "feat-e")
    result = await trains.assemble(spec(remote, ["feat-e", "feat-b"], busy_files=["a.txt", "other.txt"]))
    assert result.held == ["feat-e"] and set(result.merged) == {"feat-b"} and not result.conflicts
    result = await trains.assemble(spec(remote, ["feat-e", "feat-b"]))
    assert set(result.merged) == {"feat-e", "feat-b"} and not result.held


async def test_a_branch_built_on_another_rides_along_and_landed_or_unchanged_ones_are_left_out(remote, tmp_path):
    result = await trains.assemble(spec(remote, ["feat-d", "feat-b"]))
    assert set(result.merged) == {"feat-d", "feat-b"} and not result.landed  # b came with d: still part of this train

    work = tmp_path / "work"
    git(work, "merge", "-q", "--no-ff", "-m", "land b", "feat-b")
    git(work, "push", "-q", "origin", "main")
    result = await trains.assemble(spec(remote, ["feat-b", "feat-a"], skip_unchanged={"feat-a": heads(remote)["feat-a"]}))
    assert result.landed == ["feat-b"] and result.unchanged == ["feat-a"] and not result.merged  # nothing to check


async def test_a_checked_train_is_pushed_to_the_base_branch_and_its_branches_are_removed(remote):
    result = await trains.assemble(spec(remote, ["feat-a", "feat-b"]))
    state = await trains.start_check(TrainCheckSpec(credentials=Credentials(), key="p1-r1", id="ab12", command="test -f b.txt && grep -q 'from a' a.txt && echo checked"))
    for _ in range(200):
        state = jobs.status(["ab12"])["ab12"]
        if not state.running:
            break
        await asyncio.sleep(0.02)
    assert state.exit_code == 0 and "checked" in state.tail  # the command sees all branches together

    land = TrainLandSpec(credentials=Credentials(), key="p1-r1", remote=RepoSpec(name="api", url=str(remote)), base="main", head_sha=result.head_sha, delete_branches={name: result.merged[name] for name in ("feat-a", "feat-b")} | {"feat-c": "0" * 40})
    landed = await trains.land(land)
    assert landed.ok and heads(remote)["main"] == result.head_sha
    assert set(heads(remote)) == {"main", "feat-c", "feat-d"}  # c is not at the commit named: it stays


async def test_a_train_does_not_land_on_a_base_branch_that_moved(remote, tmp_path):
    result = await trains.assemble(spec(remote, ["feat-a"]))
    work = tmp_path / "work"
    commit(work, "other.txt", "x\n", "someone pushed to main")
    git(work, "push", "-q", "origin", "main")
    moved = heads(remote)["main"]
    landed = await trains.land(TrainLandSpec(credentials=Credentials(), key="p1-r1", remote=RepoSpec(name="api", url=str(remote)), base="main", head_sha=result.head_sha))
    assert not landed.ok and heads(remote)["main"] == moved  # never forced: it has to be assembled and checked again
    again = await trains.assemble(spec(remote, ["feat-a"]))
    assert again.merged and (trains.folder("p1-r1") / "other.txt").exists()


async def test_the_head_of_a_branch_on_the_remote(remote):
    access = RepoSpec(name="api", url=str(remote))
    assert await trains.branch_head(Credentials(), access, "feat-a") == heads(remote)["feat-a"]
    assert await trains.branch_head(Credentials(), access, "nope") is None


class FakeGh:
    """gh for a repository that allows no merge commits: its rebase cannot replay the train, its squash
    is done for real in the bare remote."""

    def __init__(self, remote: Path, work: Path):
        self.remote, self.work, self.calls = remote, work, []

    async def __call__(self, args, cwd, env, timeout=600):
        if args[:2] == ["gh", "api"]:
            return "[false,true,true]"
        if args[:3] == ["gh", "pr", "view"]:
            return '{"state": "MERGED"}'
        method = args[-2]
        self.calls.append(method)
        if method == "--rebase":
            raise WorkspaceError("gh pr failed: GraphQL: This branch can't be rebased (mergePullRequest)")
        git(self.work, "fetch", "-q", "origin")
        git(self.work, "checkout", "-q", "-B", "main", "origin/main")
        git(self.work, "merge", "-q", "--squash", "origin/plainboard/train-1")
        git(self.work, "commit", "-q", "-m", "Land the train (squashed)")
        git(self.work, "push", "-q", "origin", "main")
        return ""


def merge_request(remote: Path, result, **extra) -> main.MergeRequest:
    return main.MergeRequest(
        credentials=Credentials(), repo="acme/api", number=7, methods=["merge", "rebase", "squash"], key="p1-r1", remote=RepoSpec(name="api", url=str(remote)),
        delete_branches=dict(result.merged), base="main", head_sha=result.head_sha, branch="plainboard/train-1", **extra,
    )  # fmt: skip


async def test_a_train_with_a_merge_commit_lands_as_the_commit_that_was_checked(remote, tmp_path, monkeypatch):
    work = tmp_path / "work"
    commit(work, "m.txt", "m\n", "on main")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "feat-a")
    git(work, "merge", "-q", "--no-edit", "main", "-m", "Merge origin/main into feat-a")  # what GitHub's rebase cannot replay
    git(work, "push", "-q", "origin", "feat-a")
    fake = FakeGh(remote, work)
    monkeypatch.setattr(github, "run_cmd", fake)
    monkeypatch.setattr(github, "_allowed", {})
    result = await trains.assemble(spec(remote, ["feat-a", "feat-b"], github="acme/api"))
    landed = await main.github_merge(merge_request(remote, result))
    assert landed.ok and landed.detail == "fast-forward" and fake.calls == []  # no merge method: the base branch only moved forward
    assert heads(remote)["main"] == result.head_sha
    assert set(heads(remote)) == {"main", "feat-c", "feat-d"}  # the members and the train's branch are gone


async def test_a_train_on_a_base_that_moved_is_squashed_last_and_what_builds_on_it_is_moved_onto_it(remote, tmp_path, monkeypatch):
    work = tmp_path / "work"
    fake = FakeGh(remote, work)
    monkeypatch.setattr(github, "run_cmd", fake)
    monkeypatch.setattr(github, "_allowed", {})
    result = await trains.assemble(spec(remote, ["feat-a", "feat-b"], github="acme/api"))
    commit(work, "other.txt", "x\n", "someone pushed to main")
    git(work, "push", "-q", "origin", "main")
    before = heads(remote)
    landed = await main.github_merge(merge_request(remote, result, restack=["feat-d", "feat-c", "gone"]))
    # Not a fast-forward any more; no merge commits allowed; the rebase cannot replay it: squashed.
    assert landed.ok and fake.calls == ["--rebase", "--squash"] and landed.detail == "squash; moved onto it: feat-d"
    after = heads(remote)
    assert after["feat-c"] == before["feat-c"]  # does not build on the train
    # d was built on b: only its own commit is on top of the squash now, and it merges without a conflict.
    assert git(remote, "rev-parse", "feat-d^") == after["main"]
    assert git(remote, "show", "feat-d:d.txt") == "d"
    again = await trains.assemble(spec(remote, ["feat-d"]))
    assert set(again.merged) == {"feat-d"} and not again.conflicts
