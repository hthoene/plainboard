"""Context management: session reuse, compression with a cheap model, fresh-session bootstrapping.

Each agent has one endless conversation. Turns resume the provider session (prompt caching, full
tool history). When the measured context exceeds the agent's limit, older messages are summarized
with the agent's summary model; the provider session is dropped and the next turn starts fresh with
system prompt + summary + the last few messages verbatim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from plainboard_protocol import CompletionSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..events import bus
from ..models import Agent, Conversation, Message, Project
from ..schemas import MessageOut
from ..services import agents as agent_service
from ..services import notes as notes_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services.util import truncate
from . import prompts
from .runner_client import RunnerClient

log = logging.getLogger(__name__)

CHUNK_CHARS = 200_000
# Compression only pays off once the conversation itself has grown; the fixed base context
# (system prompt, tool definitions, memory) cannot be compressed.
MIN_GROWTH_TOKENS = 4000


def should_compress(context_tokens: int, base_tokens: int, limit: int) -> bool:
    return context_tokens >= limit and context_tokens - base_tokens >= MIN_GROWTH_TOKENS


@dataclass
class Turn:
    system_prompt: str
    prompt: str
    resume_session_id: str | None
    briefing_changes: list[str] = field(default_factory=list)  # sections sent as an update this turn


def workspace_path(project: Project, agent: Agent) -> str:
    root = get_config().workspaces_root
    if project.workspace_path:  # shared folder, e.g. a bind-mounted local project directory
        return f"{root}/{project.workspace_path}"
    return f"{root}/{project.slug}/{agent.slug}"


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def render_message(message: Message) -> str:
    if message.kind == "tool":
        meta = message.meta or {}
        args = truncate(str(meta.get("input", "")), 300)
        result = truncate(str(meta.get("result", "")), 400)
        flag = " (error)" if meta.get("is_error") else ""
        return f"[tool] {meta.get('name', message.content)} {args} -> {result}{flag}"
    inbox = (message.meta or {}).get("inbox")
    prefix = ("[updates] " + " | ".join(inbox) + "\n") if inbox else ""
    return f"{prefix}[{message.role}] {message.content}"


class ContextManager:
    def __init__(self, runner: RunnerClient):
        self.runner = runner

    async def _uncovered(self, session: AsyncSession, conv: Conversation, *, before_id: int | None, text_only: bool) -> list[Message]:
        q = select(Message).where(Message.conversation_id == conv.id, Message.id > conv.summary_until_message_id)
        q = q.where(Message.kind == "text") if text_only else q.where(Message.kind.in_(("text", "tool")))
        if before_id is not None:
            q = q.where(Message.id < before_id)
        return list((await session.scalars(q.order_by(Message.id))).all())

    async def build_turn(
        self, session: AsyncSession, *, project: Project, agent: Agent, conv: Conversation, text: str, message_id: int
    ) -> Turn:
        team = await agent_service.list_for_project(session, project.id)
        pinned = await notes_service.pinned(session, project.id)
        workspace = workspace_path(project, agent)
        project_repos = await repo_service.list_for_project(session, project.id)
        repos_by_agent = {a.id: [r.name for r in repo_service.accessible(a, project_repos)] for a in team} if project_repos else None

        def sections() -> dict[str, str]:
            return prompts.system_prompt_sections(
                project,
                agent,
                team,
                pinned,
                workspace,
                memory=conv.summary,
                my_repos=repo_service.accessible(agent, project_repos),
                project_repos=project_repos,
                repos_by_agent=repos_by_agent,
            )

        if conv.provider_session_id and not conv.system_prompt:
            # Session from before prompts were frozen: adopt the current briefing and keep the session.
            current = sections()
            conv.system_prompt, conv.prompt_sections = prompts.join_sections(current), current
            await session.commit()
        if conv.provider_session_id and conv.system_prompt:
            # Keep the frozen system prompt (prompt cache stays valid); send only what changed.
            current = sections()
            update, changed = prompts.briefing_update(conv.prompt_sections or {}, current)
            if changed:
                conv.prompt_sections = current
                await session.commit()
            return Turn(conv.system_prompt, f"{update}\n\n{text}" if update else text, conv.provider_session_id, changed)

        uncovered = await self._uncovered(session, conv, before_id=message_id, text_only=True)
        if len(uncovered) > agent.keep_recent_messages:
            await self.compress(session, agent, conv, before_id=message_id)
            uncovered = await self._uncovered(session, conv, before_id=message_id, text_only=True)
        recent = [(m.role, m.content) for m in uncovered[-agent.keep_recent_messages :]] if agent.keep_recent_messages else []
        # New session (first turn or after compression): fresh briefing with the current state.
        current = sections()
        conv.system_prompt = prompts.join_sections(current)
        conv.prompt_sections = current
        await session.commit()
        return Turn(conv.system_prompt, prompts.fresh_session_prompt(recent, text), None)

    async def maybe_compress(self, session: AsyncSession, agent: Agent, conv: Conversation) -> bool:
        if not should_compress(conv.context_tokens, conv.session_base_tokens, agent.context_limit_tokens):
            return False
        return await self.compress(session, agent, conv)

    async def compress(self, session: AsyncSession, agent: Agent, conv: Conversation, *, before_id: int | None = None) -> bool:
        """Summarize everything except the last ``keep_recent_messages`` text messages and drop the session."""
        messages = await self._uncovered(session, conv, before_id=before_id, text_only=False)
        texts = [m for m in messages if m.kind == "text"]
        keep = agent.keep_recent_messages
        cutoff = texts[-keep].id if keep and len(texts) > keep else None
        to_summarize = [m for m in messages if cutoff is None or m.id < cutoff]
        if not to_summarize:
            conv.provider_session_id = None
            await session.commit()
            return False

        values = await settings_service.get_all(session)
        credentials = settings_service.credentials(values)
        summary = conv.summary
        cost = 0.0
        tokens_in = tokens_out = 0
        for chunk in self._chunks([render_message(m) for m in to_summarize]):
            result = await self.runner.complete(
                CompletionSpec(
                    provider=agent.provider,
                    model=agent.summary_model,
                    system_prompt=prompts.SUMMARY_SYSTEM,
                    prompt=prompts.summary_prompt(summary, chunk),
                    credentials=credentials,
                )
            )
            summary = result.text.strip()
            cost += result.cost_usd
            tokens_in += result.input_tokens
            tokens_out += result.output_tokens

        tokens_before = conv.context_tokens
        recent_chars = sum(min(len(m.content), prompts.RECENT_MESSAGE_CHARS) for m in texts[-keep:]) if keep else 0
        conv.summary = summary
        conv.summary_until_message_id = to_summarize[-1].id
        conv.provider_session_id = None
        conv.compressions += 1
        conv.context_tokens = estimate_tokens(summary or "") + recent_chars // 4
        conv.total_cost_usd += cost
        conv.total_input_tokens += tokens_in
        conv.total_output_tokens += tokens_out
        marker = Message(
            conversation_id=conv.id,
            role="system",
            kind="summary",
            content=summary or "",
            meta={"tokens_before": tokens_before, "covered_until": conv.summary_until_message_id, "model": agent.summary_model},
        )
        session.add(marker)
        await session.commit()
        bus.publish(agent.project_id, "message.created", MessageOut.model_validate(marker))
        await agent_service.publish(session, agent)
        log.info("Compressed conversation of agent %s (%s tokens before)", agent.id, tokens_before)
        return True

    @staticmethod
    def _chunks(lines: list[str]) -> list[str]:
        chunks, current, size = [], [], 0
        for line in lines:
            if current and size + len(line) > CHUNK_CHARS:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(line[:CHUNK_CHARS])
            size += len(line)
        if current:
            chunks.append("\n".join(current))
        return chunks
