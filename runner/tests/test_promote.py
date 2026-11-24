"""Promotion (#129) with real git: a fixed commit of dev goes to main, by fast-forward, a merge commit, or a pull request."""

from plainboard_protocol import Credentials, PromotePushSpec, PromoteSpec, RepoSpec

from plainboard_runner import promote, trains
from test_trains import commit, git, heads, remote  # noqa: F401  (remote is a fixture)

KEY = "p1-r1-release"


def plan_spec(remote, **extra) -> PromoteSpec:  # noqa: F811
    return PromoteSpec(credentials=Credentials(), key=KEY, remote=RepoSpec(name="api", url=str(remote)), base="dev", release="main", **extra)


def push_spec(remote, sha: str, **extra) -> PromotePushSpec:  # noqa: F811
    return PromotePushSpec(credentials=Credentials(), key=KEY, remote=RepoSpec(name="api", url=str(remote)), base="dev", release="main", sha=sha, **extra)


def with_dev(tmp_path) -> str:
    """dev = main + feat-a and feat-b landed; returns its head."""
    work = tmp_path / "work"
    git(work, "checkout", "-q", "-b", "dev", "main")
    for branch in ("feat-a", "feat-b"):
        git(work, "merge", "-q", "--no-ff", "-m", f"Land {branch}", branch)
    git(work, "push", "-q", "origin", "dev")
    return git(work, "rev-parse", "HEAD")


async def test_a_promotion_fast_forwards_main_to_the_checked_commit_and_tags_it(remote, tmp_path):  # noqa: F811
    head = with_dev(tmp_path)
    plan = await promote.plan(plan_spec(remote, checkout=True))
    assert plan.sha == head and plan.fast_forward and plan.release_sha == heads(remote)["main"]
    assert plan.total == 4 and [c.message for c in plan.commits] == ["work on feat-b", "work on feat-a"]
    assert plan.first_parent[0] == head  # the base branch's own commits: the train heads that landed
    assert git(trains.folder(KEY), "rev-parse", "HEAD") == head  # checked out for the check command

    # dev moves on meanwhile: the promotion keeps its fixed commit.
    work = tmp_path / "work"
    commit(work, "late.txt", "late\n", "late work")
    git(work, "push", "-q", "origin", "dev")
    result = await promote.push(push_spec(remote, head, tag="v1.0"))
    assert result.ok and result.how == "fast-forward" and result.sha == head and not result.detail
    assert heads(remote)["main"] == head
    assert git(remote, "rev-parse", "v1.0^{commit}") == head


async def test_main_with_commits_of_its_own_gets_a_merge_commit(remote, tmp_path):  # noqa: F811
    head = with_dev(tmp_path)
    work = tmp_path / "work"
    git(work, "checkout", "-q", "main")
    commit(work, "hotfix.txt", "fix\n", "hotfix on main")
    git(work, "push", "-q", "origin", "main")
    hotfix = heads(remote)["main"]
    plan = await promote.plan(plan_spec(remote))
    assert not plan.fast_forward and plan.release_sha == hotfix
    result = await promote.push(push_spec(remote, head))
    assert result.ok and result.how == "merge"
    assert git(remote, "rev-parse", "main^1") == hotfix and git(remote, "rev-parse", "main^2") == head


async def test_a_protected_main_gets_a_pull_request_from_a_branch_at_the_checked_commit(remote, tmp_path, monkeypatch):  # noqa: F811
    head = with_dev(tmp_path)
    before = heads(remote)["main"]
    hook = remote / "hooks" / "pre-receive"
    hook.write_text('#!/bin/sh\nwhile read old new ref; do [ "$ref" = refs/heads/main ] && { echo "remote: error: GH006: Protected branch update failed for refs/heads/main." >&2; exit 1; }; done\nexit 0\n')
    hook.chmod(0o755)
    opened = []

    async def open_pull_request(credentials, repo, base, head_branch, title, body):
        opened.append((repo, base, head_branch, title))
        return 12, "https://github.com/acme/api/pull/12"

    monkeypatch.setattr(promote.github, "open_pull_request", open_pull_request)
    result = await promote.push(push_spec(remote, head, github="acme/api", tag="v2"))
    assert result.ok and result.how == "pull-request" and result.number == 12 and result.url.endswith("/pull/12")
    branch = f"promote/dev-{head[:12]}"
    assert opened == [("acme/api", "main", branch, "Promote dev to main (v2)")]
    assert heads(remote)["main"] == before and heads(remote)[branch] == head  # main untouched, the pull request has exactly the checked commit

    # Not on GitHub: nothing to fall back to, the refusal is the answer.
    refused = await promote.push(push_spec(remote, head))
    assert not refused.ok and "GH006" in refused.detail and heads(remote)["main"] == before
