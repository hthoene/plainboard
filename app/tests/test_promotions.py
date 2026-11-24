"""Promotion (#129): the base branch (dev) brought to the release branch (main) when asked, after one check
run on exactly that commit; what it brings is shown before, and the tickets say they were released."""

import json

from conftest import idle, wait_for
from plainboard_protocol import GitCommit, JobState, PromotePlan, PromoteResult
from test_flow import chat, finish_check, flow_events, land_tick, manager_of, team_with_branches, tool_results


def landed_on_dev(client, pid: int):
    """feat-a landed on dev (by a train with head train-1) for ticket #1; main is the release branch."""
    fake = team_with_branches(client, pid, ["feat-a"])
    land_tick(client)
    finish_check(client, fake)
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "Login page"}).json()
    from plainboard.db import sessionmaker
    from plainboard.models import Landing
    from sqlalchemy import update

    async def on_ticket() -> None:
        async with sessionmaker()() as s:
            await s.execute(update(Landing).where(Landing.branch == "feat-a").values(task_id=task["id"]))
            await s.commit()

    client.portal.call(on_ticket)
    (repo,) = client.get(f"/api/projects/{pid}/repositories").json()
    resp = client.patch(f"/api/repositories/{repo['id']}", json={"default_branch": "dev", "release_branch": "main"})
    assert resp.status_code == 200 and resp.json()["release_branch"] == "main"
    fake.promote_plan = PromotePlan(
        base="dev", release="main", sha="c0ffee1", release_sha="abc1234", total=3,
        commits=[GitCommit(sha="c0ffee2", author="Dev1", message="work on feat-a")], first_parent=["c0ffee1", "train-1"],
    )  # fmt: skip
    return fake, repo, task


def told(client, pid: int) -> list[str]:
    from plainboard.db import sessionmaker
    from plainboard.models import Agent, InboxItem
    from sqlalchemy import select

    async def texts() -> list[str]:
        async with sessionmaker()() as s:
            manager = await s.scalar(select(Agent.id).where(Agent.project_id == pid, Agent.is_manager))
            return list(await s.scalars(select(InboxItem.text).where(InboxItem.agent_id == manager).order_by(InboxItem.id)))

    return client.portal.call(texts)


def promotions(client, pid: int) -> list[dict]:
    return client.get(f"/api/projects/{pid}/flow").json()["promotions"]


def test_the_preview_shows_what_goes_and_a_promotion_checks_that_commit_once_then_fast_forwards_main(client, project):
    pid = project["id"]
    fake, repo, task = landed_on_dev(client, pid)
    preview = client.get(f"/api/repositories/{repo['id']}/promotion").json()
    assert preview["base"] == "dev" and preview["release"] == "main" and preview["total"] == 3 and preview["fast_forward"]
    assert [(t["task_number"], t["branch"], t["title"]) for t in preview["tickets"]] == [(task["number"], "feat-a", "Login page")]
    assert not preview["checked"] and preview["commits"][0]["message"] == "work on feat-a"

    # The manager promotes when the user asks: one check run on exactly that commit, in a clone of its own.
    manager = manager_of(client, pid)
    chat(client, manager["id"], "TOOL promotion_preview " + json.dumps({"repository": "api"}) + "\nTOOL promote_branch " + json.dumps({"repository": "api", "tag": "v1.0"}))
    wait_for(lambda: idle(client, pid))
    shown, started = tool_results(client, manager["id"])[-2:]
    assert f"#{task['number']}: Login page" in shown and "3 commits, fast-forward" in shown
    assert "Promotion of dev at c0ffee1 to main started (3 commits, 1 branches; its check runs now)" in started
    check = fake.check_specs[-1]
    assert check.command == "./ci.sh" and check.key.endswith("-release") and fake.promotion_plans[-1].checkout
    assert client.post(f"/api/repositories/{repo['id']}/promotion", json={}).status_code == 400  # one at a time

    land_tick(client)  # still checking: nothing pushed
    assert not fake.promotion_pushes and promotions(client, pid)[0]["status"] == "checking"
    fake.jobs[check.id] = JobState(running=False, exit_code=0, log_path="/jobs/p.log", tail="ok")
    land_tick(client)
    (push,) = fake.promotion_pushes
    assert (push.sha, push.base, push.release, push.tag) == ("c0ffee1", "dev", "main", "v1.0")
    (done,) = promotions(client, pid)
    assert done["status"] == "promoted" and done["how"] == "fast-forward" and done["tickets"] == 1 and done["by"] == "manager"
    assert any("Promoted dev at c0ffee1 (3 commits, 1 branches, tag v1.0) to main of api (fast-forward)" in t for t in told(client, pid))
    (way,) = client.get(f"/api/tasks/{task['id']}/flow").json()
    assert way["steps"][-1] == {"key": "released", "state": "done", "at": way["steps"][-1]["at"], "detail": "main (v1.0)"}
    assert [e["kind"] for e in flow_events(client, pid) if e["kind"].startswith("promotion.")] == ["promotion.started", "promotion.promoted"]

    # Promoted branches are not offered again.
    assert client.get(f"/api/repositories/{repo['id']}/promotion").json()["tickets"] == []


def test_a_failed_check_pushes_nothing_and_the_manager_hears_why(client, project):
    pid = project["id"]
    fake, repo, _ = landed_on_dev(client, pid)
    resp = client.post(f"/api/repositories/{repo['id']}/promotion", json={"sha": "c0ffee1"})
    assert resp.status_code == 201 and resp.json()["by"]
    fake.jobs[fake.check_specs[-1].id] = JobState(running=False, exit_code=1, log_path="/jobs/p.log", tail="FAILED test_login")
    land_tick(client)
    assert not fake.promotion_pushes
    (failed,) = promotions(client, pid)
    assert failed["status"] == "failed" and "exit code 1" in failed["detail"] and "FAILED test_login" in failed["detail"]
    assert any("The promotion of dev at c0ffee1" in t and "FAILED test_login" in t for t in told(client, pid))


def test_a_commit_that_passed_before_is_not_checked_again_and_a_protected_main_becomes_a_pull_request(client, project):
    pid = project["id"]
    fake, repo, task = landed_on_dev(client, pid)
    from plainboard.db import sessionmaker
    from plainboard.models import Train
    from sqlalchemy import update

    async def checked_head() -> None:  # dev's head is what the train checked and landed
        async with sessionmaker()() as s:
            await s.execute(update(Train).values(head_sha="beef123"))
            await s.commit()

    client.portal.call(checked_head)
    fake.promote_plan = fake.promote_plan.model_copy(update={"sha": "beef123", "first_parent": ["beef123"]})
    fake.promote_result = PromoteResult(ok=True, how="pull-request", sha="beef123", number=7, url="https://gitlab.com/acme/api/-/merge_requests/7", detail="protected branch")
    assert client.get(f"/api/repositories/{repo['id']}/promotion").json()["checked"]
    checks = len(fake.check_specs)
    assert client.post(f"/api/repositories/{repo['id']}/promotion", json={}).status_code == 201
    assert len(fake.check_specs) == checks  # no further check run
    land_tick(client)
    (pr,) = promotions(client, pid)
    assert pr["status"] == "pull_request" and pr["url"].endswith("/7")
    assert any("refused a direct push" in t and pr["url"] in t for t in told(client, pid))
    (way,) = client.get(f"/api/tasks/{task['id']}/flow").json()
    assert way["steps"][-1]["key"] == "released" and way["steps"][-1]["state"] == "now"


def test_without_a_release_branch_there_is_nothing_to_promote(client, project):
    pid = project["id"]
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://gitlab.com/acme/api.git"}).status_code == 201
    (repo,) = client.get(f"/api/projects/{pid}/repositories").json()
    resp = client.get(f"/api/repositories/{repo['id']}/promotion")
    assert resp.status_code == 400 and "no release branch" in resp.json()["detail"]
    assert client.get(f"/api/projects/{pid}/flow").json()["promotions"] == []
