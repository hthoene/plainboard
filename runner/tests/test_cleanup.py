"""What the runner removes when room is short (cleanup.py), and what it never touches."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from plainboard_protocol import CleanupFolder, CleanupSpec, UnpushedSpec

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
    monkeypatch.setattr(cleanup, "_size", lambda paths: {p: _bytes(p) for p in paths if p.exists()})
    monkeypatch.setattr(cleanup, "_file_bytes", lambda st: st.st_size)
    monkeypatch.setattr(cleanup, "_survey", lambda paths: {q: (_bytes(q), q.stat().st_mtime) for p in paths if p.exists() for q in [p, *(p.iterdir() if p.is_dir() else [])]})
    return root


def _bytes(p):
    return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


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
    assert f"{home / '.ssh'}: not a cleanup candidate (list_cleanup shows what can go)" in result.skipped

    result = cleanup.delete(DeleteSpec(paths=asked, folders=folders, force=True))
    assert not busy_target.exists() and not scratch.exists()
    assert work.exists() and gone.exists() and (home / "tools").exists() and (work / "x" / "src.rs").exists()  # never with unpushed work or tools


def test_candidates_say_which_agent_folders_name_them_and_what_they_hold(ws, tmp_path, monkeypatch):
    from plainboard_protocol import CandidatesSpec

    home = tmp_path / "home"
    dev, other = agent(ws, "dev"), agent(ws, "other")
    repo = clone(tmp_path, dev)
    (repo / "check.sh").write_text("CARGO_TARGET_DIR=$HOME/.cache/pp-check cargo check\n")  # untracked script counts too
    (clone(tmp_path, other) / "notes.md").write_text("see ~/.cache/pp-check-old\n")  # a longer name is another folder
    for name in ("pp-check", "pp-check-old", "sccache"):
        (home / ".cache" / name / "set1").mkdir(parents=True)
    (home / ".cache" / "pp-check" / "set1" / "blob").write_bytes(os.urandom(5000))
    monkeypatch.setattr(cleanup, "_sizes", {})
    monkeypatch.setattr(cleanup, "_mentions", {})

    folders = [CleanupFolder(path=str(dev)), CleanupFolder(path=str(other))]
    found = {os.path.relpath(c.path, home): c for c in cleanup.candidates(CandidatesSpec(folders=folders)) if c.path.startswith(str(home))}
    assert found[".cache/pp-check"].mentioned == [str(dev)] and found[".cache/pp-check-old"].mentioned == [str(other)]
    assert found[".cache/pp-check"].entries == {"set1": 5000} and found[".cache/pp-check"].modified > 0
    assert found[".cache/sccache"].tool == "sccache" and not found[".cache/sccache"].mentioned


def test_big_shared_tool_caches_lose_their_oldest_files_down_to_half(ws, tmp_path):
    from plainboard_protocol import TrimSpec

    home = tmp_path / "home"
    rust, web = agent(ws, "rust"), agent(ws, "web")
    (clone(tmp_path, rust) / "Cargo.toml").write_text("[package]\n")
    clone(tmp_path, web)
    sccache, pip = home / ".cache" / "sccache", home / ".cache" / "pip"
    for n in range(10):
        (sccache / str(n)).mkdir(parents=True)
        (sccache / str(n) / "obj").write_bytes(os.urandom(10_000))
        at = time.time() - (10 - n) * DAY  # 0 is the oldest
        os.utime(sccache / str(n) / "obj", (at, at))
    pip.mkdir(parents=True)
    (pip / "wheel").write_bytes(os.urandom(10_000))  # small: stays

    folders = [CleanupFolder(path=str(rust)), CleanupFolder(path=str(web))]
    result = cleanup.trim(TrimSpec(need=10**9, large=50_000, keep=20_000, folders=folders))
    left = sorted(int(p.parent.name) for p in sccache.rglob("obj"))
    assert left == [5, 6, 7, 8, 9] and not (sccache / "0").exists()  # the oldest half went, never more
    assert (pip / "wheel").exists()
    (item,) = result.items
    assert item.path == str(sccache) and item.users == [str(rust)]

    result = cleanup.trim(TrimSpec(need=15_000, large=10_000, keep=10_000, folders=folders))
    assert sorted(int(p.parent.name) for p in sccache.rglob("obj")) == [7, 8, 9]  # only what is needed


def test_before_a_move_every_clone_says_what_of_its_work_is_on_no_remote(ws, tmp_path):
    clean = clone(tmp_path, agent(ws, "dev"))
    folder = agent(ws, "qa")
    busy = clone(tmp_path, folder, "api")
    (busy / "src.rs").write_text("changed\n")
    (busy / "new.txt").write_text("new\n")
    git(busy, "stash", "push", "-qu", "--", "new.txt")
    git(busy, "checkout", "-qb", "feat/x")
    (busy / "x.rs").write_text("x\n")
    git(busy, "add", "x.rs")
    git(busy, "commit", "-qm", "x")
    git(busy, "branch", "spare", "main")  # no upstream, nowhere on the remote
    git(busy, "branch", "pushed", "main")
    git(busy, "push", "-q", "origin", "pushed")  # no upstream either, but on the remote
    (busy / "src.rs").write_text("changed again\n")

    found = cleanup.unpushed_work(UnpushedSpec(folders=[str(clean.parent), str(folder), str(tmp_path / "outside"), str(ws)]))
    assert [(w.folder, w.repo) for w in found] == [(str(folder), "api")]
    assert found[0].problems == ["1 uncommitted change", "1 stash", "commits on no remote branch: feat/x (1)", "branches without upstream: spare"]


def test_extra_work_folders_of_an_agent_are_offered_and_go_only_without_own_work(ws, tmp_path):
    from plainboard_protocol import CandidatesSpec, DeleteSpec

    folder = agent(ws, "dev")
    repo = clone(tmp_path, folder)
    git(repo, "worktree", "add", "-q", "-b", "feature", "../ws-b")  # a branch with unpushed commits: the main clone keeps it
    (folder / "ws-b" / "f.rs").write_text("x")
    git(folder / "ws-b", "add", ".")
    git(folder / "ws-b", "commit", "-qm", "on a branch")
    (folder / "ws-b" / "target").mkdir()
    git(repo, "worktree", "add", "-q", "-b", "dirty", "../ws-c")
    (folder / "ws-c" / "src.rs").write_text("changed")
    git(repo, "worktree", "add", "-q", "--detach", "../ws-d")
    (folder / "ws-d" / "g.rs").write_text("y")
    git(folder / "ws-d", "add", ".")
    git(folder / "ws-d", "commit", "-qm", "on no branch")
    (folder / "notes").mkdir()
    (folder / "notes" / "todo.txt").write_text("mine")
    (folder / ".claude").mkdir()

    folders = [CleanupFolder(path=str(folder), clones=["repo"])]
    found = {os.path.relpath(c.path, folder): (c.what, c.safety, c.why) for c in cleanup.candidates(CandidatesSpec(folders=folders), sizes=False)}
    assert found["ws-b"] == ("workdir", "safe", "")
    assert found["ws-c"] == ("workdir", "keep", "ws-c has changes")
    assert found["ws-d"] == ("workdir", "keep", "ws-d has commits on no branch")
    assert found["notes"] == ("workdir", "unsure", "notes/todo.txt is not in a clone")
    assert "repo" not in found and ".claude" not in found and found["repo/target"][0] == "build"
    assert not any(c.what == "workdir" for c in cleanup.candidates(CandidatesSpec(folders=[CleanupFolder(path=str(folder))]), sizes=False))  # an app that sends no clones
    busy = cleanup.candidates(CandidatesSpec(folders=[CleanupFolder(path=str(folder), clones=["repo"], busy=True)]), sizes=False)
    assert next(c for c in busy if c.path == str(folder / "ws-b")).safety == "busy"

    asked = [str(folder / n) for n in ("ws-b", "ws-c", "ws-d", "gone")]
    result = cleanup.delete(DeleteSpec(paths=asked, folders=folders, force=True))
    assert [(i.path, i.what) for i in result.items] == [(str(folder / "ws-b"), "workdir")]
    assert f"{folder / 'ws-c'}: kept, ws-c has changes" in result.skipped and f"{folder / 'gone'}: gone already" in result.skipped
    assert not (folder / "ws-b").exists() and (folder / "ws-c" / "src.rs").read_text() == "changed"
    listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert "ws-b" not in listed and "ws-c" in listed  # the main clone forgot the deleted worktree, and keeps its branch
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", "feature"], cwd=repo, capture_output=True).returncode == 0


def test_temp_leftovers_go_only_when_nothing_in_them_changed_for_days_and_nobody_works_there(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    old = time.time() - 5 * DAY
    for name in ("old-dir", "busy-dir", "fresh-dir", "plainboard-signins", ".X11-unix"):
        (tmp / name).mkdir(parents=True)
        (tmp / name / "f").write_text("x")
    (tmp / "old-file.log").write_text("x")
    (tmp / "fresh-dir" / "new").write_text("x")
    for path in [*tmp.rglob("*"), tmp]:
        if path.name != "new":
            os.utime(path, (old, old))
    monkeypatch.setattr(cleanup, "_cwds", lambda: {str(tmp / "busy-dir")})
    assert [p.name for p in cleanup.tmp_leftovers(time.time() - 3 * DAY, tmp)] == ["old-dir", "old-file.log"]


def test_sizes_kept_but_measured_again_where_a_cleanup_touched(monkeypatch):
    measured = []
    monkeypatch.setattr(cleanup, "_survey", lambda paths: measured.extend(paths) or {p: (1, 0.0) for p in paths})
    cleanup.forget_sizes()
    a, b, c = Path("/w/a"), Path("/w/a/target"), Path("/w/b")
    cleanup._measure([a, b, c])
    cleanup._measure([a, b, c])
    assert measured == [a, c, b]  # kept (a folder and one in it in separate walks)
    cleanup.forget_sizes(["/w/a/target/debug"])
    cleanup._measure([a, b, c])
    assert measured == [a, c, b, a, b]  # what holds the deleted path, not the rest
    assert cleanup._measured[str(c)] <= cleanup._measured[str(a)]


def test_folders_in_a_home_folder_are_offered_one_by_one_with_their_build_outputs(ws, tmp_path, monkeypatch):
    from plainboard_protocol import SCRATCH, CandidatesSpec, DeleteSpec

    home = tmp_path / "home"
    scratch = home / "scratch"
    (scratch / "k483").mkdir(parents=True)
    (scratch / "k483" / "notes.md").write_text("findings")
    clone(tmp_path, scratch / "k483", "rel")
    for tagged in (scratch / "hil" / "target-pf7", scratch / "a88-fp-target"):  # cargo's target, whatever it is called
        tagged.mkdir(parents=True)
        (tagged / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
        (tagged / "blob").write_bytes(os.urandom(5000))
    (scratch / "hil" / "run.sh").write_text("cargo build\n")
    dev = agent(ws, "dev")
    clone(tmp_path, dev)
    (dev / SCRATCH / "target-x").mkdir(parents=True)
    (dev / SCRATCH / "target-x" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    monkeypatch.setattr(cleanup, "_sizes", {})
    monkeypatch.setattr(cleanup, "_home_agents", lambda home: [(str(scratch / "k483" / "rel"), 7)])

    folders = [CleanupFolder(path=str(dev), clones=["repo"])]
    found = {os.path.relpath(c.path, tmp_path): c for c in cleanup.candidates(CandidatesSpec(folders=folders))}
    safety = {p: (c.what, c.safety, c.why) for p, c in found.items()}
    assert safety["home/scratch"][0] == "home" and not found["home/scratch"].parent
    assert safety["home/scratch/k483"] == ("home", "unsure", "k483/notes.md is not in a clone")
    assert found["home/scratch/k483"].parent == str(scratch) and found["home/scratch/k483"].agents == [7]
    assert safety["home/scratch/k483/rel/target"] == ("build", "safe", "") and found["home/scratch/k483/rel/target"].parent == str(scratch)
    assert safety["home/scratch/hil/target-pf7"] == ("build", "safe", "")
    assert safety["home/scratch/a88-fp-target"] == ("home", "safe", "") and found["home/scratch/a88-fp-target"].bytes > 0
    assert sum(1 for p in found if p == "home/scratch/a88-fp-target") == 1  # not as a build output too
    assert safety[f"ws/p/dev/{SCRATCH}"][0] == "workdir" and safety[f"ws/p/dev/{SCRATCH}/target-x"] == ("build", "safe", "")

    result = cleanup.delete(DeleteSpec(paths=[str(scratch / "hil" / "target-pf7"), str(scratch / "k483")], folders=folders))
    assert [i.path for i in result.items] == [str(scratch / "hil" / "target-pf7")]
    assert (scratch / "hil" / "run.sh").exists() and (scratch / "k483" / "notes.md").exists()


def test_nested_paths_are_measured_in_separate_walks():
    a, b, c, d = map(cleanup.Path, ("/h/s", "/h/s/k", "/h/s/k/t", "/h/w"))
    assert cleanup._layers([c, a, b, d]) == [[a, d], [b], [c]]
