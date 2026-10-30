"""Prompt construction. System prompts are kept stable (cache-friendly) and short (token-friendly);
volatile data (time, inbox) goes into the user turn."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..models import Agent, Assignment, Attachment, Comment, Decision, DecisionStatus, Note, Project, Repository, Task
from ..services.util import truncate

MANAGER_GUIDE = """\
You coordinate the project through your PlainBoard tools and delegate the real work.

Do small things yourself when delegating would cost more: quick checks (branch state, logs, reading
code) and tiny fixes without design decisions (a typo, a version bump, a few lines). Delegate anything
larger, specialist work and long investigations.

How work flows:
- Tickets on the Kanban board describe what the user wants. Tickets are never assigned to agents.
- When a ticket is handed to you (or the user asks), plan it: split it into clear, self-contained
  assignments and delegate each one with `delegate` to the agent whose area fits best. One ticket may
  need several agents; independent assignments can run in parallel, dependent ones go one after another.
- An assignee only sees the assignment text, so include goal, relevant context, constraints and the
  expected result. Never just say "do ticket #5".
- Effort: omit it normally (the agent's own setting applies). Override only when an assignment is
  clearly far from the agent's usual difficulty (e.g. "high" for a hard bug on a low-effort agent):
  every change of effort invalidates the agent's prompt cache, which costs tokens.
- Finished assignment reports arrive as updates in your next turn. Review them, delegate follow-ups if
  needed, then move the ticket (update_task: review/done) and leave a short comment for the user.
- The user decides what is theirs to decide (design direction, trade-offs, scope). Have proposals
  produced and attached first if useful, then call request_decision with clear options and their
  attachment ids. Then stop that work: the answer arrives as an update. If the user answers you in
  chat instead, record it with resolve_decision.
- Missing a role? Create a focused agent (create_agent) with a narrow area of responsibility so its
  context stays small. Prefer cheaper models for simple roles.
- `ask_agent` asks a teammate and waits for the answer; `message_agent` sends without waiting.
- Notes are the project's long-term memory (decisions, conventions, where things are). Keep them
  clean: one note per topic - search (list_notes) and update_note instead of creating a new one;
  rewrite outdated content, merge overlapping notes, delete obsolete ones. Keep notes short and
  factual. Pin only what every agent must always know (pinned notes cost tokens on every turn).
- Repositories: each agent works on its own clone of the repositories it has access to. Grant or
  revoke access with set_agent_repositories; add/remove project repositories as needed.
- Schedules run prompts later or repeatedly (cron), e.g. reminders, reports, recurring checks.
- The environment is your job, never the user's. When a project needs a toolchain or package, make sure
  it gets installed, and record the commands with update_project (setup_script) so they survive
  container rebuilds, including what agents report having installed. The script runs automatically
  before the next agent run, and again after every change or container restart, so it must be safe to
  repeat. No secrets.

Be brief with the user. Summarize tool results instead of repeating them.
The user only sees your chat text, not your tool calls or reasoning. Answer every question from the
user in your final chat message, even if you also changed a ticket because of it."""

WORKER_GUIDE = """\
How you work:
- You receive assignments, mostly from the manager. Each one is self-contained; do exactly that, autonomously.
- Your final message is your report and goes back to whoever assigned the work: state what you did,
  where (files, branch, PR link), how to verify, and open issues. Keep it short.
- You can read tickets (get_task) and comment on them. Ticket status is managed by the manager.
- Ask teammates with ask_agent (waits for the answer) or message_agent (does not wait).
- Store durable knowledge in notes. Pin only what everyone must always know.
- If you installed a tool the project will keep needing, name the commands in your report.
- Files for the user's review (e.g. design proposals): attach them with attach_file and name the
  attachment ids in your report. Attachments marked "for the user's review" are not input for your work."""

ENV_GUIDE = """\
Environment: Linux container with full shell access and passwordless sudo. If a tool is missing, install
it yourself and carry on; never report it as a blocker or ask the user to install it. Your home directory
is permanent and shared by all agents (put PATH changes in ~/.profile); system packages are lost when the
container is rebuilt. Other agents may be working in this container right now: never kill processes you did
not start, and do not upgrade or remove installed toolchains or packages. Add what you need alongside them."""

GIT_GUIDE = """\
git and the GitHub CLI (gh) are authenticated when a GitHub token is configured. For repository
changes: work on a feature branch, commit with clear messages, push, and open a pull request with gh
unless told otherwise. Repositories are fetched before each of your runs, so origin/* is current;
your checkout only moves forward if it has no local changes. Start new branches from origin/<default>."""

PINNED_BUDGET_CHARS = 4000  # ~1k tokens for all pinned notes together

SUMMARY_SYSTEM = """\
You compress an AI agent's conversation history into a working memory for the same agent.
Write a dense, factual summary in English with these sections (omit empty ones):
## Goals & context
## Decisions & conventions
## Work done (files, branches, PRs, tickets, assignments - keep identifiers exact)
## Current state & open items
## Important facts about the user/team
Keep every identifier, path, URL, number and name exactly. Drop chit-chat and anything obsolete.
Merge the previous summary with the new transcript; the result replaces both."""


def team_roster(agents: list[Agent], me: Agent, repos_by_agent: dict[int, list[str]] | None = None) -> str:
    lines = []
    for agent in agents:
        if not agent.enabled:
            continue
        marker = " (you)" if agent.id == me.id else ""
        area = truncate(agent.description, 160)
        line = f"- {agent.name} [{agent.slug}]{marker}: {agent.role or 'agent'}" + (f" — {area}" if area else "")
        if repos_by_agent is not None and not agent.is_manager:
            line += f" · repos: {', '.join(repos_by_agent.get(agent.id, [])) or 'none'}"
        lines.append(line)
    return "\n".join(lines)


def pinned_notes_block(notes: list[Note]) -> str:
    """Pinned notes within a fixed budget; beyond it only titles are listed (and the overflow is flagged)."""
    if not notes:
        return ""
    parts, used, overflow = [], 0, []
    for note in notes:
        body = note.content.strip()
        if used + len(body) <= PINNED_BUDGET_CHARS:
            parts.append(f"### {note.title} (note {note.id})\n{body}")
            used += len(body)
        else:
            overflow.append(f"{note.title} (note {note.id})")
    block = "\n\n## Pinned notes\n" + "\n\n".join(parts)
    if overflow:
        block += (
            "\n\nPinned notes exceed their budget; not shown (read with get_note): "
            + "; ".join(overflow)
            + ". Condense or unpin notes to fix this."
        )
    return block


def repos_block(repos: list[Repository], workspace: str) -> str:
    lines = []
    for repo in repos:
        branch = f", branch {repo.default_branch}" if repo.default_branch else ""
        about = f" — {truncate(repo.description, 140)}" if repo.description else ""
        lines.append(f"- {repo.name}: {workspace}/{repo.name} ({repo.url}{branch}){about}")
    return "\n".join(lines)


def system_prompt(
    project: Project,
    agent: Agent,
    team: list[Agent],
    pinned: list[Note],
    workspace: str,
    memory: str | None = None,
    *,
    my_repos: list[Repository] | None = None,
    project_repos: list[Repository] | None = None,
    repos_by_agent: dict[int, list[str]] | None = None,
) -> str:
    return join_sections(
        system_prompt_sections(
            project, agent, team, pinned, workspace, memory, my_repos=my_repos, project_repos=project_repos, repos_by_agent=repos_by_agent
        )
    )


# Human-readable names, used when changed sections are sent as a briefing update.
SECTION_TITLES = {
    "identity": "Your role and the project",
    "guide": "How you work",
    "environment": "Environment",
    "repositories": "Your repositories",
    "project_repositories": "Project repositories",
    "instructions": "Instructions",
    "team": "Team",
    "pinned_notes": "Pinned notes",
    "memory": "Memory",
}


def system_prompt_sections(
    project: Project,
    agent: Agent,
    team: list[Agent],
    pinned: list[Note],
    workspace: str,
    memory: str | None = None,
    *,
    my_repos: list[Repository] | None = None,
    project_repos: list[Repository] | None = None,
    repos_by_agent: dict[int, list[str]] | None = None,
) -> dict[str, str]:
    """The system prompt as named sections (ordered), so changes can be detected per section."""
    my_repos = my_repos or []
    sections: dict[str, str] = {}
    identity = [f"You are {agent.name}, {agent.role or 'an agent'} in the project \"{project.name}\" (PlainBoard)."]
    if project.description:
        identity.append(f"Project: {truncate(project.description, 1500)}")
    if agent.description:
        identity.append(f"Your area of responsibility: {agent.description}")
    sections["identity"] = "\n\n".join(identity)
    sections["guide"] = MANAGER_GUIDE if agent.is_manager else WORKER_GUIDE
    if project.workspace_path:
        where = (
            f"Your workspace: {workspace} — a folder SHARED with the other agents (and visible to the user). "
            "Other agents may work here at the same time: keep changes focused and avoid editing files others are working on."
        )
    else:
        where = f"Your workspace: {workspace}."
    env = [ENV_GUIDE, GIT_GUIDE] if my_repos else [ENV_GUIDE]
    sections["environment"] = "\n".join(env) + f"\n{where} Ticket attachments are readable under /uploads."
    if my_repos:
        sections["repositories"] = f"## Repositories (cloned in your workspace)\n{repos_block(my_repos, workspace)}"
    if agent.is_manager and project_repos:
        lines = [f"- {r.name}: {r.url}" + (f" — {truncate(r.description, 140)}" if r.description else "") for r in project_repos]
        sections["project_repositories"] = "## Project repositories\n" + "\n".join(lines)
    if agent.instructions:
        sections["instructions"] = f"## Instructions\n{agent.instructions}"
    sections["team"] = f"## Team\n{team_roster(team, agent, repos_by_agent)}"
    if pinned:
        sections["pinned_notes"] = pinned_notes_block(pinned).strip()
    if memory:
        sections["memory"] = f"## Memory (summary of your earlier conversation)\n{memory}"
    return sections


def join_sections(sections: dict[str, str]) -> str:
    return "\n\n".join(sections.values())


def briefing_update(old: dict[str, str], new: dict[str, str]) -> tuple[str, list[str]]:
    """Text describing changed/removed sections (empty if nothing changed) and their keys."""
    changed = [key for key, value in new.items() if old.get(key) != value]
    removed = [key for key in old if key not in new]
    if not changed and not removed:
        return "", []
    parts = ["[Briefing update] Parts of your briefing changed since this session started. These replace the earlier versions:"]
    for key in changed:
        parts.append(f"### {SECTION_TITLES.get(key, key)}\n{new[key]}")
    for key in removed:
        parts.append(f"### {SECTION_TITLES.get(key, key)}\n(removed)")
    return "\n\n".join(parts), changed + removed


def now_line(tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(UTC).astimezone(tz).strftime("Current time: %Y-%m-%d %H:%M %Z (%A)")


def ticket_brief(
    task: Task, attachments: list[Attachment], comments: list[Comment], names: dict[int, str], runner_path, decisions: list[Decision] | None = None
) -> str:
    lines = [f"Ticket #{task.number}: {task.title}", f"Status: {task.status} | Priority: {task.priority}" + (f" | Labels: {', '.join(task.labels)}" if task.labels else "")]
    if task.description:
        lines += ["", task.description.strip()]
    decisions = decisions or []
    for_user = {i for d in decisions for i in [*d.attachment_ids, *(a for o in d.options for a in o["attachment_ids"])]}
    if attachments:
        lines += ["", "Attachments:"] + [f"- {a.id} {a.filename}: {runner_path(a)}" + (" (for the user's review)" if a.id in for_user else "") for a in attachments]
    if open_decisions := [d for d in decisions if d.status == DecisionStatus.OPEN]:
        lines += ["", "Waiting for the user's decision:"]
        for d in open_decisions:
            options = "; ".join(f"{i}. {o['label']}" for i, o in enumerate(d.options, 1))
            lines.append(f"- Decision {d.id}: {d.question}" + (f" ({options})" if options else ""))
    if comments:
        lines += ["", "Recent comments:"]
        for c in comments:
            author = names.get(c.author_agent_id or 0, c.author_kind) if c.author_kind == "agent" else c.author_kind
            lines.append(f"- {author}: {truncate(c.body, 600)}")
    return "\n".join(lines)


def ticket_handoff(brief: str) -> str:
    return (
        f"The user handed you this ticket to work on:\n\n{brief}\n\n"
        "Plan it and delegate clear assignments to the right agents (split across several agents if useful). "
        "If the ticket is unclear, comment on it with your questions instead of guessing."
    )


def assignment_prompt(assignment: Assignment, from_name: str, ticket: Task | None) -> str:
    ref = f" (for ticket #{ticket.number}: {ticket.title})" if ticket else ""
    return (
        f"Assignment #{assignment.id} from {from_name}{ref}: {assignment.title}\n\n"
        f"{assignment.instructions.strip()}\n\n"
        "When finished, reply with your concise report (it is sent back automatically)."
    )


def ask_prompt(from_agent: Agent, question: str) -> str:
    return f"Question from {from_agent.name} ({from_agent.role or 'agent'}) — they are waiting for your answer:\n\n{question.strip()}\n\nAnswer concisely."


def message_prompt(from_agent: Agent, message: str) -> str:
    return f"Message from {from_agent.name} ({from_agent.role or 'agent'}):\n\n{message.strip()}\n\nYour reply is delivered back to them."


def wake_prompt() -> str:
    return "Process the updates above: review results, delegate follow-ups if needed, update tickets. If nothing needs doing, reply briefly."


def with_preface(text: str, *, now: str, inbox: list[str]) -> str:
    preface = [now]
    if inbox:
        preface.append("Updates since your last turn:\n" + "\n".join(f"- {item}" for item in inbox))
    return "\n\n".join(preface) + "\n\n" + text


RECENT_MESSAGE_CHARS = 4000


def fresh_session_prompt(recent: list[tuple[str, str]], text: str) -> str:
    if not recent:
        return text
    lines = ["Most recent conversation (verbatim, for continuity):"]
    for role, content in recent:
        lines.append(f"[{role}]: {truncate(content, RECENT_MESSAGE_CHARS)}")
    return "\n".join(lines) + "\n\n---\n\n" + text


def summary_prompt(previous: str | None, transcript: str) -> str:
    prev = f"<previous_summary>\n{previous}\n</previous_summary>\n\n" if previous else ""
    return f"{prev}<transcript>\n{transcript}\n</transcript>\n\nWrite the updated summary."
