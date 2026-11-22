"""Trains: several finished branches merged into one, so that one check run covers all of them.

Plain git in a clone of the runner's own, so it works with any git host. The app's watcher decides
what goes on a train and what happens after its checks; nothing here needs an agent."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from plainboard_protocol import CheckResult, Credentials, JobSpec, JobState, RepoSpec, TidyBranch, TidyResult, TidySpec, TrainCheckSpec, TrainLandSpec, TrainResult, TrainSpec

from . import jobs
from .workspace import WorkspaceError, config_args, git_env, run_cmd

_locks: dict[str, asyncio.Lock] = {}


def trains_root() -> Path:
    return Path.home() / ".plainboard" / "trains"


def folder(key: str) -> Path:
    return trains_root() / key


def _lock(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


class Git:
    """git in the train's clone, with the repository's access."""

    def __init__(self, credentials: Credentials, remote: RepoSpec, cwd: Path | None):
        self.env = git_env(credentials)
        self.config = config_args(remote, credentials)
        self.cwd = cwd

    async def __call__(self, *args: str, timeout: float = 600) -> str:
        return await run_cmd(["git", *self.config, *args], cwd=self.cwd, env=self.env, timeout=timeout)

    async def ok(self, *args: str) -> bool:
        try:
            await self(*args)
            return True
        except WorkspaceError:
            return False


async def remote_heads(git: Git, url: str) -> dict[str, str]:
    heads = {}
    for line in (await git("ls-remote", "--heads", url, timeout=120)).splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha.strip()
    return heads


async def default_branch(git: Git, url: str) -> str:
    for line in (await git("ls-remote", "--symref", url, "HEAD", timeout=120)).splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split("\t")[0].removeprefix("ref: refs/heads/")
    raise WorkspaceError("Could not find the repository's default branch")


async def branch_head(credentials: Credentials, remote: RepoSpec, branch: str) -> str | None:
    """The commit a branch points to on the remote; None if it is not there."""
    return (await remote_heads(Git(credentials, remote, None), remote.url)).get(branch)


CONFLICT_FILES_NAMED = 20


def _files(listing: str) -> list[str]:
    return [name for name in listing.split("\0") if name]


async def assemble(spec: TrainSpec) -> TrainResult:
    """Merge the member branches, in order, onto the base branch. A branch that conflicts is left
    out; the others still go. With ``spec.github`` the result is pushed as the train's branch.

    A left-out branch is one of three: it conflicts with the base branch (``conflicts``: its author
    can resolve that), only with a branch before it on this train (``waits``: there is nothing to
    resolve until that one has landed), or it changes a file someone resolves a conflict in right
    now (``held``)."""
    where = folder(spec.key)
    url = spec.remote.url
    async with _lock(spec.key):
        where.mkdir(parents=True, exist_ok=True)
        git = Git(spec.credentials, spec.remote, where)
        if not (where / ".git").exists():
            await git("init", "-q")
            await git("remote", "add", "origin", url)
        else:
            await git("remote", "set-url", "origin", url)
        heads = await remote_heads(git, url)
        base = spec.base or await default_branch(git, url)
        if base not in heads:
            raise WorkspaceError(f"Base branch {base} not found on the remote")
        result = TrainResult(base=base)
        members = []
        for branch in dict.fromkeys(spec.members):
            if branch not in heads:
                result.missing.append(branch)
            elif spec.skip_unchanged.get(branch) == heads[branch]:
                result.unchanged.append(branch)
            else:
                members.append(branch)
        refs = [f"+refs/heads/{name}:refs/remotes/origin/{name}" for name in (base, *members)]
        await git("fetch", "-q", "--no-tags", "origin", *refs)
        await git.ok("merge", "--abort")  # a half-finished merge of an earlier attempt
        await git("checkout", "-q", "-f", "-B", spec.branch, f"refs/remotes/origin/{base}")
        await git("clean", "-q", "-fdx")
        base_sha = await git("rev-parse", "HEAD")
        busy = set(spec.busy_files)
        for branch in members:
            before = await git("rev-parse", "HEAD")
            ref = f"refs/remotes/origin/{branch}"
            if busy and busy & set(_files(await git("diff", "--name-only", "-z", f"{base_sha}...{ref}"))):
                result.held.append(branch)
            elif not await git.ok("merge", "-q", "--no-ff", "--no-edit", "-m", f"Land {branch}", ref):
                unmerged = _files(await git("diff", "--name-only", "-z", "--diff-filter=U"))
                if not await git.ok("merge", "--abort"):
                    await git("reset", "-q", "--hard", before)
                result.conflict_files[branch] = unmerged[:CONFLICT_FILES_NAMED]
                # With the base branch alone it may merge: then a branch before it on this train is in its way.
                alone = before != base_sha and await git.ok("merge-tree", "--write-tree", "--no-messages", base_sha, ref)
                (result.waits if alone else result.conflicts).append(branch)
            elif await git("rev-parse", "HEAD") != before or not await git.ok("merge-base", "--is-ancestor", ref, base_sha):
                result.merged[branch] = heads[branch]  # also a branch that an earlier member already brought along
            else:
                result.landed.append(branch)  # the base branch has it already
        result.head_sha = await git("rev-parse", "HEAD")
        if result.head_sha == base_sha:  # nothing new: nothing to check
            result.landed += list(result.merged)
            result.merged = {}
        elif spec.github:
            await git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{spec.branch}")
        return result


async def start_check(spec: TrainCheckSpec) -> JobState:
    """The repository's check command, in the clone that holds the assembled train. It runs as a job:
    the app polls for its end like for any other."""
    where = folder(spec.key)
    if not (where / ".git").exists():
        raise WorkspaceError("The train has not been assembled on this runner")
    return await jobs.start(JobSpec(id=spec.id, command=spec.command, workspace=str(where), credentials=spec.credentials), cwd=where)


async def land(spec: TrainLandSpec) -> CheckResult:
    """Push the checked train to the base branch. Not forced: if the base branch moved meanwhile the
    push is refused, and the train has to be assembled and checked again."""
    where = folder(spec.key)
    if not (where / ".git").exists():
        return CheckResult(ok=False, detail="The train has not been assembled on this runner")
    async with _lock(spec.key):
        git = Git(spec.credentials, spec.remote, where)
        if not await git.ok("cat-file", "-e", f"{spec.head_sha}^{{commit}}"):
            await git.ok("fetch", "-q", "--no-tags", "origin", spec.head_sha)  # built elsewhere: the push names what is missing
        try:
            await git("push", "-q", "origin", f"{spec.head_sha}:refs/heads/{spec.base}", timeout=300)
        except WorkspaceError as exc:
            return CheckResult(ok=False, detail=str(exc)[-600:])
        await _remove(git, spec.delete_branches)
        return CheckResult(ok=True, detail="pushed")


async def restack(credentials: Credentials, remote: RepoSpec, key: str, base: str, squashed: str, branches: list[str]) -> list[str]:
    """After a train was squashed onto the base branch: move the branches that build on its commits
    onto the squash commit, so they do not conflict with their own history. Only their own commits
    are replayed (those after ``squashed``, the train's head); the push is leased on what was read,
    so commits pushed to a branch meanwhile are not lost. One that does not replay cleanly stays as
    it is. The branches that moved."""
    where = folder(key)
    if not (where / ".git").exists() or not branches:
        return []
    moved = []
    async with _lock(key):
        git = Git(credentials, remote, where)
        await git("fetch", "-q", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
        if not await git.ok("cat-file", "-e", f"{squashed}^{{commit}}") and not await git.ok("fetch", "-q", "--no-tags", "origin", squashed):
            return []
        tip = f"refs/remotes/origin/{base}"
        for branch in dict.fromkeys(branches):
            ref = f"refs/remotes/origin/{branch}"
            if not await git.ok("fetch", "-q", "--no-tags", "origin", f"+refs/heads/{branch}:{ref}"):
                continue  # not on the remote
            head = await git("rev-parse", ref)
            if await git.ok("merge-base", "--is-ancestor", head, squashed):
                continue  # nothing of its own after the train
            try:
                shared = await git("merge-base", head, squashed)
            except WorkspaceError:
                continue  # no common history
            if await git.ok("merge-base", "--is-ancestor", shared, tip):
                continue  # it does not build on the train's commits
            await git.ok("rebase", "--abort")
            await git("checkout", "-q", "-f", "--detach", head)
            if not await git.ok("rebase", "-q", "--onto", tip, squashed):
                await git.ok("rebase", "--abort")
                continue
            if await git.ok("push", "-q", f"--force-with-lease=refs/heads/{branch}:{head}", "origin", f"HEAD:refs/heads/{branch}"):
                moved.append(branch)
        await git.ok("checkout", "-q", "-f", "--detach", tip)
    return moved


async def _remove(git: Git, branches: dict[str, str]) -> None:
    """Remove landed branches from the remote. Only while a branch still points to the commit that
    landed: commits pushed to it since are not lost."""
    for branch, sha in branches.items():
        await git.ok("push", "-q", f"--force-with-lease=refs/heads/{branch}:{sha}", "origin", f":refs/heads/{branch}")


# Branches that stay whatever points at them: a repository keeps these on purpose.
KEPT_NAMES = {"main", "master", "develop", "development", "dev", "trunk", "staging", "production", "stable", "gh-pages"}
KEPT_PREFIXES = ("release", "hotfix", "dependabot/")


# How far back the base branch's history is searched for the point at which a branch had landed.
LANDED_SEARCH = 1000


async def merged_how(git: Git, sha: str, tip: str) -> str:
    """How the content of a commit is on the base branch already, by content and not only by history:
    "ancestor" (its commits are there), "merged" (merging it into the base branch changes nothing) or
    "landed at <commit>" (at that commit of the base branch merging it changed nothing: it got there
    squashed, rebased or together with other branches, and the base branch went on since). Empty: it
    has something of its own, or that is unclear."""
    if await git.ok("merge-base", "--is-ancestor", sha, tip):
        return "ancestor"
    try:
        shared = await git("merge-base", tip, sha)
    except WorkspaceError:
        return ""  # no common history
    listed = await git("log", "--first-parent", f"--max-count={LANDED_SEARCH}", "--format=%H %T", f"{shared}..{tip}")
    for index, line in enumerate(listed.splitlines()):
        commit, _, tree = line.partition(" ")
        try:
            merged = (await git("merge-tree", "--write-tree", "--no-messages", commit, sha)).split()[0]
        except (WorkspaceError, IndexError):
            continue  # it conflicts with that state
        if merged == tree.strip():
            return "merged" if index == 0 else f"landed at {commit[:12]}"
    return ""


async def tidy(spec: TidySpec) -> TidyResult:
    """Remove the branches whose content is all on the base branch already: what merging left behind,
    also squashed, rebased or inside another branch (``merged_how``). Only a name goes, never work: a
    branch stays if anything of it is not proven to be on the base branch, if its last commit is young
    (a branch just started or pushed), if it is the base or the default branch, has a name repositories
    keep on purpose, is named in ``spec.keep``, or, on GitHub, is protected or part of an open pull
    request. Branches with work of their own whose last commit is older than ``spec.stale_days`` are
    named in the result, and stay."""
    where = folder(spec.key)
    url = spec.remote.url
    async with _lock(spec.key):
        where.mkdir(parents=True, exist_ok=True)
        git = Git(spec.credentials, spec.remote, where)
        if not (where / ".git").exists():
            await git("init", "-q")
            await git("remote", "add", "origin", url)
        else:
            await git("remote", "set-url", "origin", url)
        heads = await remote_heads(git, url)
        try:
            default = await default_branch(git, url)
        except WorkspaceError:
            default = ""  # a remote whose HEAD names no branch
        base = spec.base or default
        if base not in heads:
            raise WorkspaceError(f"Base branch {base or '?'} not found on the remote")
        keep = {base, default, *spec.keep, *KEPT_NAMES}  # whatever git says about them
        if spec.github:
            # A branch an open pull request comes from or is based on: the host would close it with the
            # branch. And a protected one. If this cannot be read, nothing is removed.
            listed = await run_cmd(
                ["gh", "api", "--paginate", f"repos/{spec.github}/pulls?state=open&per_page=100", "--jq", ".[] | .base.ref, .head.ref"], cwd=None, env=git.env, timeout=120
            )
            protected = await run_cmd(
                ["gh", "api", "--paginate", f"repos/{spec.github}/branches?protected=true&per_page=100", "--jq", ".[].name"], cwd=None, env=git.env, timeout=120
            )
            keep |= {line.strip() for line in (listed + "\n" + protected).splitlines() if line.strip()}
        candidates = {name: sha for name, sha in sorted(heads.items()) if name not in keep and not name.startswith(KEPT_PREFIXES)}
        refs = [f"+refs/heads/{name}:refs/remotes/origin/{name}" for name in (base, *candidates)]
        await git("fetch", "-q", "--no-tags", "origin", *refs)
        tip = f"refs/remotes/origin/{base}"
        now = time.time()
        young = now - spec.min_age_hours * 3600
        stale = now - spec.stale_days * 86400
        gone: dict[str, TidyBranch] = {}
        result = TidyResult()
        for name, sha in candidates.items():
            when = int(await git("show", "-s", "--format=%ct", sha))
            if when > young or len(gone) >= spec.limit:
                continue
            if how := await merged_how(git, sha, tip):
                gone[name] = TidyBranch(name=name, sha=sha, how=how)
            elif when < stale:
                result.unmerged.append(TidyBranch(name=name, sha=sha, days=int((now - when) // 86400)))
        await _remove(git, {name: b.sha for name, b in gone.items()})
        left = await remote_heads(git, url) if gone else {}
        result.removed = [b for name, b in gone.items() if name not in left]
        result.deleted = [b.name for b in result.removed]
        return result


async def delete_branches(credentials: Credentials, remote: RepoSpec, key: str, branches: dict[str, str]) -> None:
    where = folder(key)
    if (where / ".git").exists():
        await _remove(Git(credentials, remote, where), branches)
