import asyncio
import io
import json
import zipfile

from plainboard_protocol import SkillSpec

from plainboard_runner import skills


def _zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return out.getvalue()


def test_skills_are_laid_down_only_when_changed_and_removed_when_gone(tmp_path):
    folder = tmp_path / ".claude" / "skills"
    archives = {1: _zip({"SKILL.md": b"one", "scripts/a.py": b"x"}), 2: _zip({"SKILL.md": b"two"})}
    fetched: list[int] = []

    async def download(skill_id: int) -> bytes:
        fetched.append(skill_id)
        return archives[skill_id]

    def sync(*specs: SkillSpec) -> list[str]:
        return asyncio.run(skills.sync(folder, list(specs), download))

    assert sync() == [] and not (tmp_path / ".claude").exists()  # no skills: .claude is not touched
    (folder / "own").mkdir(parents=True)  # the user's own skill
    (folder / "own" / "SKILL.md").write_text("mine")
    (tmp_path / ".claude" / "settings.json").write_text("{}")

    one, two = SkillSpec(id=1, name="one", hash="h1"), SkillSpec(id=2, name="two", hash="h2")
    assert sync(one, two) == []
    assert (folder / "one" / "scripts" / "a.py").read_bytes() == b"x" and (folder / "two" / "SKILL.md").read_bytes() == b"two"
    assert json.loads((folder / skills.MANIFEST).read_text()) == {"one": {"id": 1, "hash": "h1"}, "two": {"id": 2, "hash": "h2"}}
    assert fetched == [1, 2]

    assert sync(one, two) == [] and fetched == [1, 2]  # nothing changed: nothing fetched
    archives[1] = _zip({"SKILL.md": b"one again"})
    assert sync(SkillSpec(id=1, name="one", hash="h1b")) == []  # one changed, two is no longer the agent's
    assert fetched == [1, 2, 1]
    assert (folder / "one" / "SKILL.md").read_bytes() == b"one again" and not (folder / "one" / "scripts").exists()
    assert not (folder / "two").exists()
    assert (folder / "own" / "SKILL.md").read_text() == "mine" and (tmp_path / ".claude" / "settings.json").exists()

    # A folder PlainBoard did not lay down stays; a skill whose zip is bad leaves the turn going.
    problems = sync(SkillSpec(id=1, name="one", hash="h1b"), SkillSpec(id=3, name="own", hash="h3"))
    assert "not PlainBoard's" in problems[0] and (folder / "own" / "SKILL.md").read_text() == "mine"
    archives[4] = _zip({"../evil": b"x"})
    assert "could not be laid down" in sync(SkillSpec(id=4, name="evil", hash="h4"))[0]
    assert not (tmp_path / ".claude" / "evil").exists() and not (folder / "evil").exists()
    assert sync() == [] and not (folder / "one").exists() and (folder / "own").exists() and not (folder / skills.MANIFEST).exists()
    assert sorted(p.name for p in folder.iterdir()) == ["own"]  # no temporary folders left


def test_a_skill_is_fetched_from_a_folder_of_a_git_repository(tmp_path):
    import subprocess

    from plainboard_protocol import Credentials, SkillFetch

    repo = tmp_path / "repo"
    (repo / "skills" / "pdf").mkdir(parents=True)
    (repo / "skills" / "pdf" / "SKILL.md").write_text("---\nname: pdf\ndescription: x\n---\n")
    (repo / "README.md").write_text("top")
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    data = asyncio.run(skills.fetch(SkillFetch(url=repo.as_uri(), ref="main", path="skills/pdf", credentials=Credentials())))
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["SKILL.md"]
    try:
        asyncio.run(skills.fetch(SkillFetch(url=repo.as_uri(), path="../..", credentials=Credentials())))
    except skills.WorkspaceError as exc:
        assert "Not a folder" in str(exc)
    else:
        raise AssertionError("a path out of the repository was taken")
