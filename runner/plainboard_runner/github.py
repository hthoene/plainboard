"""GitHub pull requests through the gh CLI: used where a train of branches is checked by GitHub's
own checks. Everything else about trains is plain git (trains.py) and works with any host."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from plainboard_protocol import CheckResult, Credentials, PullRequestCheck, PullRequestState, UpstreamState

from .workspace import WorkspaceError, git_env, run_cmd

FIELDS = "state,mergeable,headRefOid,headRefName,url,title,statusCheckRollup"
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
MERGE_METHODS = ("squash", "merge", "rebase")  # tried in this order: a repository may allow only some
PULL_URL = re.compile(r"https://\S+/pull/(\d+)")


def check_state(item: dict[str, Any]) -> PullRequestCheck:
    """One entry of GitHub's status roll-up: a check run (Actions and apps) or a commit status."""
    if item.get("__typename") == "StatusContext":
        state = (item.get("state") or "").upper()
        result = "pass" if state == "SUCCESS" else "pending" if state in ("PENDING", "EXPECTED") else "fail"
        return PullRequestCheck(name=item.get("context") or "status", state=result, link=item.get("targetUrl") or "")
    name = " / ".join(part for part in (item.get("workflowName"), item.get("name")) if part) or "check"
    if (item.get("status") or "").upper() != "COMPLETED":
        result = "pending"
    else:
        result = "pass" if (item.get("conclusion") or "").upper() in PASSED else "fail"
    return PullRequestCheck(name=name, state=result, link=item.get("detailsUrl") or "")


def token_summary(response: str) -> tuple[str, list[str] | None]:
    """Who a token belongs to and what it may do, from `gh api -i user`: the login and the scopes.
    Scopes None: a fine-grained token, whose rights GitHub does not list."""
    head, _, body = response.replace("\r\n", "\n").partition("\n\n")
    login = json.loads(body)["login"]
    found = re.search(r"(?im)^x-oauth-scopes:(.*)$", head)
    return login, None if found is None else [scope.strip() for scope in found.group(1).split(",") if scope.strip()]


def parse_state(data: dict[str, Any]) -> PullRequestState:
    mergeable = {"MERGEABLE": "yes", "CONFLICTING": "conflict"}.get((data.get("mergeable") or "").upper(), "unknown")
    # A re-run adds a second entry for the same check: the last one counts.
    checks = {c.name: c for c in (check_state(item) for item in data.get("statusCheckRollup") or [])}
    return PullRequestState(
        state={"MERGED": "merged", "CLOSED": "closed"}.get((data.get("state") or "").upper(), "open"),
        mergeable=mergeable,  # type: ignore[arg-type]
        head_sha=data.get("headRefOid") or "",
        branch=data.get("headRefName") or "",
        url=data.get("url") or "",
        title=data.get("title") or "",
        checks=list(checks.values()),
    )


async def pull_request(credentials: Credentials, repo: str, number: int) -> PullRequestState:
    out = await run_cmd(["gh", "pr", "view", str(number), "--repo", repo, "--json", FIELDS], cwd=None, env=git_env(credentials), timeout=60)
    return parse_state(json.loads(out))


async def open_pull_request(credentials: Credentials, repo: str, base: str, head: str, title: str, body: str) -> tuple[int, str]:
    """The open pull request of a branch, created if there is none: its number and URL."""
    env = git_env(credentials)
    found = json.loads(await run_cmd(["gh", "pr", "list", "--repo", repo, "--head", head, "--state", "open", "--json", "number,url"], cwd=None, env=env, timeout=60) or "[]")
    if found:
        return found[0]["number"], found[0]["url"]
    out = await run_cmd(["gh", "pr", "create", "--repo", repo, "--base", base, "--head", head, "--title", title, "--body", body], cwd=None, env=env, timeout=120)
    created = PULL_URL.search(out)
    if not created:
        raise WorkspaceError(f"Could not open the pull request: {out[-300:]}")
    return int(created.group(1)), created.group(0)


async def merge(credentials: Credentials, repo: str, number: int, methods: tuple[str, ...] | list[str] = MERGE_METHODS) -> CheckResult:
    env = git_env(credentials)
    error = ""
    for method in methods or MERGE_METHODS:
        try:
            await run_cmd(["gh", "pr", "merge", str(number), "--repo", repo, f"--{method}", "--delete-branch"], cwd=None, env=env, timeout=120)
            return CheckResult(ok=True, detail=method)
        except WorkspaceError as exc:
            error = str(exc)
            if "not allowed" not in error.lower() and "not enabled" not in error.lower():
                break  # not about the merge method: another one would fail the same way
    return CheckResult(ok=False, detail=error[-600:])


async def commit_checks(credentials: Credentials, repo: str, sha: str) -> list[PullRequestCheck]:
    """The check runs of a commit (what a push to a branch started), the latest per name. For a base
    branch that got its branches before the checks. With a token that may not read check runs: the
    workflow runs of Actions for the commit, and its statuses."""
    env = git_env(credentials)
    try:
        out = await run_cmd(
            ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100", "--paginate", "--jq", ".check_runs[] | {name, status, conclusion, html_url}"],
            cwd=None, env=env, timeout=90,
        )  # fmt: skip
        return parse_check_runs([json.loads(line) for line in out.splitlines() if line.strip()])
    except WorkspaceError as refused:
        # Not every token may read check runs (seen with a project's own fine-grained token). What Actions
        # ran for the commit is asked instead ("Actions: read"), and the commit's statuses.
        try:
            out = await run_cmd(
                ["gh", "api", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100", "--jq", ".workflow_runs[] | {name, status, conclusion, html_url}"],
                cwd=None, env=env, timeout=90,
            )  # fmt: skip
        except WorkspaceError as also:
            raise WorkspaceError(f"{str(refused)[-250:]} | the runs of Actions neither: {str(also)[-250:]}") from also
    checks = {c.name: c for c in parse_check_runs([json.loads(line) for line in out.splitlines() if line.strip()])}
    try:
        out = await run_cmd(["gh", "api", f"repos/{repo}/commits/{sha}/status", "--jq", ".statuses[] | {context, state, target_url}"], cwd=None, env=env, timeout=60)
    except WorkspaceError:
        out = ""  # without "Commit statuses: read": the runs of Actions are what there is
    for item in (json.loads(line) for line in out.splitlines() if line.strip()):
        state = {"success": "pass", "pending": "pending"}.get(item.get("state") or "", "fail")
        name = item.get("context") or "status"
        checks.setdefault(name, PullRequestCheck(name=name, state=state, link=item.get("target_url") or ""))  # type: ignore[arg-type]
    return list(checks.values())


async def rerun_failed(credentials: Credentials, repo: str, run_ids: list[int]) -> int:
    """Re-run the failed jobs of workflow runs of Actions; how many runs were started again. A token
    without "Actions: write" may not: then 0, and nothing else happens."""
    started = 0
    for run_id in run_ids:
        try:
            await run_cmd(["gh", "api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs"], cwd=None, env=git_env(credentials), timeout=60)
            started += 1
        except WorkspaceError:
            pass
    return started


def parse_check_runs(items: list[dict[str, Any]]) -> list[PullRequestCheck]:
    checks: dict[str, PullRequestCheck] = {}
    for item in items:
        conclusion = (item.get("conclusion") or "").upper()
        if (item.get("status") or "").upper() != "COMPLETED":
            state = "pending"
        elif conclusion in PASSED:
            state = "pass"
        else:
            state = "cancelled" if conclusion in ("CANCELLED", "STALE") else "fail"
        checks[item.get("name") or "check"] = PullRequestCheck(name=item.get("name") or "check", state=state, link=item.get("html_url") or "")  # type: ignore[arg-type]
    return list(checks.values())


API = "https://api.github.com"


async def upstream(credentials: Credentials, repository: str, branch: str, commit: str, *, transport: httpx.AsyncBaseTransport | None = None) -> UpstreamState | None:
    """How many commits ``branch`` of ``repository`` has that ``commit`` has not. None if GitHub does
    not know the commit there (a local build, a fork). Asked without a token first: a public
    repository needs none, and the user's token is not sent where it is not needed."""
    url = f"{API}/repos/{repository}/compare/{commit}...{branch}"
    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        tokens = [None, credentials.github_token] if credentials.github_token else [None]
        for token in tokens:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "plainboard"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                resp = await client.get(url, params={"per_page": 1}, headers=headers)
            except httpx.HTTPError as exc:
                raise WorkspaceError(f"GitHub is not reachable: {exc}") from exc
            if resp.status_code == 200:
                data = resp.json()
                return UpstreamState(behind=int(data.get("ahead_by") or 0), url=data.get("html_url") or "")
        if resp.status_code == 404:
            return None
        raise WorkspaceError(f"GitHub answered {resp.status_code} for {repository}")


async def close(credentials: Credentials, repo: str, number: int) -> bool:
    """Close a pull request that will not be merged, with its branch."""
    try:
        await run_cmd(["gh", "pr", "close", str(number), "--repo", repo, "--delete-branch"], cwd=None, env=git_env(credentials), timeout=60)
        return True
    except WorkspaceError:
        return False
