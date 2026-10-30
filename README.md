# PlainBoard

[![CI](https://github.com/hthoene/plainboard/actions/workflows/ci.yml/badge.svg)](https://github.com/hthoene/plainboard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A minimal, self-hosted workspace for a team of AI agents: a **Kanban board** and a **chat with a
project manager agent** that plans tickets, builds the team, and delegates clear assignments to
specialised agents. Agents run on Claude via the **Claude Agent SDK** (Anthropic API key, or your own
Claude login); the provider layer is ready for others such as OpenAI.

- **Kanban board**: tickets with Markdown, labels, priorities, attachments (paste or drag & drop
  images, click for a large view), and comments.
- **Your decisions**: the manager can ask you on a ticket, e.g. to choose between two design
  proposals with screenshots. Pick an option with one click, answer in your own words, or discuss it
  in chat. The ticket shows a badge until you have decided.
- **Manager chat**: the manager reads tickets, splits them into self-contained assignments for one or
  more agents, reviews the reports, and moves the tickets. It also manages agents, notes, schedules
  and MCP servers.
- **Team per project**: each agent has a role, a narrow area of responsibility, its own model,
  effort level and capabilities. Agents can ask each other (`ask_agent`) or message each other
  (`message_agent`).
- **Endless chats with few tokens**: every agent compresses its own context once it exceeds a
  configurable limit, using a cheap model (Claude Haiku 5.5 by default). See *Token efficiency*.
- **Notes** (shared knowledge; pinned notes are in every prompt), **schedules** (one-off or cron),
  **MCP servers** (stdio/http/sse; for all or selected agents).
- **Repositories (optional)**: a project can have any number of git repositories. Each agent gets
  the repositories it has access to (all, or a selection) cloned into its workspace, one folder per
  repository. The manager can add or remove repositories and change who has access. With a
  fine-grained GitHub token, agents branch, push and open PRs with `gh`. Without git everything works
  locally.
- **Notes stay tidy**: one note per topic (similar titles are rejected in favour of updating), list
  views show each note's age and size, pinned notes have a fixed token budget, and the manager is
  reminded to consolidate as the collection grows.
- **Responsive**: desktop with a resizable chat panel or a full-page chat; phones get a bottom
  navigation, swipeable board columns and full-screen dialogs.
- **Clean chat by default**: only messages, with events as subtle one-line notes. The eye icon in the
  chat header shows agent activity (tool calls, compressions, context gauge, full event cards).

## Quick start

Requirements: Docker with Docker Compose.

```bash
docker compose up -d --build
```

Open <http://localhost:8080>, then:

1. **Settings → Claude**: paste an [Anthropic API key](https://console.anthropic.com/) and click
   **Test connection**. Alternatively you can use your own Claude login (a token from
   `claude setup-token`, or `claude login` inside the runner container) – see *Notes* below.
2. Optional, **Settings → GitHub**: paste a fine-grained personal access token.
3. Create a project, describe what you want in the manager chat, or add tickets and move them to
   **In Progress**.

Optional configuration: copy `.env.example` to `.env` (port, password, workspace folder, credential
fallbacks).

### Working on local folders (desktop)

Set `PLAINBOARD_WORKSPACES_DIR` in `.env` to a folder on your machine, e.g.

```
PLAINBOARD_WORKSPACES_DIR=D:/Code
```

and set the project's **Local folder** to a sub folder (e.g. `my-app`). All agents of that project work
directly in `D:/Code/my-app` and you see every change immediately. Project repositories are cloned
into sub folders there (e.g. `D:/Code/my-app/backend`) if they don't exist yet. On Linux hosts the folder must be writable by UID 1000.

### Server usage

By default workspaces live in a Docker volume and every agent gets its own clones of the repositories
it may use (branches + pull requests, no conflicts between parallel agents). To expose PlainBoard,
set `PLAINBOARD_BIND=0.0.0.0` and `PLAINBOARD_PASSWORD`, ideally behind a reverse proxy with TLS.

## How work flows

```
You ──chat──▶ Manager ──delegate──▶ Agent A ─┐
  │              ▲  ▲                         │ report (+ ticket comment)
  └─ ticket ─────┘  └──── inbox / auto-wake ──┘
```

- Tickets are **never assigned to agents directly**. Handing a ticket to the manager (button or drag to
  *In Progress*) lets it plan the work and create **assignments**: clear, self-contained work orders for
  one or more agents.
- Assignment reports go back to the manager's inbox and appear as ticket comments. The manager is
  woken up once per batch of reports (turn off per project with *auto-wake*), reviews them, delegates
  follow-ups and moves the ticket to *Review* / *Done*.
- You can chat with any agent directly; the full-page **Chat** view lists all conversations.

## Token efficiency

- **Specialised agents**: each agent only carries the context of its own area.
- **Session reuse**: turns resume the Claude Code session, so prompt caching applies.
- **Compression**: when an agent's context exceeds *Compress at (tokens)*, older messages are
  summarised with the *summary model*. The session restarts with system prompt + summary + the last
  *N* messages verbatim. Chats can grow without limit; the UI always shows the full history. The
  limit must be below the model's context window (default: 400,000); otherwise Claude Code compacts
  first, using its own threshold.
- **Coordinator toolset** for the manager: a short custom prompt instead of the full Claude Code
  prompt, plus board tools and read-only file access.
- **Inbox instead of extra runs**: updates are prepended to an agent's next turn; manager wake-ups
  are coalesced.
- Compact tool schemas and results, effort per agent, titles-only note listings, pinned notes only
  when needed.

## Development

```bash
# backend tests
docker compose build app runner
docker run --rm -w /src/app --entrypoint python plainboard-app -m pytest -q
docker run --rm -w /src/runner plainboard-runner python -m pytest -q

# frontend with hot reload (proxies /api to a running stack on :8080)
cd frontend && npm install && npm run dev
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and extension points.

## Notes

- Agents have full control of the **runner** container (shell, passwordless sudo, network). They
  cannot touch the app container. Only give them credentials you are comfortable with.
- **Claude login vs. API key:** an Anthropic API key is the officially supported way to use the Claude
  Agent SDK. PlainBoard also lets you use your own Claude login (like other open-source agent
  orchestrators do); whether that is permitted depends on Anthropic's terms for your plan – check them
  before relying on it. PlainBoard is not affiliated with Anthropic.
- Secrets (tokens, MCP env/headers) are encrypted at rest with a key generated in the `data` volume.

## License

[MIT](LICENSE)
