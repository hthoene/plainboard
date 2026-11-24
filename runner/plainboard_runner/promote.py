"""Promotion (#129): a fixed commit of the base branch (e.g. dev) goes to the release branch (e.g. main).

Plain git in a clone of the runner's own, separate from the repository's train clone: trains of the base
branch go on while a promotion is checked. The app decides when; the check command runs as a job in this
clone (``trains.start_check`` with the promotion's key)."""

from __future__ import annotations

from plainboard_protocol import GitCommit, PromotePlan, PromotePushSpec, PromoteResult, PromoteSpec

from . import github
from .trains import Git, _clone, _lock, default_branch, folder, remote_heads
from .workspace import WorkspaceError

FIRST_PARENT_LIMIT = 1000
# What a host answers when it refuses a push to a branch on purpose (GitHub: GH006 protected branch, GH013 rulesets).
REFUSED = ("protected branch", "GH006", "GH013", "declined", "denied", "not allowed", "protected")


def refused(detail: str) -> bool:
    return any(mark in detail for mark in REFUSED) and "fetch first" not in detail and "non-fast-forward" not in detail


async def _fetch(git: Git, spec: PromoteSpec | PromotePushSpec, heads: dict[str, str], sha: str | None) -> None:
    refs = [f"+refs/heads/{name}:refs/remotes/origin/{name}" for name in (spec.base, spec.release) if name in heads]
    await git("fetch", "-q", "--no-tags", "origin", *refs)
    if sha and not await git.ok("cat-file", "-e", f"{sha}^{{commit}}"):
        await git("fetch", "-q", "--no-tags", "origin", sha)


async def plan(spec: PromoteSpec) -> PromotePlan:
    """What the release branch lacks of the base branch at ``spec.sha`` (or its head now)."""
    async with _lock(spec.key):
        git = await _clone(spec.credentials, spec.remote, folder(spec.key))
        heads = await remote_heads(git, spec.remote.url)
        spec = spec.model_copy(update={"base": spec.base or await default_branch(git, spec.remote.url)})
        if spec.base not in heads:
            raise WorkspaceError(f"Base branch {spec.base} not found on the remote")
        if spec.base == spec.release:
            raise WorkspaceError(f"The release branch is the base branch ({spec.base}): nothing to promote")
        await _fetch(git, spec, heads, spec.sha)
        sha = (await git("rev-parse", f"{spec.sha or heads[spec.base]}^{{commit}}")).strip()
        release_sha = heads.get(spec.release)
        span = f"{release_sha}..{sha}" if release_sha else sha
        result = PromotePlan(base=spec.base or "", release=spec.release, sha=sha, release_sha=release_sha)
        result.fast_forward = release_sha is None or await git.ok("merge-base", "--is-ancestor", release_sha, sha)
        result.total = int((await git("rev-list", "--count", span)).strip() or 0)
        out = await git("log", "--no-merges", f"-n{spec.limit}", "--format=%x1e%H%x1f%an%x1f%cI%x1f%s", span, timeout=300)
        for record in (r for r in out.split("\x1e") if r.strip()):
            sha_, author, date, subject = (record.split("\x1f", 3) + [""] * 4)[:4]
            result.commits.append(GitCommit(sha=sha_.strip(), author=author, date=date, message=subject.strip()))
        result.first_parent = (await git("rev-list", "--first-parent", f"--max-count={FIRST_PARENT_LIMIT}", span)).split()
        if spec.checkout:
            await git.ok("merge", "--abort")
            await git("checkout", "-q", "-f", "--detach", sha)
            await git("clean", "-q", "-fdx")
        return result


async def push(spec: PromotePushSpec) -> PromoteResult:
    """The checked commit to the release branch: fast-forward, else a merge commit; never forced. Refused by
    the host (a protected branch): a pull request from a branch at that commit, if the host is GitHub."""
    async with _lock(spec.key):
        git = await _clone(spec.credentials, spec.remote, folder(spec.key))
        try:
            heads = await remote_heads(git, spec.remote.url)
            await _fetch(git, spec, heads, spec.sha)
        except WorkspaceError as exc:
            return PromoteResult(ok=False, detail=str(exc)[-600:])
        release_sha = heads.get(spec.release)
        target, how = spec.sha, "fast-forward"
        if release_sha and release_sha != spec.sha and not await git.ok("merge-base", "--is-ancestor", release_sha, spec.sha):
            if await git.ok("merge-base", "--is-ancestor", spec.sha, release_sha):
                return PromoteResult(ok=True, how="fast-forward", sha=release_sha, detail=f"{spec.release} has this commit already")
            await git.ok("merge", "--abort")
            await git("checkout", "-q", "-f", "--detach", release_sha)
            message = spec.title or f"Promote {spec.base} to {spec.release}"
            if not await git.ok("merge", "-q", "--no-ff", "--no-edit", "-m", message, spec.sha):
                await git.ok("merge", "--abort")
                return PromoteResult(ok=False, detail=f"{spec.base} at {spec.sha[:12]} conflicts with {spec.release}: it has commits {spec.base} lacks. Merge {spec.release} into {spec.base} first.")
            target, how = (await git("rev-parse", "HEAD")).strip(), "merge"
        try:
            if target != release_sha:
                await git("push", "-q", "origin", f"{target}:refs/heads/{spec.release}", timeout=300)
        except WorkspaceError as exc:
            detail = str(exc)[-600:]
            if not (spec.github and refused(detail)):
                return PromoteResult(ok=False, detail=detail)
            branch = f"promote/{spec.base}-{spec.sha[:12]}"
            try:
                await git("push", "-q", "--force", "origin", f"{spec.sha}:refs/heads/{branch}", timeout=300)
                title = spec.title or f"Promote {spec.base} to {spec.release}" + (f" ({spec.tag})" if spec.tag else "")
                body = f"{spec.release} refused a direct push, so PlainBoard opened this pull request for {spec.base} at {spec.sha[:12]}, which passed its checks."
                number, url = await github.open_pull_request(spec.credentials, spec.github, spec.release, branch, title, body)
            except WorkspaceError as pr_exc:
                return PromoteResult(ok=False, detail=f"{detail}\nThe pull request could not be opened either: {str(pr_exc)[-300:]}")
            return PromoteResult(ok=True, how="pull-request", sha=spec.sha, number=number, url=url, detail=detail)
        result = PromoteResult(ok=True, how=how, sha=target)
        if spec.tag:
            if not (await git.ok("tag", "-a", spec.tag, "-m", spec.tag, target) and await git.ok("push", "-q", "origin", f"refs/tags/{spec.tag}")):
                result.detail = f"The tag {spec.tag} could not be set (it may exist already)."
        return result
