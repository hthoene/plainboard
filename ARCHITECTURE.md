# Architecture

```
┌──────────────────────────── docker compose ─────────────────────────────┐
│ app  (FastAPI + React SPA)                          127.0.0.1:8080       │
│  api/            REST + WebSocket (UI), /api/internal (runner callbacks) │
│  services/       domain logic, shared by REST API and agent tools        │
│  tools/          agent tool registry (single source of truth)            │
│  orchestration/  run queue, context/compression, prompts, scheduler      │
│  SQLite (WAL) + Alembic · uploads volume                                 │
│        │ POST /runs  (RunSpec → NDJSON RunEvents)      ▲ tool calls       │
│        ▼                                               │ (per-run token)  │
│ runner  (internal only; user `agent` with sudo)                          │
│  providers/      claude_code (Claude Agent SDK) · mock · (openai later)  │
│  tool_bridge.py  proxies tool calls to the app; local tools (files)      │
│  workspace.py    /workspaces/<project>/<agent>/<repo> or a shared folder  │
└──────────────────────────────────────────────────────────────────────────┘
protocol/  shared pydantic models (RunSpec, RunEvent, ToolDef, ...)
```

**The app is the brain, the runner only executes.** The app decides what an agent sees (system prompt,
resumed session or summary, tools, MCP servers, credentials) and sends a `RunSpec`. The runner runs it
with a provider and streams normalised `RunEvent`s back. Agents get full control of the runner
container, but the app, its database and its secrets stay out of their reach. Each run gets a random
token that is valid only while the run lasts and only for that run's project and agent.

## Core concepts

| Concept | Where | Notes |
|---|---|---|
| Ticket (`Task`) | `models.py`, `services/board.py` | Never assigned to agents. Statuses live in `TaskStatus`. |
| Customer texts | `task.customer_title`, `task.customer_description`, `api/public.py` | Short public title and description on a ticket, mostly written by the manager. `GET /api/public/tasks` with a project key returns only tickets with a customer title, and only these texts, number, status and date. |
| Assignment | `run_manager.delegate` | A work order for one agent, optionally linked to a ticket. |
| Decision | `services/decisions.py`, `tools/board.py` | A question from the manager to the user on a ticket, with options to click and attachments for the user's review. The answer becomes a ticket comment and an inbox update that wakes the agent that asked. If the user answers in chat, the manager records it with `resolve_decision`. |
| Chat attachment | `services/board.py`, `api/chat.py` | A file sent with a chat message: an `Attachment` without a ticket, listed on the `Run` and in the user message's meta. The agent gets its path under `/uploads` in the turn and opens it with its file tools. |
| Run | `run_manager.py` | One agent turn. Triggers: chat, ticket, assignment, ask, message, schedule, wake. |
| Conversation | `context.py` | One endless conversation per agent, with provider session, summary and token totals. |
| Inbox | `orchestration/inbox.py` | Prepended to the agent's next turn. Manager wake-ups are coalesced. |
| Job | `services/jobs.py`, `tools/jobs.py`, `orchestration/job_watcher.py`, `runner/jobs.py` | A slow shell command that runs in the runner independently of the turn that started it, so the agent can finish its turn and answer the user. The watcher polls the runner and delivers the result as an inbox update with a wake-up. A runner restart loses running jobs, which is reported the same way. |
| Setup script | `project.setup_script`, `runner/workspace.py` | Maintained by the manager (no user setup): agents install what they need, the manager records the commands. The runner runs each script once per container start, one at a time, before the agent starts, so installs survive rebuilds; a failure becomes a notice in the prompt. Home (`agent-home` volume) is permanent and shared by all agents; `BASH_ENV` loads `~/.profile` in their non-interactive shells. |
| Repository | `services/repositories.py` | Many per project. Per agent: `all_repos` or an explicit selection. The runner clones them into `<workspace>/<name>` and fetches existing clones before each run; a clean checkout is fast-forwarded (not in a shared folder). A failed clone or fetch becomes a notice in the prompt instead of failing the run. |
| Note | `services/notes.py`, `tools/knowledge.py` | Durable knowledge, independent of chat compression. `create_note` rejects similar titles, pinned notes are capped by `prompts.PINNED_BUDGET_CHARS`, and the manager gets a tidy-up reminder every 30 notes. |

### Run lifecycle (`orchestration/run_manager.py`)
1. `enqueue` creates a `Run(queued)`. The dispatcher starts it when the agent is idle and fewer than
   `max_parallel_runs` top-level runs are active. Nested `ask_agent` runs bypass the limit, so a run
   that is waiting can never starve the run it waits for.
2. `_prepare`: consume the inbox, store the user message, build the turn (`ContextManager.build_turn`),
   issue a run token, then assemble the `RunSpec`.
3. `_stream`: persist assistant texts and tool calls, publish text deltas over the WebSocket, and track
   context size from usage events.
4. `_finish` → `_after_run`: route results (answer to the asker, report to the requester's inbox plus a
   ticket comment, wake the manager), then `maybe_compress`.

A restart fails running runs. Nothing retries them, because they may be half done: the requester of an
interrupted assignment (or the manager, for its own turn) gets an inbox update and a wake-up.

A job outlives its run: the stop button of a run does not touch it. If it is still running when a worker
reports, the assignment counts as done; a worker's wake-up result goes to the manager's inbox.

`ask_agent` blocks on a future. A wait-for graph between agents rejects cycles, and depth and timeout
limits apply. An answer that arrives after a timeout goes to the asker's inbox instead.

### Context & compression (`orchestration/context.py`)
- With a session: resume it (prompt caching, full tool history inside Claude Code). The system prompt
  is **frozen per session** (`conversation.system_prompt`) so the cached prefix stays valid. If the
  briefing changes mid-session (team, pinned notes, repositories, instructions …), only the changed
  sections are sent as a `[Briefing update]` at the start of the next turn (`prompts.briefing_update`).
  A new session (first turn, after compression or reset) gets a fresh system prompt with the current state.
- Above `agent.context_limit_tokens`: summarise every uncovered message except the last
  `keep_recent_messages` text messages, using `agent.summary_model` via `runner /complete`. Large
  transcripts are summarised in chunks. Then drop the session and record a `summary` message.
- Fresh session: system prompt + `## Memory` (summary) + recent messages verbatim (truncated) + the new
  turn. This also works unchanged for stateless providers.
- A provider may compact its own session, e.g. mid-run near the model's context limit (`compaction`
  event). The session continues, a `summary` message without text is recorded, and the next turn
  re-sends what differs from the frozen prompt (`conversation.session_sections`), because earlier
  briefing updates may be lost. This also applies when `context_limit_tokens` is above the provider's
  own threshold: then the provider compacts and PlainBoard's compression does not start.

### Prompts (`orchestration/prompts.py`)
The system prompt is stable (cache-friendly): role, area, guide, workspace, instructions, team roster,
pinned notes, memory. Volatile data such as the current time and inbox updates goes into the user turn.
The manager uses the `board` toolset: a short custom prompt and no Claude Code preset, with shell and
file editing for small tasks it would cost more to delegate. Workers use
`full`: the Claude Code preset with the PlainBoard part appended.

## Extension points

**Add an agent tool.** Add a decorated function to a module in `app/plainboard/tools/`:

```python
class MyArgs(BaseModel):
    number: int

@tool("my_tool", "What it does (shown to the model).", MyArgs, roles=(MANAGER,))
async def my_tool(ctx: ToolContext, args: MyArgs) -> str:
    ...  # use services, return compact text
```
The schema is derived from the args model, stripped of titles, and sent with every run. The runner
needs no change. Tools that need the agent's filesystem are declared with `local_tool(...)` and
implemented in `runner/plainboard_runner/tool_bridge.py`.

**Add a provider (e.g. OpenAI).**
1. Implement `Provider` in `runner/plainboard_runner/providers/<name>.py`: `info()` (models and efforts),
   `run(spec, bridge)` yielding `RunEvent`s and ending with a `ResultEvent`, and `complete(spec)`.
2. Register it in `providers/registry.py`.
3. Credentials are already part of `Credentials` (`openai_api_key`, settings key `openai_api_key`).

The UI picks up models from `/api/meta`. If the provider has no server-side sessions, return no
`session_id`: the context manager then rebuilds every turn from summary + recent messages.

**Add a ticket status.** Extend `TaskStatus` (backend), `Status` in `schemas.py`, `STATUSES` and
`statusColor` in `frontend/src/features/board/Board.tsx`, and the `taskStatus` texts in `en.json`.

**Add a UI language.** Copy `frontend/src/i18n/en.json` to `<code>.json` and register it in `i18n/index.ts`.

**Database changes.** Edit `models.py`, then generate a migration:
```bash
docker run --rm -v "$PWD":/repo -w /repo/app plainboard-app sh -c \
  "PYTHONPATH=/repo/app PLAINBOARD_DATA_DIR=/tmp/db python -m plainboard.migrate revision 'describe change'"
```
Migrations run automatically on startup.

## Testing
- `app/tests`: a `FakeRunner` executes tools through the real `RunManager`. It covers ticket hand-off,
  delegation with reports and wake-ups, `ask_agent`/`message_agent`, compression, schedules, tool
  scoping and settings.
- `runner/tests`: SDK event mapping, tool bridge, workspace safety, the mock provider over HTTP.
- End-to-end without credentials: set an agent's provider to **Mock** and use the directives
  `TOOL <name> <json>` / `SAY <text>` in messages.
