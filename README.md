<div align="center">

<img src="docs/logo.svg" width="64" height="64" alt="">

# PlainBoard

**A team of AI agents that builds your software. You only talk to its manager. Your friends do too.**

You say what you want. A manager agent plans the work, specialised agents build it,<br>
and PlainBoard checks and lands what they finish. Self-hosted, started with one command.

[![CI](https://github.com/hthoene/plainboard/actions/workflows/ci.yml/badge.svg)](https://github.com/hthoene/plainboard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[How it works](#how-it-works) · [Features](#features) · [Quick start](#quick-start) · [Where things are](#where-things-are) · [Guide](docs/guide.md) · [Architecture](ARCHITECTURE.md) · [slop2gether.com](https://slop2gether.com)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/board-dark.webp">
  <img src="docs/screenshots/board-light.webp" alt="The board of a project with four people: five columns, the agents running now, the branches being landed, and the manager chat with messages from Mia, Jonas and Lena" width="100%">
</picture>

</div>

## How it works

1. **You say what you want**, in the chat or as a ticket on the board. Alone or with the people you
   work with: everyone talks to the same manager.
2. **The manager plans it.** It writes the tickets, builds the team and gives each piece of work to
   one agent as a complete assignment.
3. **The agents build it, PlainBoard lands it.** Finished branches are merged together, checked once,
   and what passes goes into your main branch.

```mermaid
flowchart LR
    you([You and your team]) -- chat, ticket or idea --> manager[Manager]
    manager -- assignment --> agents[Agents]
    agents -- report --> manager
    agents -- finished branch --> train[One check run<br>for all branches]
    train -- lands --> main[(main)]
```

You never assign a ticket to an agent, write a prompt for one or merge a branch. When an agent needs
a call from you, the ticket says so, and you answer with one click.

## Why PlainBoard

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>Simple from the first minute</h3>
      One file, one command, and it runs. There are no config files to edit: PlainBoard asks a few
      short questions in the browser, each with a recommendation. After that you have one chat with
      one manager, and the board shows the rest.
    </td>
    <td width="50%" valign="top">
      <h3>Finished means landed</h3>
      The work does not end at a branch that waits for you. PlainBoard merges the finished branches
      into one train, checks it once and lands what passes. If a check fails, one agent makes the
      branches pass together.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h3>Your tokens pay for work, not for waiting</h3>
      No agent sits in a test run or polls a pipeline: PlainBoard waits, without a model. Ten changes
      cost one check run, not ten. Every ticket starts with a fresh, small context, and long chats
      are summed up by a cheap model.
    </td>
    <td valign="top">
      <h3>Better together</h3>
      Several people in one project, each with a role: they write to the same manager, mention each
      other with <code>@name</code>, answer decisions and share their subscriptions with the
      project. Agents know who wrote what.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h3>You decide, they keep working</h3>
      A question for you shows up on its ticket, with options to click and pictures to compare.
      What waits for you always stands out, on the desktop and on the phone. Meanwhile the team goes
      on with everything else.
    </td>
    <td valign="top">
      <h3>Agents work in a box of their own</h3>
      Agents get a container with a shell, sudo and the network, so they can really build and test.
      PlainBoard itself, its database and your secrets are outside of it and out of their reach.
      Secrets are encrypted at rest.
    </td>
  </tr>
</table>

And it is yours: self-hosted, MIT licensed, on your own subscriptions or API keys, with no account
and no tracking.

## Features

### Work together

Put PlainBoard on a server, add people under **PlainBoard settings → People**, and give each a role
per project:

| Role | Can |
| --- | --- |
| Viewer | read everything in the project: board, chats, team, notes, statistics |
| Member | also work in it: tickets, chats with the agents, the team, notes, schedules |
| Owner | also its settings, repositories, secrets, MCP servers, public API keys and members |

Everyone writes in the same chats, with their name and picture. `@name` in a message or comment
mentions someone: it is highlighted, and they get a browser notification (if they want one) that
opens where it was written. People can bring their own Claude or Codex subscription and share it with a project or
with selected people: the project runs on it, nobody sees its token.

<table>
  <tr>
    <td width="60%">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/chat-dark.webp">
  <img src="docs/screenshots/chat-light.webp" alt="The chat view: conversations with every agent, the manager chat with messages from three people and mentions, what waits for you, and the plan limits">
</picture>
    </td>
    <td width="40%">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/members-dark.webp">
  <img src="docs/screenshots/members-light.webp" alt="The members of a project: two members, the owner and a viewer">
</picture>
    </td>
  </tr>
  <tr>
    <td><b>One chat, many people.</b> Every conversation, what waits for you, what the manager handed out, and how full the subscriptions are.</td>
    <td><b>Roles per project.</b> Viewer, member or owner; admins run the installation.</td>
  </tr>
</table>

### Plan with ideas

**Ideas** is a pinboard for what is not a ticket yet. Write down whatever comes to mind, one sentence
is enough. The AI sorts the ideas into topics, notices duplicates and asks when something is unclear;
you can move cards, attach files and restore older versions. Nothing starts there: when you
are ready, ask the manager to turn the ideas into tickets.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/ideas-dark.webp">
  <img src="docs/screenshots/ideas-light.webp" alt="The ideas pinboard: three topics with idea cards, one with an open question and answers to click" width="100%">
</picture>

### Watch the work land

**Flow** shows every repository from left to right: who is working, what was handed over, the train
being checked with its progress, and what is on the main branch, with the history of every train
below. An agent hands its finished branch over with `merge_when_green`; the repository's **quick
check** (format, lint) runs at once, the full **check command** once per train. A repository can also
**land first**: branches are merged at once and the base branch is checked afterwards. A branch can be
**held** until its author or the manager releases it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/flow-dark.webp">
  <img src="docs/screenshots/flow-light.webp" alt="The flow view: agents at work in two repositories, a handed-over and a held branch, a train of two branches being checked, what landed, and the history with a failed train and its repair" width="100%">
</picture>

### Decide in one click

<table>
  <tr>
    <td width="60%">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/ticket-dark.webp">
  <img src="docs/screenshots/ticket-light.webp" alt="A ticket on which an agent asks for a decision between two layouts, each with a picture">
</picture>
    </td>
    <td width="40%" valign="top">
      An agent asks, you choose: pick an option, answer in your own words or discuss it in the chat.
      The ticket stays marked until someone has decided.<br><br>
      <b>Quick questions.</b> <kbd>Ctrl</kbd>+<kbd>Enter</kbd> in the chat asks a fast agent instead:
      it looks the answer up in the code, tickets and history, changes nothing, and only you see it.
      The manager neither waits for it nor learns of it.<br><br>
      <b>Search.</b> <kbd>Ctrl</kbd>+<kbd>K</kbd> searches areas, tickets, projects and the
      project's history, by meaning in any language (with PostgreSQL).
    </td>
  </tr>
</table>

### On the phone too

<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/phone-board-dark.webp">
  <img src="docs/screenshots/phone-board-light.webp" alt="The board on a phone" width="30%">
</picture>
  &nbsp;
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/phone-chat-dark.webp">
  <img src="docs/screenshots/phone-chat-light.webp" alt="The manager chat on a phone, with a mention" width="30%">
</picture>
  &nbsp;
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/phone-ticket-dark.webp">
  <img src="docs/screenshots/phone-ticket-light.webp" alt="A decision between two layouts on a phone" width="30%">
</picture>
</p>

### And the rest

- **A board that shows who is doing what.** Every ticket shows which agent works on it and for how
  long. Tickets take Markdown, labels, priorities, attachments and comments. Files of done tickets
  can be deleted after a number of days, per project (off by default).
- **A team per project.** Each agent has a role, a model, an effort level and its own tools. Claude
  and Codex work side by side, on API keys or on your subscriptions. Agents can also run on a model
  server of your own (Ollama, LM Studio, llama.cpp, vLLM) and on other hosts: Gemini, OpenRouter,
  DeepSeek, Kimi, GLM, Mistral, Groq and more.
- **Skills and MCP servers.** Give a project's agents Claude skills (uploaded or from a git
  repository) and MCP servers.
- **A team that remembers.** Shared notes, each agent's own notes, and a ticket that knows what was
  done, decided, tried and dropped. With PostgreSQL, agents search the project's history by meaning.
- **Runners on other servers.** A project's agents can work on a server of their own: it only needs
  Docker and connects out to PlainBoard ([below](#agents-on-another-server)).
- **PlainBoard in your own AI client.** PlainBoard is an MCP server: with a personal token, Claude
  Desktop, Claude Code, Cursor and others work with your projects, with your role.
- **A look at what was built.** Agents post a link to the page they started, and the **Browser** view
  shows it as the agent sees it.
- **Show progress on your website.** Tickets can have a title and text for customers, in several
  **customer languages**, and the public API serves them.
- **Numbers to steer by.** How fast work lands, what it costs, how full your plans are, and what
  slows the team down.
- **Desktop and phone, three designs, dark and light.** slop2gether, Classic and Fahrplan; installable
  as an app.

## Quick start

You need Docker. In an empty folder:

```bash
curl -o docker-compose.yml https://raw.githubusercontent.com/hthoene/plainboard/main/deploy/docker-compose.yml
```

```bash
docker compose up -d
```

Then open <http://localhost:8080>. There is nothing to configure in files. PlainBoard asks for what
it needs:

1. **Create your account.**
2. **Answer a few short questions**, each with a recommendation: which subscription or
   [API key](https://console.anthropic.com/) the agents run on, which models, how they reach your
   code, how many work at once.
3. **Create your first project** and tell the manager what you want.

The file starts three containers: the app, the runner the agents work in, and PostgreSQL (with
pgvector, for the search by meaning). An installation that still has its data in SQLite stays on it
until you move it with one click: **PlainBoard settings → General → Move to PostgreSQL**
([guide](docs/guide.md#postgresql)).

To update:

```bash
docker compose pull && docker compose up -d
```

PlainBoard tells you in the sidebar when a newer version exists.

<details>
<summary>Without curl, or in Windows PowerShell: copy the file</summary>

Save this as `docker-compose.yml`, then run `docker compose up -d`:

```yaml
# PlainBoard. Save this file as docker-compose.yml, then:
#
#   docker compose up -d
#
# and open http://localhost:8080. Everything else is set in the browser.
# To update: docker compose pull, then docker compose up -d.
# :latest is the newest release. Every commit of main instead: PLAINBOARD_TAG=edge (in .env or the
# environment); a fixed release: PLAINBOARD_TAG=1.2.3 (RELEASING.md).

name: plainboard

services:
  # The web UI and everything PlainBoard does by itself.
  app:
    image: thoenehannes/plainboard:${PLAINBOARD_TAG:-latest}
    restart: unless-stopped
    ports:
      # On a server: a PLAINBOARD_PASSWORD, behind a reverse proxy with TLS (PLAINBOARD_BIND only if the proxy is on another machine).
      - "${PLAINBOARD_BIND:-127.0.0.1}:${PLAINBOARD_PORT:-8080}:8080"
    environment:
      # The setup password: asked once, when the first account is created in the browser.
      PLAINBOARD_PASSWORD: ${PLAINBOARD_PASSWORD:-}
      # Security (docs/guide.md, "Security"): previews on a name of their own, and HTTPS behind a proxy.
      PLAINBOARD_PREVIEW_ORIGIN: ${PLAINBOARD_PREVIEW_ORIGIN:-}
      PLAINBOARD_PREVIEW_SANDBOX: ${PLAINBOARD_PREVIEW_SANDBOX:-false}
      PLAINBOARD_SECURE_COOKIES: ${PLAINBOARD_SECURE_COOKIES:-}
      PLAINBOARD_HSTS: ${PLAINBOARD_HSTS:-true}
      PLAINBOARD_ALLOWED_ORIGINS: ${PLAINBOARD_ALLOWED_ORIGINS:-}
      # auto: PostgreSQL in the "db" below. An installation that has its data in SQLite stays on it until
      # you move it with one click in Settings (docs/guide.md, "PostgreSQL"). Or sqlite, or a PostgreSQL
      # server of yours: postgresql://user:password@host:5432/name.
      PLAINBOARD_DATABASE: ${PLAINBOARD_DATABASE:-auto}
      PLAINBOARD_RUNNER_URL: http://runner:8090
      PLAINBOARD_INTERNAL_URL: http://app:8080
    volumes:
      - data:/data
      - uploads:/uploads
    networks: [default, database]
    depends_on:
      runner:
        condition: service_healthy
      db:
        condition: service_healthy

  # Where the agents work: their shell, their tools, your code. Only the app talks to it.
  runner:
    image: thoenehannes/plainboard-runner:${PLAINBOARD_TAG:-latest}
    restart: unless-stopped
    volumes:
      # To let agents work in a folder of your machine: PLAINBOARD_WORKSPACES_DIR=/home/me/code
      - ${PLAINBOARD_WORKSPACES_DIR:-workspaces}:/workspaces
      - uploads:/uploads:ro
      - agent-home:/home/agent

  # The database. Reachable by the app only: neither from outside nor from the agents in the runner.
  db:
    image: pgvector/pgvector:pg17
    restart: unless-stopped
    environment:
      POSTGRES_USER: plainboard
      POSTGRES_PASSWORD: plainboard
      POSTGRES_DB: plainboard
    volumes:
      - db:/var/lib/postgresql/data
    networks: [database]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U plainboard -d plainboard"]
      interval: 5s
      timeout: 5s
      retries: 20

networks:
  database:
    internal: true

volumes:
  data: # the database and the key your secrets are encrypted with: back this up
  db: # PostgreSQL's data: back this up too
  uploads:
  workspaces:
  agent-home:
```

</details>

<details>
<summary>Build from source, to change PlainBoard itself</summary>

```bash
git clone https://github.com/hthoene/plainboard.git
cd plainboard
docker compose up -d --build
```

</details>

### Agents on another server

A project's agents can work on a server of their own: more room, more power, or projects kept apart.
That server only needs Docker; it connects out to PlainBoard, so it needs no open port.

1. **PlainBoard settings → Runners**: enter a name, **Add runner**. PlainBoard shows a
   `docker-compose.yml` with its address and a token filled in (the template is
   [`deploy/runner-compose.yml`](deploy/runner-compose.yml)).
2. On the other server, save it in an empty folder and run `docker compose up -d`.
3. **Project settings → Runner**: choose it.

The runner updates itself to PlainBoard's version. Should PlainBoard ask for it:
`docker compose pull && docker compose up -d` in its folder. Details in the
[guide](docs/guide.md#run-a-project-on-another-server).

## Where things are

| You want to | Go to |
| --- | --- |
| Change a subscription, the models, access to your code | The gear: **PlainBoard settings** |
| Change a project: its repositories, secrets, skills, members | **Project settings**, in the **…** menu next to the project name |
| Notes, schedules, the browser window, statistics | The **…** menu next to the project name |
| Change an agent: its model, instructions, tools | **Team** |
| Work with others | **PlainBoard settings → People**, then **Project settings → Members**: viewer, member or owner |
| Share your own subscription | **PlainBoard settings → Subscriptions**: who may use it |
| Collect ideas before there are tickets | **Ideas** |
| See what is being checked and landed | **Flow** |
| Ask something without disturbing the manager | <kbd>Ctrl</kbd>+<kbd>Enter</kbd> in the chat |
| Find a ticket, an area or something from the history | <kbd>Ctrl</kbd>+<kbd>K</kbd> |
| Use PlainBoard from Claude Desktop, Claude Code or Cursor | **PlainBoard settings → MCP / API access** |
| Show progress on your website | **Project settings → Public API** and **Customer languages** |
| Keep the disk small | **Project settings → Board**: delete the files of done tickets after some days |
| Change the look | The palette button: three designs, each light and dark |
| Run it on a server | `.env`: a setup password (`PLAINBOARD_PASSWORD`), behind a reverse proxy with TLS: [guide](docs/guide.md#behind-a-reverse-proxy) |
| Use another port than 8080 | `.env`: `PLAINBOARD_PORT=9000` |
| Let agents work in a folder on your machine | `.env`: `PLAINBOARD_WORKSPACES_DIR`, then **Project settings → Advanced → Shared folder** |

`.env` is optional: a file next to your `docker-compose.yml`. [`.env.example`](.env.example) explains each line. The [guide](docs/guide.md) walks
through the first project, lists every setting and has the details.

## Good to know

- Agents have full control of the **runner** container: shell, passwordless sudo, network. They
  cannot touch the app container or the database. Only give them credentials you are comfortable with.
- All agents on one runner share it: someone who can chat with agents can have them read whatever
  that runner holds. Give roles to people you trust, or give a project a runner of its own.
- An Anthropic API key is the officially supported way to use the Claude Agent SDK. Whether you may
  use your own Claude login or ChatGPT subscription depends on the provider's terms for your plan;
  check them before relying on it. PlainBoard is not affiliated with Anthropic or OpenAI.
- Agents on other models (a model server of your own, Gemini, another host) run in the same harness
  as agents on Claude. Anthropic does not support that harness on models that are not Claude's: it
  works as far as the model does.
- Secrets are encrypted at rest with a key generated in the `data` volume. Back up the `data` and
  `db` volumes.

## Development

```bash
# backend and runner tests
docker compose build app runner
docker run --rm -w /src/app --entrypoint python plainboard-app -m pytest -q -n auto
docker run --rm -w /src/runner plainboard-runner python -m pytest -q

# frontend with hot reload, proxies /api to a running stack on :8080
cd frontend && npm install && npm run dev
```

[ARCHITECTURE.md](ARCHITECTURE.md) explains the design and the extension points,
[DESIGN.md](DESIGN.md) the look. The screenshots are made with
[`scripts/screenshots`](scripts/screenshots).

## License

[MIT](LICENSE)
