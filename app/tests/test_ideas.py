"""A project's ideas (#91): read, sorted and rearranged by a model, undoable, and turned into tickets by the manager."""

import json
import re

import pytest
from conftest import agents_by_slug, idle, runs_of, wait_for
from plainboard_protocol import CompletionResult
from test_flow import chat, tool_results
from test_transfer import bring_in, export
from test_users import new_user, sign_in

from plainboard.orchestration import ideas as idea_orchestration
from plainboard.services import ideas as idea_service

KINDS = {idea_service.SORT_SYSTEM: "sort", idea_service.REORDER_SYSTEM: "reorder", idea_service.INTENT_SYSTEM: "intent"}


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(idea_orchestration, "SORT_DELAY", 0.0)


def answer(client, fail: bool = False, **queues) -> dict[str, list]:
    """The model's answers per kind of call (sort, reorder, intent), in turn: a text, or a function of the
    spec. An empty queue answers "". Returns the specs asked, per kind."""
    runner = client.app_ref.state.runner
    asked: dict[str, list] = {"sort": [], "reorder": [], "intent": [], "other": []}
    queues = {k: list(v) for k, v in queues.items()}

    async def complete(spec):
        runner.database_free("complete")
        kind = KINDS.get(spec.system_prompt, "other")
        asked[kind].append(spec)
        if fail:
            raise RuntimeError("model down")
        queue = queues.get(kind) or []
        text = queue.pop(0) if queue else ""
        return CompletionResult(text=text(spec) if callable(text) else text, input_tokens=50, output_tokens=20, cost_usd=0.001)

    runner.complete = complete
    return asked


def ids_in(spec, section: str) -> list[int]:
    return [int(n) for n in re.findall(r"^\s*i(\d+) ", spec.prompt.split(section)[1], re.M)]


def ideas_of(client, pid: int, **params) -> dict:
    resp = client.get(f"/api/projects/{pid}/ideas", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def add(client, pid: int, text: str, **more) -> dict:
    resp = client.post(f"/api/projects/{pid}/ideas", json={"text": text, **more})
    assert resp.status_code == 201, resp.text
    return resp.json()


def typed(client, pid: int, text: str) -> dict:
    resp = client.post(f"/api/projects/{pid}/ideas/input", json={"text": text})
    assert resp.status_code == 201, resp.text
    return resp.json()


def settled(client, pid: int) -> dict:
    wait_for(lambda: not (data := ideas_of(client, pid))["sorting"] and all(i["sort_state"] != "pending" for i in data["ideas"]))
    return ideas_of(client, pid)


def by_id(data: dict) -> dict[int, dict]:
    return {i["id"]: i for i in data["ideas"]}


def test_ideas_are_sorted_together_with_similar_ones_and_duplicates(client, project, monkeypatch):
    pid = project["id"]

    def first(spec) -> str:
        (idea_id,) = ids_in(spec, "Sort these:")
        return json.dumps({"ideas": [{"id": idea_id, "topic": "Payment"}], "topics": [{"name": "Payment", "hint": "1 idea about payment"}]})

    asked = answer(client, sort=[first])
    one = add(client, pid, "Pay by card")
    data = settled(client, pid)
    (topic,) = data["topics"]
    assert (topic["name"], topic["hint"], topic["color"], topic["manual"]) == ("Payment", "1 idea about payment", 0, False)
    assert asked["sort"][0].model == "claude-haiku-5-5"  # the provider's small model, by default

    monkeypatch.setattr(idea_orchestration, "SORT_DELAY", 0.5)  # ideas that come quickly are sorted together

    def second(spec) -> str:
        two, three = ids_in(spec, "Sort these:")
        reply = {
            "ideas": [
                {"id": two, "topic": "payment", "similar": [one["id"], 999], "hint": f"similar to i{one['id']}"},
                {"id": f"i{three}", "topic": "Payment", "duplicate_of": one["id"]},
            ],
            "topics": [{"name": "Payment", "hint": "3 ideas about payment"}],
        }
        return "Here you go:\n```json\n" + json.dumps(reply)[:-1] + ",}\n```"

    asked = answer(client, sort=[second])
    two, three = add(client, pid, "PayPal too", title="PayPal"), add(client, pid, "Accept credit cards")
    data = settled(client, pid)
    assert len(asked["sort"]) == 1  # both in one call
    ideas = by_id(data)
    assert {i["topic_id"] for i in ideas.values()} == {topic["id"]} and data["topics"][0]["hint"] == "3 ideas about payment"
    assert ideas[two["id"]]["similar"] == [one["id"]] and ideas[three["id"]]["duplicate_of"] == one["id"]
    assert sorted(ideas[one["id"]]["similar"]) == sorted([two["id"], three["id"]])
    runs = [r for r in runs_of(client, pid)]
    assert runs and all(r["trigger"] == "ideas" and r["input_text"] == "" for r in runs)
    assert client.get(f"/api/projects/{pid}/tasks").json() == []  # sorting starts nothing


def test_a_broken_answer_or_a_failing_model_leaves_the_idea_unsorted(client, project):
    pid = project["id"]
    answer(client, sort=["Sorry, I cannot do that."])
    idea = add(client, pid, "Dark mode")
    (data,) = settled(client, pid)["ideas"]
    assert data["id"] == idea["id"] and data["sort_state"] == "failed" and data["topic_id"] is None

    answer(client, sort=[json.dumps({"ideas": [{"id": "nonsense"}], "topics": "x"})])
    client.patch(f"/api/projects/{pid}/ideas/{idea['id']}", json={"text": "Dark mode for the night"})
    (data,) = settled(client, pid)["ideas"]
    assert data["sort_state"] == "failed" and data["topic_id"] is None

    answer(client, fail=True)
    client.patch(f"/api/projects/{pid}/ideas/{idea['id']}", json={"text": "Dark mode, also on phones"})
    (data,) = settled(client, pid)["ideas"]
    assert data["sort_state"] == "failed" and data["text"] == "Dark mode, also on phones"
    assert [r["status"] for r in runs_of(client, pid) if r["trigger"] == "ideas"].count("failed") == 2  # the second answered, wrongly


def test_parse_json_finds_the_object_in_what_a_model_writes():
    assert idea_service.parse_json('```json\n{"a": [1, 2,],}\n```') == {"a": [1, 2]}
    assert idea_service.parse_json('Sure! {"a": 1} Hope that helps.') == {"a": 1}
    for bad in ("", "[1, 2]", "{not json", None):
        with pytest.raises(ValueError):
            idea_service.parse_json(bad)


def test_topics_are_rearranged_after_a_few_ideas_within_the_rules_and_it_can_be_undone(client, project):
    pid = project["id"]
    answer(client)
    fixed_topic = client.post(f"/api/projects/{pid}/ideas/topics", json={"name": "Mine"}).json()
    pinned = add(client, pid, "Logo in blue", topic_id=fixed_topic["id"])
    old = client.post(f"/api/projects/{pid}/ideas/topics", json={"name": "Stuff"}).json()
    client.patch(f"/api/projects/{pid}/ideas/topics/{old['id']}", json={"x": 10})
    settled(client, pid)

    def sort(spec) -> str:
        return json.dumps({"ideas": [{"id": i, "topic": "Stuff"} for i in ids_in(spec, "Sort these:")]})

    def reorder(spec) -> str:
        assert f"id {fixed_topic['id']}: Mine (fixed)" in spec.prompt and "(fixed)" in spec.prompt.split(f"i{pinned['id']} ")[1].splitlines()[0]
        a, b, c = [i for i in ids_in(spec, "Ideas [topic id]:") if i != pinned["id"]]
        return json.dumps(
            {
                "topics": [
                    {"id": fixed_topic["id"], "name": "Renamed by AI", "ideas": [pinned["id"]]},
                    {"id": None, "name": "Payment", "hint": "2 ideas", "ideas": [a, b, pinned["id"]]},
                    {"id": None, "name": "Login", "ideas": [c]},
                ]
            }
        )

    asked = answer(client, sort=[sort] * 3, reorder=[reorder])
    new_ids = [add(client, pid, text)["id"] for text in ("Card", "PayPal", "Google login")]
    wait_for(lambda: len(asked["reorder"]) == 1 and not ideas_of(client, pid)["sorting"])
    data = ideas_of(client, pid)
    names = {t["id"]: t["name"] for t in data["topics"]}
    ideas = by_id(data)
    # A person's topic keeps its name, a person's idea its topic; the emptied topic of the model is gone.
    assert names[fixed_topic["id"]] == "Mine" and ideas[pinned["id"]]["topic_id"] == fixed_topic["id"]
    assert sorted(names.values()) == ["Login", "Mine", "Payment", "Stuff"]  # "Stuff" was made by a person, so it stays
    assert {names[ideas[i]["topic_id"]] for i in new_ids[:2]} == {"Payment"} and names[ideas[new_ids[2]]["topic_id"]] == "Login"
    (change,) = client.get(f"/api/projects/{pid}/ideas/changes").json()
    assert change["kind"] == "reorder" and change["source"] == "auto"
    assert {c["name"] for c in change["summary"]["created"]} == {"Payment", "Login"} and all(c["from"] == ["Stuff"] for c in change["summary"]["created"])
    assert len(change["summary"]["moved"]) == 3 and change["summary"]["renamed"] == []

    undone = client.post(f"/api/projects/{pid}/ideas/changes/{change['id']}/undo")
    assert undone.status_code == 200 and undone.json()["undone_at"]
    data = ideas_of(client, pid)
    assert {t["name"] for t in data["topics"]} == {"Mine", "Stuff"}
    assert all(i["topic_id"] == old["id"] for i in data["ideas"] if i["id"] in new_ids)
    assert client.post(f"/api/projects/{pid}/ideas/changes/{change['id']}/undo").status_code == 409


def test_the_button_rearranges_and_a_model_topic_is_removed_once_empty(client, project):
    pid = project["id"]

    def sort(spec) -> str:
        return json.dumps({"ideas": [{"id": i, "topic": "Misc"} for i in ids_in(spec, "Sort these:")]})

    asked = answer(client, sort=[sort] * 2)
    a, b = add(client, pid, "Card")["id"], add(client, pid, "Chat")["id"]
    misc = settled(client, pid)["topics"][0]

    def reorder(spec) -> str:
        return json.dumps({"topics": [{"id": misc["id"], "name": "Payment", "ideas": [a]}, {"id": None, "name": "Chat", "ideas": [b]}]})

    asked = answer(client, reorder=[reorder])
    assert client.post(f"/api/projects/{pid}/ideas/sort").status_code == 202
    wait_for(lambda: len(asked["reorder"]) == 1 and not ideas_of(client, pid)["sorting"])
    (change,) = client.get(f"/api/projects/{pid}/ideas/changes").json()
    assert change["source"] == "button" and change["summary"]["renamed"] == [{"topic_id": misc["id"], "from": "Misc", "to": "Payment"}]
    assert sorted(t["name"] for t in ideas_of(client, pid)["topics"]) == ["Chat", "Payment"]


def test_what_is_typed_is_read_as_intent_and_every_change_keeps_the_old_text(client, project):
    pid = project["id"]
    answer(client)
    booking = add(client, pid, "Book appointments online", title="Booking")
    other = add(client, pid, "Remind by e-mail", title="Reminder")
    settled(client, pid)

    # An addition to an existing idea: the old text stays as a version, and it can be undone.
    amend = {"action": "amend", "target": booking["id"], "title": "Booking", "text": "Book appointments online, with a reminder by SMS"}
    asked = answer(client, intent=[json.dumps(amend)])
    change = typed(client, pid, "bei der Terminbuchung auch Erinnerung per SMS")
    assert "i%d Booking: Book appointments online" % booking["id"] in asked["intent"][0].prompt
    assert change["kind"] == "amend" and change["summary"]["idea_id"] == booking["id"]
    assert change["summary"]["before"]["text"] == "Book appointments online" and change["summary"]["after"]["text"].endswith("by SMS")
    assert [i["id"] for i in change["ideas"]] == [booking["id"]] and change["ideas"][0]["text"].endswith("by SMS")
    (version,) = client.get(f"/api/projects/{pid}/ideas/{booking['id']}/versions").json()
    assert (version["text"], version["source"]) == ("Book appointments online", "ai")
    client.post(f"/api/projects/{pid}/ideas/changes/{change['id']}/undo")
    assert by_id(ideas_of(client, pid))[booking["id"]]["text"] == "Book appointments online"
    assert len(client.get(f"/api/projects/{pid}/ideas/{booking['id']}/versions").json()) == 2  # the SMS text is kept too

    # Several ideas at once, with a hint to a similar one.
    several = {"action": "new", "ideas": [{"title": "Dark mode", "text": "Dark mode"}, {"title": "Big fonts", "text": "Bigger fonts"}], "similar": [other["id"], 999]}
    answer(client, intent=[json.dumps(several)])
    change = typed(client, pid, "dark mode and bigger fonts")
    assert change["kind"] == "several" and len(change["summary"]["created"]) == 2 and change["summary"]["similar"] == [other["id"]]
    assert all(i["similar"] == [other["id"]] for i in change["ideas"])
    client.post(f"/api/projects/{pid}/ideas/changes/{change['id']}/undo")
    kept = by_id(ideas_of(client, pid, closed=True))
    assert all(kept[i]["status"] == "dismissed" and kept[i]["closed_reason"] == "undone" for i in change["summary"]["created"])

    # Merging two ideas: the second goes into the first and is kept as merged.
    merge = {"action": "merge", "ids": [booking["id"], other["id"]], "title": "Booking with reminders", "text": "Book online, reminded by e-mail"}
    answer(client, intent=[json.dumps(merge)])
    change = typed(client, pid, "leg Buchung und Erinnerung zusammen")
    assert change["kind"] == "merge" and change["summary"]["merged"] == [other["id"]]
    kept = by_id(ideas_of(client, pid, closed=True))
    assert kept[other["id"]]["status"] == "merged" and kept[other["id"]]["merged_into_id"] == booking["id"]
    assert kept[booking["id"]]["title"] == "Booking with reminders"
    client.post(f"/api/projects/{pid}/ideas/changes/{change['id']}/undo")
    kept = by_id(ideas_of(client, pid))
    assert kept[other["id"]]["status"] == "open" and kept[booking["id"]]["title"] == "Booking"

    # Nonsense, or an amend of an idea that does not exist: a new idea, nothing is overwritten.
    for reply in ("no idea", json.dumps({"action": "amend", "target": 999, "text": "x"})):
        answer(client, intent=[reply])
        change = typed(client, pid, "Something else entirely")
        assert change["kind"] == "new" and change["summary"]["fallback"] and change["ideas"][0]["text"] == "Something else entirely"


def test_an_unclear_input_becomes_a_question_and_the_answer_is_worked_in(client, project):
    pid = project["id"]
    question = {"action": "question", "question": "What should be exported?", "options": [{"label": "Invoices", "description": "PDF"}, "Customers"]}
    answer(client, intent=[json.dumps(question)])
    change = typed(client, pid, "export")
    assert change["kind"] == "question"
    (idea,) = change["ideas"]
    assert idea["question_status"] == "open" and idea["question_options"] == [{"label": "Invoices", "description": "PDF"}, {"label": "Customers", "description": ""}]
    assert client.post(f"/api/projects/{pid}/ideas/{idea['id']}/answer", json={"option_index": 7}).status_code == 400

    resolved = {"action": "amend", "target": idea["id"], "title": "Export invoices", "text": "Export invoices as PDF"}
    asked = answer(client, intent=[json.dumps(resolved)])
    resp = client.post(f"/api/projects/{pid}/ideas/{idea['id']}/answer", json={"option_index": 0, "text": "monthly"})
    assert resp.status_code == 200, resp.text
    assert "Their answer: Invoices — monthly" in asked["intent"][0].prompt
    (after,) = resp.json()["ideas"]
    assert after["text"] == "Export invoices as PDF" and after["question_status"] == "answered" and after["question_answer"] == "Invoices — monthly"
    assert client.post(f"/api/projects/{pid}/ideas/{idea['id']}/answer", json={"text": "again"}).status_code == 409

    # The model may also ask while sorting, rarely: once per idea at most.
    settled(client, pid)

    def sort(spec) -> str:
        return json.dumps({"ideas": [{"id": i, "topic": "Misc", "question": {"question": "Which app?", "options": ["Web", "Phone"]}} for i in ids_in(spec, "Sort these:")]})

    answer(client, sort=[sort])
    vague = add(client, pid, "make it nicer")
    asked_idea = by_id(settled(client, pid))[vague["id"]]
    assert asked_idea["question"] == "Which app?" and asked_idea["question_status"] == "open"
    assert client.delete(f"/api/projects/{pid}/ideas/{vague['id']}/question").json()["question_status"] == "withdrawn"


def test_a_persons_edit_is_kept_as_a_version_sorted_again_and_can_be_restored(client, project):
    pid = project["id"]
    answer(client)
    idea = add(client, pid, "First text", title="One")
    settled(client, pid)
    moved = client.patch(f"/api/projects/{pid}/ideas/{idea['id']}", json={"x": 5, "y": 6}).json()
    assert moved["topic_manual"] and (moved["x"], moved["y"]) == (5, 6)
    edited = client.patch(f"/api/projects/{pid}/ideas/{idea['id']}", json={"text": "Second text"}).json()
    assert edited["text"] == "Second text" and (edited["x"], edited["y"]) == (5, 6)
    (change,) = [c for c in client.get(f"/api/projects/{pid}/ideas/changes").json() if c["kind"] == "edit"]
    assert change["summary"]["before"] == {"title": "One", "text": "First text"}
    (version,) = client.get(f"/api/projects/{pid}/ideas/{idea['id']}/versions").json()
    restored = client.post(f"/api/projects/{pid}/ideas/{idea['id']}/versions/{version['id']}/restore").json()
    assert restored["text"] == "First text"
    assert [v["text"] for v in client.get(f"/api/projects/{pid}/ideas/{idea['id']}/versions").json()] == ["Second text", "First text"]


def test_viewers_read_the_ideas_and_members_change_them(client, project):
    pid = project["id"]
    answer(client)
    viewer, member = new_user(client, "vera"), new_user(client, "max")
    for user, role in ((viewer, "viewer"), (member, "member")):
        assert client.put(f"/api/projects/{pid}/members/{user['id']}", json={"role": role}).status_code == 200
    idea = add(client, pid, "Offline mode")
    settled(client, pid)
    edit = client.patch(f"/api/projects/{pid}/ideas/{idea['id']}", json={"text": "Offline mode, sync later"})
    change_id = client.get(f"/api/projects/{pid}/ideas/changes").json()[0]["id"]
    assert edit.status_code == 200

    sign_in(client, "vera", "vera-password")
    assert [i["id"] for i in ideas_of(client, pid)["ideas"]] == [idea["id"]]
    assert client.get(f"/api/projects/{pid}/ideas/changes").status_code == 200 and client.get(f"/api/projects/{pid}/ideas/{idea['id']}/versions").status_code == 200
    for method, path, body in (
        ("post", "", {"text": "x"}),
        ("post", "/input", {"text": "x"}),
        ("patch", f"/{idea['id']}", {"votes": 3}),
        ("delete", f"/{idea['id']}", None),
        ("post", f"/{idea['id']}/ticket", {}),
        ("post", f"/{idea['id']}/answer", {"text": "x"}),
        ("post", f"/changes/{change_id}/undo", None),
        ("post", "/sort", None),
        ("post", "/topics", {"name": "x"}),
    ):
        resp = getattr(client, method)(f"/api/projects/{pid}/ideas{path}", **({"json": body} if body is not None else {}))
        assert resp.status_code == 403, (method, path)

    sign_in(client, "max", "max-password")
    mine = add(client, pid, "Push notifications")
    assert mine["author"] == "Max"
    topic = client.post(f"/api/projects/{pid}/ideas/topics", json={"name": "Mobile"}).json()
    moved = client.patch(f"/api/projects/{pid}/ideas/{mine['id']}", json={"topic_id": topic["id"], "votes": 2}).json()
    assert (moved["topic_id"], moved["votes"], moved["topic_manual"]) == (topic["id"], 2, True)
    renamed = client.patch(f"/api/projects/{pid}/ideas/topics/{topic['id']}", json={"name": "Phone"}).json()
    assert renamed["name"] == "Phone" and renamed["manual"]
    assert client.post(f"/api/projects/{pid}/ideas/changes/{change_id}/undo").status_code == 200
    assert client.delete(f"/api/projects/{pid}/ideas/{idea['id']}").status_code == 204


def test_the_manager_reads_ideas_and_marks_what_went_into_tickets(client, project):
    pid = project["id"]
    answer(client)
    topic = client.post(f"/api/projects/{pid}/ideas/topics", json={"name": "Payment"}).json()
    card = add(client, pid, "Pay by card", topic_id=topic["id"])
    paypal = add(client, pid, "PayPal", topic_id=topic["id"])
    again = add(client, pid, "Card payments please")
    settled(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    runner = client.app_ref.state.runner

    lines = [
        "TOOL list_ideas {}",
        "TOOL get_idea " + json.dumps({"idea": card["id"]}),
        'TOOL create_tasks {"tickets": [{"title": "Payments"}]}',
        "TOOL take_ideas " + json.dumps({"ticket": 1, "ideas": [card["id"], paypal["id"]]}),
        "TOOL leave_out_ideas " + json.dumps({"ideas": [again["id"]], "reason": "duplicate", "duplicate_of": card["id"]}),
        "TOOL take_ideas " + json.dumps({"ticket": 1, "ideas": [card["id"]]}),
    ]
    chat(client, manager["id"], "Make tickets from the payment ideas\n" + "\n".join(lines))
    wait_for(lambda: idle(client, pid))
    assert "Ideas: 3 open (list_ideas)." in runner.specs[-1].prompt
    listed, detail, _created, taken, left, twice = tool_results(client, manager["id"])[-6:]
    assert listed.startswith("Ideas 1-3 of 3:") and "## Payment" in listed and f"i{card['id']} (no title)\n  Pay by card" in listed and "## No topic" in listed
    assert detail.startswith(f"i{card['id']} (no title) — open") and "Pay by card" in detail
    assert taken == "2 idea(s) taken into #1." and left == "1 idea(s) left out." and "no longer open" in twice

    data = ideas_of(client, pid)
    assert data["ideas"] == [] and [t["name"] for t in data["topics"]] == ["Payment"]  # emptied, but a person made it
    kept = by_id(ideas_of(client, pid, closed=True))
    assert (kept[card["id"]]["status"], kept[card["id"]]["task_number"]) == ("taken", 1)
    assert (kept[again["id"]]["status"], kept[again["id"]]["closed_reason"]) == ("dismissed", "duplicate")


def test_one_idea_becomes_a_ticket_and_the_project_moves_with_its_ideas(client, project):
    pid = project["id"]
    answer(client)
    topic = client.post(f"/api/projects/{pid}/ideas/topics", json={"name": "Look"}).json()
    one = add(client, pid, "Dark mode\nfor the night", topic_id=topic["id"])
    two = add(client, pid, "Bigger fonts", topic_id=topic["id"])
    settled(client, pid)
    client.patch(f"/api/projects/{pid}/ideas/{two['id']}", json={"text": "Much bigger fonts"})
    resp = client.post(f"/api/projects/{pid}/ideas/{one['id']}/ticket", json={"status": "backlog"})
    assert resp.status_code == 201, resp.text
    ticket = resp.json()["ticket"]
    assert ticket["title"] == "Dark mode" and ticket["description"] == "Dark mode\nfor the night" and ticket["status"] == "backlog"
    assert resp.json()["idea"]["status"] == "taken"
    assert [i["id"] for i in ideas_of(client, pid)["ideas"]] == [two["id"]]

    moved = bring_in(client, export(client, pid))
    assert moved.status_code == 201, moved.text
    other = moved.json()["project"]["id"]
    data = ideas_of(client, other, closed=True)
    assert [t["name"] for t in data["topics"]] == ["Look"]
    texts = {i["text"]: i for i in data["ideas"]}
    assert texts["Much bigger fonts"]["topic_id"] == data["topics"][0]["id"]
    assert [v["text"] for v in client.get(f"/api/projects/{other}/ideas/{texts['Much bigger fonts']['id']}/versions").json()] == ["Bigger fonts"]
    taken = texts["Dark mode\nfor the night"]
    assert taken["status"] == "taken" and taken["task_number"] == ticket["number"] and taken["task_id"] != ticket["id"]


def test_list_ideas_gives_every_text_whole_and_pages_by_whole_ideas(client, project, monkeypatch):
    from plainboard.db import sessionmaker

    pid = project["id"]
    answer(client)
    long_text = "Line one\n" + "x" * 300
    ids = [add(client, pid, text)["id"] for text in (long_text, "Short", "Also short")]
    settled(client, pid)
    monkeypatch.setattr(idea_service, "LIST_CHARS", 200)

    async def page(offset: int) -> str:
        async with sessionmaker()() as session:
            return await idea_service.manager_list(session, pid, topic=None, query=None, status="open", offset=offset)

    first = client.portal.call(page, 0)
    assert first.startswith("Ideas 1-1 of 3:") and "  " + "x" * 300 in first and first.endswith("(continue with offset=1: 2 more)")
    second = client.portal.call(page, 1)
    assert f"i{ids[1]} (no title)\n  Short" in second and f"i{ids[2]}" in second and "continue" not in second
    assert client.portal.call(page, 5) == "No ideas at offset 5 (3 in all)."
