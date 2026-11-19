"""What the runner removes when room is short (cleanup.py), and what it never touches."""

import os
import subprocess
import sys
import time

import pytest
from plainboard_protocol import CleanupFolder, CleanupSpec

from plainboard_runner import cleanup, config, jobs

DAY = 86400


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True)


def clone(tmp_path, folder, name="repo", ignore="target/\nnode_modules/\n__pycache__/\ndist/\n"):
    """A clone of a bare origin, everything pushed; build outputs in it."""
    origin = tmp_path / "origins" / f"{folder.name}-{name}.git"
    origin.mkdir(parents=True)
    git(origin, "init", "--bare", "-q")
    repo = folder / name
    git(folder, "clone", "-q", str(origin), name)
    (repo / ".gitignore").write_text(ignore)
    (repo / "src.rs").write_text("fn main() {}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "start")
    git(repo, "push", "-q", "origin", "HEAD")
    for out in ("target/debug", "node_modules/left-pad", "pkg/__pycache__"):
        (repo / out).mkdir(parents=True)
        (repo / out / "blob").write_bytes(os.urandom(20_000))
    return repo


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root, home = tmp_path / "ws", tmp_path / "home"
    (root / "p").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACES_ROOT", root)
    monkeypatch.setattr(jobs, "JOBS_ROOT", home / ".plainboard" / "jobs")
    monkeypatch.setenv("HOME", str(home))
    # du sees new files only seconds later on some file systems: count what was written
    monkeypatch.setattr(cleanup, "_size", lambda paths: {p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in paths if p.exists()})
    return root


def agent(ws, name):
    folder = ws / "p" / name
    folder.mkdir()
    return folder


def test_build_outputs_are_what_git_ignores_and_what_tools_marked(ws, tmp_path):
    folder = agent(ws, "dev")
    repo = clone(tmp_path, folder)
    (repo / "build").mkdir()  # not ignored: may be the agent's own work
    (repo / "build" / "notes").write_text("mine")
    (repo / "dist").mkdir()
    (repo / "dist" / "keep.js").write_text("tracked")
    git(repo, "add", "-f", "dist/keep.js")
    git(repo, "commit", "-qm", "dist is tracked here")
    venv = folder / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    (folder / "target").mkdir()  # outside a clone, and cargo did not mark it

    soft = {p.relative_to(folder).as_posix() for p in cleanup.build_outputs(folder, "soft")}
    hard = {p.relative_to(folder).as_posix() for p in cleanup.build_outputs(folder, "hard")}
    assert soft == {"repo/target", "repo/pkg/__pycache__"}
    assert hard == soft | {"repo/node_modules", "venv"}


def test_idle_folders_go_longest_idle_first_until_enough_is_free(ws, tmp_path):
    now = time.time()
    old, recent, fresh = agent(ws, "old"), agent(ws, "recent"), agent(ws, "fresh")
    for folder in (old, recent, fresh):
        clone(tmp_path, folder)
    spec = CleanupSpec(
        level="soft",
        need=1,
        folders=[CleanupFolder(path=str(recent), idle_since=now - 2 * DAY), CleanupFolder(path=str(old), idle_since=now - 9 * DAY), CleanupFolder(path=str(fresh), idle_since=now - 600)],
    )
    result = cleanup.run(spec, now)
    assert not (old / "repo" / "target").exists() and (old / "repo" / "node_modules").exists()  # soft: dependencies stay
    assert (recent / "repo" / "target").exists()  # enough was free already
    assert result.freed > 0 and {i.what for i in result.items} == {"build"}

    result = cleanup.run(spec.model_copy(update={"need": 10**12}), now)
    assert not (recent / "repo" / "target").exists()
    assert (fresh / "repo" / "target").exists()  # its agent stopped ten minutes ago: it may go on soon
    assert (old / "repo" / "src.rs").exists() and (recent / "repo" / "src.rs").exists()


def test_a_folder_something_works_in_is_left_alone(ws, tmp_path):
    folder = agent(ws, "busy")
    clone(tmp_path, folder)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=folder / "repo")
    try:
        assert str(folder) in cleanup.busy_folders()
        result = cleanup.run(CleanupSpec(level="hard", need=10**12, folders=[CleanupFolder(path=str(folder), idle_since=0)]))
    finally:
        proc.kill()
    assert (folder / "repo" / "target").exists() and result.skipped == [f"{folder}: in use"]


def test_a_removed_agents_folder_goes_only_without_unpushed_work(ws, tmp_path):
    clean, ahead, dirty, other = agent(ws, "clean"), agent(ws, "ahead"), agent(ws, "dirty"), agent(ws, "other")
    clone(tmp_path, clean)
    repo = clone(tmp_path, ahead)
    (repo / "more.rs").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "not pushed")
    (clone(tmp_path, dirty) / "src.rs").write_text("changed")
    clone(tmp_path, other)
    (other / "report.md").write_text("an agent's own file")

    folders = [CleanupFolder(path=str(f), orphan=True) for f in (clean, ahead, dirty, other)]
    result = cleanup.run(CleanupSpec(level="soft", need=10**12, folders=folders))
    assert not clean.exists()
    for kept in (ahead, dirty, other):
        assert (kept / "repo" / "src.rs").exists() and not (kept / "repo" / "target").exists()  # only its build output went
    assert sorted(result.skipped) == sorted(
        [f"{ahead}: kept, repo has unpushed commits", f"{dirty}: kept, repo has changes", f"{other}: kept, report.md is not in a clone"]
    )


def test_nothing_outside_an_agents_folder_is_touched(ws, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    clone(tmp_path, outside)
    link = ws / "p" / "link"
    link.symlink_to(outside)
    result = cleanup.run(CleanupSpec(level="hard", need=10**12, folders=[CleanupFolder(path=str(p), orphan=True) for p in (outside, link, ws / "p")]))
    assert (outside / "repo" / "target").exists() and len(result.skipped) == 3


def test_caches_lose_what_was_not_used_for_a_month_and_old_job_logs_go(ws, tmp_path):
    home = tmp_path / "home"
    cache = home / ".cargo" / "registry" / "cache" / "index"
    cache.mkdir(parents=True)
    (cache / "old.crate").write_bytes(os.urandom(10_000))
    (cache / "new.crate").write_bytes(os.urandom(10_000))
    month_ago = time.time() - 40 * DAY
    os.utime(cache / "old.crate", (month_ago, month_ago))
    jobs.JOBS_ROOT.mkdir(parents=True)
    (jobs.JOBS_ROOT / "j1.log").write_text("old output")
    os.utime(jobs.JOBS_ROOT / "j1.log", (month_ago, month_ago))

    result = cleanup.run(CleanupSpec(level="soft", need=0))
    assert not (cache / "old.crate").exists() and (cache / "new.crate").exists()
    assert not (jobs.JOBS_ROOT / "j1.log").exists()
    assert {i.what for i in result.items} == {"cache", "logs"}


def test_candidates_say_how_safe_each_is_and_delete_keeps_to_it(ws, tmp_path, monkeypatch):
    from plainboard_protocol import CandidatesSpec, DeleteSpec

    home = tmp_path / "home"
    idle_agent, busy_agent, gone = agent(ws, "idle"), agent(ws, "busy"), agent(ws, "gone")
    clone(tmp_path, idle_agent)
    clone(tmp_path, busy_agent)
    repo = clone(tmp_path, gone)
    (repo / "more.rs").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "not pushed")
    jobs_dir = home / "fw-jobs"
    jobs_dir.mkdir()
    clone(tmp_path, jobs_dir, "job-1")  # only a clone without own work: safe
    (home / "scratch").mkdir()
    (home / "scratch" / "notes.txt").write_text("someone's")
    work = home / "work"
    work.mkdir()
    (clone(tmp_path, work, "x") / "src.rs").write_text("changed")
    (home / "tools" / "bin").mkdir(parents=True)
    (home / ".profile").write_text('export PATH="$HOME/tools/bin:$PATH"\n')
    (home / ".cache" / "sccache").mkdir(parents=True)
    (home / ".cache" / "ms-playwright").mkdir()
    (home / ".ssh").mkdir()
    monkeypatch.setattr(cleanup, "_sizes", {})

    folders = [CleanupFolder(path=str(idle_agent)), CleanupFolder(path=str(busy_agent), busy=True), CleanupFolder(path=str(gone), orphan=True)]
    found = {c.path: c for c in cleanup.candidates(CandidatesSpec(folders=folders))}
    safety = {os.path.relpath(p, tmp_path): (c.what, c.safety, c.why) for p, c in found.items()}
    assert safety["ws/p/idle/repo/target"] == ("build", "safe", "")
    assert safety["ws/p/idle/repo/node_modules"] == ("build", "safe", "")
    assert safety["ws/p/busy/repo/target"] == ("build", "busy", "")
    assert safety["ws/p/gone"] == ("folder", "keep", "repo has unpushed commits")
    assert safety["ws/p/gone/repo/target"] == ("build", "safe", "")  # its build output may go
    assert safety["home/fw-jobs"] == ("home", "safe", "")
    assert found[str(jobs_dir)].remotes and found[str(jobs_dir)].bytes > 0
    assert safety["home/scratch"] == ("home", "unsure", "scratch/notes.txt is not in a clone")
    assert safety["home/work"] == ("home", "keep", "x has changes")
    assert safety["home/tools"] == ("home", "keep", "holds tools on PATH")
    assert safety["home/.cache/sccache"] == ("cache", "safe", "")
    assert safety["home/.cache/ms-playwright"][1] == "unsure"
    assert not any(p.startswith("home/.ssh") or p.startswith("home/.profile") for p in safety)

    busy_target, scratch = busy_agent / "repo" / "target", home / "scratch"
    asked = [str(p) for p in (idle_agent / "repo" / "target", busy_target, scratch, work, gone, home / ".ssh", home / "tools")]
    result = cleanup.delete(DeleteSpec(paths=asked, folders=folders))
    assert [i.path for i in result.items] == [str(idle_agent / "repo" / "target")]
    assert busy_target.exists() and scratch.exists() and work.exists() and gone.exists() and (home / ".ssh").exists()
    assert f"{busy_target}: busy; force=true deletes it anyway" in result.skipped and f"{work}: kept, x has changes" in result.skipped
    assert f"{home / '.ssh'}: not a cleanup candidate" in result.skipped

    result = cleanup.delete(DeleteSpec(paths=asked, folders=folders, force=True))
    assert not busy_target.exists() and not scratch.exists()
    assert work.exists() and gone.exists() and (home / "tools").exists() and (work / "x" / "src.rs").exists()  # never with unpushed work or tools
