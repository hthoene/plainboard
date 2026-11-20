"""The base branch of a repository whose trains land after their checks is guarded as well: red after a
landing, or after a push past PlainBoard, it is repaired, and nobody forgets it while it stays red."""

import json
from datetime import timedelta

from conftest import idle, wait_for
from plainboard_protocol import PullRequestCheck
from test_flow import flow_events, hand_over, land_tick_at, last_update, repairs, runner, team_with_branches

RED = [PullRequestCheck(name="Tests", state="fail", link="https://github.com/acme/api/actions/runs/77/job/1"), PullRequestCheck(name="Lint", state="pass")]
GREEN = [PullRequestCheck(name="Tests", state="pass"), PullRequestCheck(name="Lint", state="pass")]


def base_events(client, pid: int) -> list[str]:
    return [e["kind"] for e in flow_events(client, pid) if e["kind"].startswith("base.")]


def quiet(client, pid: int) -> bool:
    """Whether a status report would be left out as "nothing happened" (a schedule whose last run is after everything)."""
    from plainboard.db import sessionmaker
    from plainboard.models import Schedule, utcnow
    from plainboard.services import schedules

    async def look() -> bool:
        async with sessionmaker()() as s:
            schedule = Schedule(project_id=pid, name="Status", prompt="SAY status", kind="cron", cron="0 * * * *", last_run_at=utcnow() + timedelta(days=1))
            return await schedules.quiet(s, schedule)

    return client.portal.call(look)


def told_of_red(client, pid: int) -> int:
    from plainboard.db import sessionmaker
    from plainboard.models import Agent, InboxItem
    from sqlalchemy import select

    async def count() -> int:
        async with sessionmaker()() as s:
            manager = await s.scalar(select(Agent.id).where(Agent.project_id == pid, Agent.is_manager))
            texts = await s.scalars(select(InboxItem.text).where(InboxItem.agent_id == manager))
            return sum("fails its checks (Tests) although its trains passed theirs" in text for text in texts)

    return client.portal.call(count)


def test_a_base_that_turns_red_after_a_train_landed_gets_one_repair_and_trains_wait_until_it_is_green(client, project):
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="")
    fake.branches["main"] = "m1"
    fake.commit_checks_of["m1"] = GREEN
    t0 = utcnow() - timedelta(hours=2)  # the looks happen before the repair has finished (it runs at the real time)
    land_tick_at(client, t0)  # the train leaves; the base branch's head is looked at
    land_tick_at(client, t0 + timedelta(minutes=3))
    assert base_events(client, pid) == ["base.green"]
    assert quiet(client, pid)

    # The train passes its own checks and is merged. Its merge moves the base branch: looked at again at once.
    number = fake.train_results[-1].number
    fake.pulls[("acme/api", number)] = fake.pulls[("acme/api", number)].model_copy(update={"checks": [PullRequestCheck(name="ci / test", state="pass")]})
    fake.branches["main"] = "m2"
    fake.commit_checks_of["m2"] = RED
    land_tick_at(client, t0 + timedelta(minutes=4))
    wait_for(lambda: idle(client, pid))
    land_tick_at(client, t0 + timedelta(minutes=8))
    # Red although the train passed: first its failed jobs run once more, it may be flaky.
    assert fake.reruns == [[77]] and base_events(client, pid) == ["base.green", "base.rerun"] and not repairs(client, pid)
    land_tick_at(client, t0 + timedelta(minutes=9))  # the re-run has not shown up yet
    assert not repairs(client, pid)

    # Still red after the re-run: one repair, by the author of what landed, and the manager hears it once.
    land_tick_at(client, t0 + timedelta(minutes=12))
    wait_for(lambda: idle(client, pid) and repairs(client, pid) and repairs(client, pid)[0]["status"] == "done")
    (repair,) = repairs(client, pid)
    assert repair["title"] == "Make main of api pass its checks again"
    prompt = next(s.prompt for s in fake.specs if "Nothing is taken back" in s.prompt)
    assert "although what landed passed its checks before" in prompt and "- feat-a (Dev1)" in prompt and "Tests: https://github.com/acme/api/actions/runs/77/job/1" in prompt
    assert base_events(client, pid) == ["base.green", "base.rerun", "base.red", "base.repair_ordered"]
    repo = client.get(f"/api/projects/{pid}/repositories").json()[0]
    assert repo["red_base"] == "main"
    assert not quiet(client, pid)  # a red base branch is never a quiet project

    # What is finished meanwhile waits: a train on a red base would fail with it. Looks without news order nothing.
    hand_over(client, pid, fake, "Dev2", "feat-b")
    trains = len(fake.train_specs)
    for minutes in (16, 20, 24):
        land_tick_at(client, t0 + timedelta(minutes=minutes))
        wait_for(lambda: idle(client, pid))
    assert len(fake.train_specs) == trains and len(repairs(client, pid)) == 1

    # Someone pushes past PlainBoard before the repair's fix is there: still red for the same reason, no second repair.
    fake.branches["main"] = "m3"
    fake.commit_checks_of["m3"] = RED
    land_tick_at(client, t0 + timedelta(minutes=28))
    land_tick_at(client, t0 + timedelta(minutes=32))
    land_tick_at(client, t0 + timedelta(minutes=36))
    wait_for(lambda: idle(client, pid))
    assert len(repairs(client, pid)) == 1 and base_events(client, pid)[-3:] == ["base.rerun", "base.red", "base.still_red"]
    assert told_of_red(client, pid) == 1  # with its next turn, once per red phase

    # Green: the marker goes, and what waited leaves on the next train.
    fake.branches["main"] = "m4"
    fake.commit_checks_of["m4"] = GREEN
    land_tick_at(client, t0 + timedelta(minutes=40))
    land_tick_at(client, t0 + timedelta(minutes=44))
    wait_for(lambda: idle(client, pid))
    assert base_events(client, pid)[-1] == "base.green"
    assert client.get(f"/api/projects/{pid}/repositories").json()[0]["red_base"] is None
    land_tick_at(client, t0 + timedelta(minutes=48))
    assert len(fake.train_specs) == trains + 1 and fake.train_specs[-1].members == ["feat-b"]
    assert len(repairs(client, pid)) == 1


def test_a_red_base_nobody_repairs_is_brought_up_again_every_few_hours_until_it_is_green(client, project):
    from plainboard.models import utcnow

    pid = project["id"]
    body = {"url": "https://github.com/acme/api.git", "check_command": "", "default_branch": "main"}
    assert client.post(f"/api/projects/{pid}/repositories", json=body).status_code == 201
    fake = runner(client)
    # Merged by hand, and no member works in this repository: nobody PlainBoard could give the repair to.
    fake.branches["main"] = "x1"
    fake.commit_checks_of["x1"] = [PullRequestCheck(name="Tests", state="fail", link="https://ci/1")]
    t0 = utcnow() - timedelta(hours=14)
    land_tick_at(client, t0)
    land_tick_at(client, t0 + timedelta(minutes=5))
    wait_for(lambda: idle(client, pid))
    assert base_events(client, pid) == ["base.red", "base.escalated"] and not fake.reruns  # no run of Actions to run again
    assert "main of api does not pass its checks (Tests), and nobody who wrote what landed is available" in last_update(fake)

    reminded = lambda: base_events(client, pid).count("base.reminded")  # noqa: E731
    land_tick_at(client, t0 + timedelta(hours=1))
    assert reminded() == 0
    land_tick_at(client, t0 + timedelta(hours=4, minutes=10))
    wait_for(lambda: idle(client, pid) and "has been red for 4 h" in last_update(fake))
    assert reminded() == 1 and "nothing repairs it now" in last_update(fake) and "base_repair=true" in last_update(fake)
    land_tick_at(client, t0 + timedelta(hours=5))
    assert reminded() == 1
    land_tick_at(client, t0 + timedelta(hours=8, minutes=20))
    wait_for(lambda: idle(client, pid))
    assert reminded() == 2

    # Green: no more reminders.
    fake.branches["main"] = "x2"
    fake.commit_checks_of["x2"] = GREEN
    land_tick_at(client, t0 + timedelta(hours=8, minutes=45))
    land_tick_at(client, t0 + timedelta(hours=8, minutes=50))
    assert base_events(client, pid)[-1] == "base.green"
    land_tick_at(client, t0 + timedelta(hours=13))
    assert reminded() == 2
    assert json.dumps(client.get(f"/api/projects/{pid}/repositories").json()[0]["red_base"]) == "null"
