"""Files of done tickets go after a project's attachment retention (services/retention.py, #97)."""

from datetime import timedelta


def _ticket(client, pid: int, title: str, *files: tuple[str, int], status: str = "done", done_days: float = 0) -> tuple[dict, list[dict]]:
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": title}).json()
    out = client.post(f"/api/tasks/{task['id']}/attachments", files=[("files", (name, b"x" * size, "application/octet-stream")) for name, size in files]).json()
    assert client.patch(f"/api/tasks/{task['id']}", json={"status": status}).status_code == 200
    if done_days:
        _age(client, pid, task["number"], done_days)
    return task, out


def _age(client, pid: int, number: int, days: float) -> None:
    from plainboard.db import sessionmaker
    from plainboard.models import TaskTransition, utcnow
    from sqlalchemy import update

    async def run():
        async with sessionmaker()() as s:
            await s.execute(update(TaskTransition).where(TaskTransition.project_id == pid, TaskTransition.task_number == number).values(at=utcnow() - timedelta(days=days)))
            await s.commit()

    client.portal.call(run)


def _purge(client) -> dict:
    from plainboard.db import sessionmaker
    from plainboard.models import utcnow
    from plainboard.services import retention

    async def run():
        async with sessionmaker()() as s:
            return {pid: sorted(d.attachment.id for d in due) for pid, due in (await retention.purge(s, utcnow())).items()}

    return client.portal.call(run)


def _file(attachment: dict):
    from plainboard.config import get_config
    from plainboard.db import sessionmaker
    from plainboard.models import Attachment

    async def run():
        async with sessionmaker()() as s:
            return get_config().uploads_dir / (await s.get(Attachment, attachment["id"])).storage_name

    return _file.client.portal.call(run)


def test_default_deletes_nothing(client, project):
    pid = project["id"]
    assert project["attachment_retention_days"] == 0
    _, files = _ticket(client, pid, "Old", ("a.png", 10), done_days=4000)
    assert _purge(client) == {}
    _file.client = client
    assert _file(files[0]).is_file()
    preview = client.get(f"/api/projects/{pid}/attachment-retention", params={"days": 0}).json()
    assert preview["now"] == {"count": 0, "bytes": 0}


def test_only_old_done_tickets_lose_their_files_and_keep_the_entry(client, project):
    from plainboard.db import sessionmaker
    from plainboard.models import Decision, FlowEvent, InboxItem, Message
    from plainboard.services import agents as agent_service
    from sqlalchemy import select

    _file.client = client
    pid = project["id"]
    old, (gone, big) = _ticket(client, pid, "Old", ("a.png", 100), ("big.bin", 3000), done_days=40)
    _, (young,) = _ticket(client, pid, "Young", ("b.png", 50), done_days=10)
    _, (soon,) = _ticket(client, pid, "Soon", ("s.png", 70), done_days=25)
    _, (open_one,) = _ticket(client, pid, "Open", ("c.png", 60), status="review")
    _age(client, pid, 4, 400)  # long in review: open all the same
    _, (asked, shown, noted, named) = _ticket(client, pid, "Used", ("d.png", 1), ("e.png", 2), ("f.png", 3), ("g.png", 4), done_days=40)
    open_task, _ = _ticket(client, pid, "Still open", status="todo")

    manager = next(a for a in client.get(f"/api/projects/{pid}/agents").json() if a["is_manager"])

    async def use():
        async with sessionmaker()() as s:
            s.add(Decision(project_id=pid, task_id=open_task["id"], question="Which?", options=[{"label": "A", "description": "", "attachment_ids": [asked["id"]]}]))
            conv = await agent_service.conversation_for(s, manager["id"])
            s.add(Message(conversation_id=conv.id, role="assistant", kind="files", content="", meta={"attachments": [{"id": shown["id"], "filename": "e.png"}]}))
            await s.commit()

    client.portal.call(use)
    assert client.post(f"/api/projects/{pid}/notes", json={"title": "Look", "content": f"See {_file(noted).name}"}).status_code in (200, 201)
    assert client.post(f"/api/tasks/{open_task['id']}/comments", json={"body": f"Like /uploads/{_file(named).name}"}).status_code in (200, 201)

    # The preview: what goes at once and within the next 30 days; 30 days in done, all sizes.
    preview = client.get(f"/api/projects/{pid}/attachment-retention", params={"days": 30}).json()
    assert preview["now"] == {"count": 2, "bytes": 3100}
    assert preview["soon"] == {"count": 2, "bytes": 120}
    assert preview["soon_days"] == 30
    sized = client.get(f"/api/projects/{pid}/attachment-retention", params={"days": 30, "min_mb": 1}).json()
    assert sized["now"]["count"] == 0
    assert _purge(client) == {}  # the preview saves nothing

    assert client.patch(f"/api/projects/{pid}", json={"attachment_retention_days": 30}).json()["attachment_retention_days"] == 30
    paths = {a["id"]: _file(a) for a in (gone, big, young, soon, open_one, asked, shown, noted, named)}
    assert _purge(client) == {pid: sorted([gone["id"], big["id"]])}
    assert not paths[gone["id"]].exists() and not paths[big["id"]].exists()
    assert all(paths[a["id"]].is_file() for a in (young, soon, open_one, asked, shown, noted, named))
    assert _purge(client) == {}  # once

    entries = {a["id"]: a for a in client.get(f"/api/tasks/{old['id']}").json()["attachments"]}
    assert entries[gone["id"]]["filename"] == "a.png" and entries[gone["id"]]["size"] == 100
    assert entries[gone["id"]]["purged_at"] and entries[gone["id"]]["purged_after_days"] == 30
    resp = client.get(f"/api/attachments/{gone['id']}")
    assert resp.status_code == 410 and "retention 30 days" in resp.text

    async def logged():
        async with sessionmaker()() as s:
            events = list(await s.scalars(select(FlowEvent).where(FlowEvent.kind == "attachment.purged")))
            return [(e.data["filename"], e.data["size"], e.data["task"]) for e in events]

    assert sorted(client.portal.call(logged)) == [("a.png", 100, old["number"]), ("big.bin", 3000, old["number"])]

    # The watcher's daily run tells the manager in one line.
    _age(client, pid, 3, 40)
    client.portal.call(lambda: client.app_ref.state.cleanup._retention(__import__("plainboard.models").models.utcnow()))

    async def told():
        async with sessionmaker()() as s:
            return [i.text for i in await s.scalars(select(InboxItem).where(InboxItem.agent_id == manager["id"]))]

    assert any("1 attachment (70 B)" in t for t in client.portal.call(told))


def test_only_the_status_done_counts_and_reopening_starts_the_time_again(client, project):
    _file.client = client
    pid = project["id"]
    assert client.patch(f"/api/projects/{pid}", json={"attachment_retention_days": 30}).status_code == 200
    archived, (kept_archived,) = _ticket(client, pid, "Archived in review", ("a.png", 10), status="review", done_days=400)
    assert client.patch(f"/api/tasks/{archived['id']}", json={"archived": True}).status_code == 200
    reopened, (kept_reopened,) = _ticket(client, pid, "Reopened", ("b.png", 20), done_days=400)
    assert client.patch(f"/api/tasks/{reopened['id']}", json={"status": "todo"}).status_code == 200
    assert _purge(client) == {}  # not done now: untouched, however long it was done before
    assert client.patch(f"/api/tasks/{reopened['id']}", json={"status": "done"}).status_code == 200
    assert _purge(client) == {}  # done again just now: the 30 days start again
    assert client.get(f"/api/projects/{pid}/attachment-retention", params={"days": 30}).json()["soon"] == {"count": 1, "bytes": 20}
    assert _file(kept_archived).is_file() and _file(kept_reopened).is_file()
    _age(client, pid, reopened["number"], 31)
    assert _purge(client) == {pid: [kept_reopened["id"]]}
