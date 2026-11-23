"""The project's ideas, for the manager: read them, and mark which went into a ticket or were left out."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..services import board as board_service
from ..services import ideas as idea_service
from .registry import MANAGER, ToolContext, tool


class ListIdeasArgs(BaseModel):
    topic: str | None = Field(default=None, description="Part of a topic's name")
    query: str | None = Field(default=None, description="Text search")
    status: Literal["open", "taken", "dismissed", "merged", "all"] = "open"
    offset: int = Field(default=0, ge=0, description="Start of the next page, as the last one says")


class IdeaRef(BaseModel):
    idea: int = Field(description="Idea id (i12 -> 12)")


class TakeIdeasArgs(BaseModel):
    ticket: int = Field(description="Ticket number the ideas went into")
    ideas: list[int] = Field(min_length=1, max_length=200)


class LeaveOutArgs(BaseModel):
    ideas: list[int] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=300)
    duplicate_of: int | None = Field(default=None, description="The idea they duplicate")


@tool("list_ideas", "The project's ideas in full (whole text) by topic, with similar ones and duplicates.", ListIdeasArgs, roles=(MANAGER,))
async def list_ideas(ctx: ToolContext, args: ListIdeasArgs) -> str:
    return await idea_service.manager_list(ctx.session, ctx.project.id, topic=args.topic, query=args.query, status=args.status, offset=args.offset)


@tool("get_idea", "One idea in full, with its earlier versions.", IdeaRef, roles=(MANAGER,))
async def get_idea(ctx: ToolContext, args: IdeaRef) -> str:
    return await idea_service.manager_detail(ctx.session, await idea_service.get(ctx.session, ctx.project.id, args.idea))


@tool("take_ideas", "Mark ideas as taken into a ticket: they leave the ideas, kept with the ticket.", TakeIdeasArgs, roles=(MANAGER,))
async def take_ideas(ctx: ToolContext, args: TakeIdeasArgs) -> str:
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.ticket)
    ideas = await idea_service.take(ctx.session, ctx.project.id, task, args.ideas)
    return f"{len(ideas)} idea(s) taken into #{task.number}."


@tool("leave_out_ideas", "Mark ideas as left out on purpose (duplicate, covered, dropped), with the reason.", LeaveOutArgs, roles=(MANAGER,))
async def leave_out_ideas(ctx: ToolContext, args: LeaveOutArgs) -> str:
    ideas = await idea_service.leave_out(ctx.session, ctx.project.id, args.ideas, args.reason, args.duplicate_of)
    return f"{len(ideas)} idea(s) left out."
