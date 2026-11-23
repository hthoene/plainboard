"""A project's ideas: collect, sort, rearrange, undo (services/ideas.py, orchestration/ideas.py).
Members change them, viewers read (services/access.py). Tickets come from them through the manager."""

from __future__ import annotations

from fastapi import APIRouter

from ..schemas import IdeaAnswer, IdeaChangeOut, IdeaCreate, IdeaInput, IdeaOut, IdeasOut, IdeaTicketIn, IdeaTicketOut, IdeaTopicIn, IdeaTopicOut, IdeaUpdate, IdeaVersionOut
from ..services import board as board_service
from ..services import ideas as idea_service
from ..services import projects as project_service
from .deps import Runs, Session, Who

router = APIRouter(prefix="/api/projects/{project_id}/ideas", tags=["ideas"])


async def _out(session, idea) -> IdeaOut:  # noqa: ANN001
    (out,) = await idea_service.outs(session, [idea])
    return out


@router.get("", response_model=IdeasOut)
async def list_ideas(project_id: int, session: Session, runs: Runs, closed: bool = False) -> IdeasOut:
    """The open ideas and their topics; ``closed``: also the ones taken into tickets, left out or merged."""
    await project_service.get(session, project_id)
    topics = [IdeaTopicOut.model_validate(t) for t in await idea_service.topics(session, project_id)]
    ideas = await idea_service.outs(session, await idea_service.listed(session, project_id, closed=closed))
    return IdeasOut(topics=topics, ideas=ideas, sorting=runs.ideas.sorting(project_id))


@router.get("/questions", response_model=list[IdeaOut])
async def open_questions(project_id: int, session: Session) -> list[IdeaOut]:
    """The model's open questions about ideas, for "for you"."""
    return await idea_service.outs(session, await idea_service.open_questions(session, project_id))


@router.post("", response_model=IdeaOut, status_code=201)
async def create_idea(project_id: int, body: IdeaCreate, session: Session, runs: Runs, who: Who) -> IdeaOut:
    """An idea exactly as typed (the field of the board uses ``/input``)."""
    await project_service.get(session, project_id)
    idea = await idea_service.create(session, project_id, body, who.name)
    runs.ideas.sort_soon(project_id)
    return await _out(session, idea)


@router.post("/input", response_model=IdeaChangeOut, status_code=201)
async def read_input(project_id: int, body: IdeaInput, session: Session, runs: Runs, who: Who) -> IdeaChangeOut:
    """What the person typed, read by the model: what it did, with the ideas it touched and an id to undo it."""
    project = await project_service.get(session, project_id)
    change = await runs.ideas.read_input(session, project, body.text, who.name)
    return await idea_service.change_out(session, change, with_ideas=True)


@router.post("/sort", status_code=202)
async def rearrange(project_id: int, session: Session, runs: Runs) -> dict:
    """Rearrange all topics now (the button); comes as an ``idea_change.upserted`` of kind reorder, if anything changed."""
    await project_service.get(session, project_id)
    runs.ideas.sort_soon(project_id, reorder=True)
    return {"sorting": True}


@router.get("/changes", response_model=list[IdeaChangeOut])
async def list_changes(project_id: int, session: Session, limit: int = 20) -> list[IdeaChangeOut]:
    return [await idea_service.change_out(session, c) for c in await idea_service.changes(session, project_id, min(max(limit, 1), 100))]


@router.post("/changes/{change_id}/undo", response_model=IdeaChangeOut)
async def undo_change(project_id: int, change_id: int, session: Session) -> IdeaChangeOut:
    change = await idea_service.undo(session, await idea_service.get_change(session, project_id, change_id))
    return await idea_service.change_out(session, change, with_ideas=True)


@router.post("/topics", response_model=IdeaTopicOut, status_code=201)
async def create_topic(project_id: int, body: IdeaTopicIn, session: Session) -> IdeaTopicOut:
    await project_service.get(session, project_id)
    return IdeaTopicOut.model_validate(await idea_service.create_topic(session, project_id, body))


@router.patch("/topics/{topic_id}", response_model=IdeaTopicOut)
async def update_topic(project_id: int, topic_id: int, body: IdeaTopicIn, session: Session) -> IdeaTopicOut:
    topic = await idea_service.get_topic(session, project_id, topic_id)
    return IdeaTopicOut.model_validate(await idea_service.update_topic(session, topic, body))


@router.delete("/topics/{topic_id}", status_code=204)
async def delete_topic(project_id: int, topic_id: int, session: Session) -> None:
    await idea_service.remove_topic(session, await idea_service.get_topic(session, project_id, topic_id))


@router.patch("/{idea_id}", response_model=IdeaOut)
async def update_idea(project_id: int, idea_id: int, body: IdeaUpdate, session: Session, runs: Runs, who: Who) -> IdeaOut:
    """A new title or text is kept as a version before (undo: the ``idea_change.upserted`` of kind edit) and
    has the idea sorted again; a move pins it."""
    idea = await idea_service.get(session, project_id, idea_id)
    idea, change = await idea_service.update(session, idea, body.model_dump(exclude_unset=True), who.name)
    if change is not None:
        runs.ideas.sort_soon(project_id)
    return await _out(session, idea)


@router.delete("/{idea_id}", status_code=204)
async def delete_idea(project_id: int, idea_id: int, session: Session) -> None:
    await idea_service.remove(session, await idea_service.get(session, project_id, idea_id))


@router.get("/{idea_id}/versions", response_model=list[IdeaVersionOut])
async def list_versions(project_id: int, idea_id: int, session: Session) -> list[IdeaVersionOut]:
    """Its earlier titles and texts, newest first."""
    idea = await idea_service.get(session, project_id, idea_id)
    return [IdeaVersionOut.model_validate(v) for v in await idea_service.versions(session, idea)]


@router.post("/{idea_id}/versions/{version_id}/restore", response_model=IdeaOut)
async def restore_version(project_id: int, idea_id: int, version_id: int, session: Session, runs: Runs, who: Who) -> IdeaOut:
    idea = await idea_service.get(session, project_id, idea_id)
    idea, change = await idea_service.restore_version(session, idea, version_id, who.name)
    if change is not None:
        runs.ideas.sort_soon(project_id)
    return await _out(session, idea)


@router.post("/{idea_id}/answer", response_model=IdeaChangeOut)
async def answer_question(project_id: int, idea_id: int, body: IdeaAnswer, session: Session, runs: Runs, who: Who) -> IdeaChangeOut:
    """The answer to the model's question about the idea; the model works it in."""
    project = await project_service.get(session, project_id)
    idea = await idea_service.get(session, project_id, idea_id)
    change = await runs.ideas.answer(session, project, idea, body.option_index, body.text, who.name)
    return await idea_service.change_out(session, change, with_ideas=True)


@router.delete("/{idea_id}/question", response_model=IdeaOut)
async def withdraw_question(project_id: int, idea_id: int, session: Session) -> IdeaOut:
    """Dismiss the model's question unanswered."""
    return await _out(session, await idea_service.withdraw_question(session, await idea_service.get(session, project_id, idea_id)))


@router.post("/{idea_id}/ticket", response_model=IdeaTicketOut, status_code=201)
async def idea_to_ticket(project_id: int, idea_id: int, body: IdeaTicketIn, session: Session) -> IdeaTicketOut:
    """One idea straight into a ticket."""
    idea = await idea_service.get(session, project_id, idea_id)
    task = await idea_service.take_one(session, idea, body)
    (ticket,) = await board_service.task_outs(session, [task])
    return IdeaTicketOut(ticket=ticket, idea=await _out(session, idea))
