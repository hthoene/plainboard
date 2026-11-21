# PlainBoard guide

How to set PlainBoard up, use it, and find every setting. For the design of the code see
[ARCHITECTURE.md](../ARCHITECTURE.md).

**Getting going**
- [First start](#first-start)
- [Your first project](#your-first-project)
- [Everyday use](#everyday-use)
- [Every setting and where it is](#every-setting-and-where-it-is)

**Details**
- [Subscriptions](#subscriptions)
- [Repositories and finished work](#repositories-and-finished-work)
- [People and roles](#people-and-roles)
- [A folder on your machine](#a-folder-on-your-machine)
- [Updating](#updating)
- [Moving a project to another PlainBoard](#moving-a-project-to-another-plainboard)
- [On a server](#on-a-server)
- [Behind a reverse proxy](#behind-a-reverse-proxy)
- [PostgreSQL](#postgresql)
- [Preview](#preview)
- [Browser window](#browser-window)
- [Public API](#public-api)
- [How work flows](#how-work-flows)
- [Token efficiency](#token-efficiency)
- [The team, notes and chat](#the-team-notes-and-chat)

## First start

Save the `docker-compose.yml` from the [README](../README.md#quick-start) in an empty folder and run

```bash
docker compose up -d
```

Open <http://localhost:8080>. PlainBoard asks you to create your account, then goes through six
steps. Each is one decision, says what it recommends, and can be changed later in
**PlainBoard settings**.

| Step | What it asks | Can you skip it? |
|---|---|---|
| Subscriptions | Which subscription or API key the agents run on. The connection is tested when you continue. | No: agents need one |
| Models | Which models managers, members and summaries use | No, but the recommendation is one click |
| Code access | A GitHub token or an SSH key, and the name on the agents' commits | Yes, if you add access per project later or use no git host |
| Pace | How many agents work at the same time | Yes: 3 is a good start |
| Secrets | Keys and tokens your agents need, e.g. for a package registry | Yes |
| People | Further people who may sign in | Yes: you can work alone |

## Your first project

After the set-up PlainBoard opens **New project**. It has five steps, and the project exists after
the first one, so closing early loses nothing.

1. **Project**: a name, and a few sentences on what is being built. The manager reads them on every
   turn.
2. **Code**: the git repositories the agents work in. A project can start without one.
3. **Team**: every project starts with a manager. Its model, the subscription and the number of
   agents come from your settings; change them here only if this project differs.
4. **Secrets**: keys and tokens for this project only.
5. **People**: who works on it with you, if anyone.

A new project starts with two tickets in *To Do*: the manager finds out with you what the project
is about, and then proposes a team (which model plans, builds and reviews), how many agents work at
once and how finished work lands, and sets it up once you agree. Write to the manager in the chat
next to the board to begin; it knows of both tickets. After that, tell it what you want: it writes
the tickets, creates the agents it needs, hands out the work and reports back.

## Everyday use

- **Say what you want.** Write to the manager. Or add a ticket yourself and hand it over: open it and
  click **Hand to manager**, or drag it to *In Progress*. You never assign a ticket to an agent.
- **The team keeps going by itself.** What is in *To Do* is work the team takes first, when someone
  is free. When nothing there can start, it goes on with the *Backlog*, the most urgent first, and
  leaves only what needs you (such a ticket is marked and counted in the **for you** plate). If
  tickets are ready and slots stay free for a quarter of an hour, PlainBoard wakes the manager and
  shows it exactly those tickets; the same ones at most twice. When nothing can start, nobody is
  woken and nothing is spent.
  To keep the Backlog for later, switch off **Project settings → Team → Go on with the Backlog when
  To Do cannot start**: then nothing there is started until you move it to *To Do*.
- **Watch the board.** A ticket shows which agent works on it and for how long. *Running now* lists
  everyone who is working.
- **Answer what waits for you.** When an agent needs a decision, or something only you can do, the
  ticket is marked and counted in the **for you** plate. Pick an option, answer in your own words, or
  discuss it in the chat.
- **Review only what needs you.** Finished tickets go to *Done*. One lands in *Review* only when you
  have to look at something.
- **Talk to anyone.** Every agent has its own chat; the **Chat** view lists them all. You can stop an
  agent from its chat.
- **Keep an eye on the quota.** The ring under the message field shows how full each subscription
  is.

## Every setting and where it is

There are two places for settings, and a few things live in their own view.

**PlainBoard settings** (the gear at the bottom of the sidebar; on a phone under *More*). For the
whole installation. Everyone finds their own account here; the rest is for admins.

| Section | What is in it |
|---|---|
| Your account | Your name, your password, signing out |
| People | Who may sign in, who is an admin, who may create projects |
| Subscriptions | Claude and Codex subscriptions and API keys; model servers of your own and other model hosts |
| Models | The models new agents start with, each with its thinking effort (managers, members, summaries); when long chats are summarised |
| Code access | GitHub token, SSH key, the name and address on the agents' commits |
| Pace | How many agents work at the same time per project; how far a manager may raise that |
| Secrets | Keys and tokens for the agents of all projects |
| General | Time zone, the notice about newer versions |
| Read access | Keys that can read everything and change nothing |

**Project settings** (in the navigation; on a phone under *More*). For one project, for its owners.

| Section | What is in it |
|---|---|
| General | The project's name, description, and the language its agents write to you in |
| Repositories | The repositories: by address, chosen from GitHub, or newly created there. Click one for its address, base branch, checks and access |
| Team | Pause this project for good; how many agents work at once, which subscription they run on, whether the manager is told when work is finished |
| Board | When tickets leave *Review* and when done tickets are archived |
| Secrets | Keys and tokens for this project's agents only |
| Members | Who sees the project, as viewer, member or owner |
| Public API | Keys for showing progress on a website |
| Advanced | A GitHub token for this project (with a test), a shared folder, deleting the project: with or without the folders its agents worked in |

**In their own view**

| Where | What |
|---|---|
| Team | Each agent: its role, model, effort, instructions, repositories, tools and subscription; its own notes and to-dos |
| Tools | MCP servers |
| Schedules | Work that starts by itself, once or regularly |
| Notes | What all agents of the project should know |
| Stats | How fast work lands, what slows it down, what it costs; the downloads |
| The palette button | Design and light or dark, per browser |
| `.env` (see `.env.example`) | Only what the browser cannot set: a server, another port, a folder of your machine |

## Subscriptions

The set-up asks for these; later they are under **PlainBoard settings → Subscriptions**.

- **Claude**: paste an [Anthropic API key](https://console.anthropic.com/) and click
  **Test connection**. Alternatively use your own Claude login: a token from `claude setup-token`, or
  `claude login` inside the runner container.
- **Codex**: sign in with your ChatGPT subscription (a device code you enter on
  chatgpt.com) or paste an OpenAI API key. Then pick **Codex** as the provider of the agents that
  should run on it; the others stay on Claude.
- Both take several accounts (**Add account**), e.g. two Claude subscriptions. Each agent
  stays on its account while that one has room and then moves to the subscription with the most room
  left. The last 5 % of each subscription are used once all are that full; API keys only step in when
  every subscription is used up.
- A project can name the subscription its agents run on (**Project settings → Team**), and an agent
  can have its own (**Team**).
- An admin can limit a project to some subscriptions (**Project settings → Team → Subscriptions this
  project may use**). Its agents then run on those only: no choice inside the project gets around
  it, and a used-up subscription is not replaced by one from outside. For a project you share with
  others while another subscription is meant for your own work. Nothing ticked means any.
- The plan limits of every subscription are shown under the chat's message field, one ring per
  subscription; a click shows the windows, their resets and a forecast.

### Other models

Agents can also run on models of your own or of other hosts (**Subscriptions → Other models**):

| Kind | Providers |
| --- | --- |
| A model server of your own | Ollama, LM Studio, llama.cpp server, vLLM |
| Model hosts | Gemini (Google), OpenRouter, DeepSeek, Kimi (Moonshot), GLM (Z.ai), MiniMax, Qwen (Alibaba), Grok (xAI), Mistral, Groq, Cerebras, Fireworks, Together, Hugging Face, Nvidia NIM, Azure OpenAI |
| Anything else | A server or gateway that speaks Anthropic's Messages interface (LiteLLM, for one) or OpenAI's Chat Completions interface |

Choose the provider, **Add**, and fill in:

- **Address**: empty for a host's usual one. A model server on the machine PlainBoard runs on is
  `http://host.docker.internal:<port>`, not `localhost`: agents work inside a container.
- **API key**: for a host. A model server of your own usually needs none.
- **Models agents can be given**: one per line, named as the server names them. **Show what the
  server offers** lists them to tick. PlainBoard keeps no list of its own: model names change by the
  month. A provider shows up in the model settings and for agents once it names a model.
- **Context window**: how many tokens these models take at once. The agent sums up its work before
  it gets there. A model server of your own often starts with a few thousand tokens until you raise
  it; an agent needs 64,000 or more.
- **Test connection** asks the first model for one word.

Then pick the provider for an agent (**Team**), or as the default (**PlainBoard settings → Models**).

What to expect: an agent on such a model works in the same harness and with the same tools as one on
Claude, so everything in PlainBoard works the same. How well it works is the model's part. It has to
call tools reliably and keep to a long prompt; small models lose the thread. Effort levels, plan
limits and costs are not known for these models, so the statistics show their tokens and no price.
Anthropic does not support its harness on models that are not Claude's; if a server refuses a
request, the error in the agent's chat says what it did not take.

## Repositories and finished work

A project can have any number of git repositories (**Project settings → Repositories**). Each agent
gets the repositories it has access to (all, or a selection) cloned into its workspace, one folder
per repository. Without git everything works locally.

**Access.** One access for all projects is under **PlainBoard settings → Code access**: a GitHub
token (agents branch, push and open pull requests with `gh`), or one SSH key that you add to your
account at any git host. A repository can have its own instead (click the repository): a token of
any git host, or an SSH key that PlainBoard creates for you to add as a deploy key.

**Finished work.** An agent hands over its finished branch and goes on. PlainBoard brings the
branches to the base branch by itself. Per repository you decide when:

- **After the checks pass** (the default): all finished branches are merged, checked once together,
  and only what passes reaches the base branch. Right for a branch you release from.
- **At once, checked afterwards**: a branch is merged at once and the base branch is checked
  afterwards; if that fails, an agent repairs it. Much faster where checks take long or often fail,
  but the base branch can be broken for a while: right for a development branch.

Two commands make this work, both optional. The **quick check** (formatting, lint, a minute or two)
runs the moment a branch is handed over, and a failure goes straight back to that agent. The **full
check** (your test suite or CI script) decides whether work may stay; without one, on GitHub, the
checks of a pull request decide.

## People and roles

Add further people in **PlainBoard settings → People**. They see nothing until they are a member of
a project (**Project settings → Members**):

| Role | Can |
|---|---|
| Viewer | read everything in the project: board, chats, team, notes, statistics |
| Member | also work in it: tickets, chats with the agents, the team, notes, schedules |
| Owner | also its settings, repositories, secrets, MCP servers, public API keys and members; delete it |

Admins run the installation (settings, subscriptions, people) and see every project. Whoever
creates a project owns it; an admin decides per person who may create projects, because every
project uses the installation's subscriptions. A project can name the subscription its agents run
on. Where several people work in a project, agents see who wrote a message or a comment, who
answered a question on a ticket, who finished what a ticket waited for and who handed a ticket over. The chat
shows each person's name and profile picture above what they wrote; you set your picture under
PlainBoard settings, Your account.

Know the limit: all agents share one runner container. Someone who can chat with agents can have
them read whatever the runner holds, also of other projects. Give roles to people you trust.

## A folder on your machine

Set `PLAINBOARD_WORKSPACES_DIR` in `.env` to a folder on your machine, e.g.

```
PLAINBOARD_WORKSPACES_DIR=D:/Code
```

and set the project's **Shared folder** (Project settings → Advanced) to a sub folder (e.g. `my-app`). All agents of that project
work directly in `D:/Code/my-app` and you see every change immediately. Project repositories are
cloned into sub folders there (e.g. `D:/Code/my-app/backend`) if they don't exist yet. On Linux hosts
the folder must be writable by UID 1000.

## Updating

PlainBoard shows a notice in the sidebar when a newer version exists.

```bash
docker compose pull
docker compose up -d
```

To update cleanly, pause first: **PlainBoard settings → Pause everything**. Every project comes to
rest: agents that are working finish their current step and save their work, and nothing new starts.
PlainBoard stops their background jobs and preview servers itself (a restart would end them anyway);
every agent reads which ones afterwards and starts again what it still needs.
The page lists what still moves and says when everything is at rest; then update, and press
**Resume** afterwards (the pause holds across the restart, and a band at the top of every screen says
so). Nothing is lost: every agent goes on with what it had open.

Without the pause an update works too: agents that are in the middle of a turn go on by themselves
afterwards, but a job that is running in the runner is lost when the runner restarts.
Built from a clone instead: `git pull`, then `docker compose up -d --build`.

## Moving a project to another PlainBoard

A project can be taken along as one file, e.g. from your machine to a server.

1. On the old PlainBoard: **Project settings → Move the project → Download the project
   file**. Best while the project is at rest (**PlainBoard settings → Pause everything**), and after
   the agents have pushed their work: the code is not in the file, it comes from git again.
2. On the new one: **New project → Or bring a project from another PlainBoard**, choose the file.
   You become the project's owner.

The file carries the project's settings, the team with its notes, task lists and schedules, the
repositories, the board with its whole history (comments, questions, assignments and their reports),
the notes and, if you want, the chats. Credentials (GitHub tokens, SSH keys, secrets, the environment
of MCP servers) travel only if you give a passphrase: they are sealed with it, and the same passphrase
opens them on the other side. Without one they stay behind, and the import lists what to enter again.

Left behind is what belongs to the installation or to the moment: users and roles, subscriptions,
running work (what was open is closed and ordered again from there), the agents' working folders and
the statistics. Nothing starts by itself after the import: check the subscription and the
repositories' access, then tell the manager to go on. The old project stays as it is; delete it
there when the new one works.

## On a server

By default workspaces live in a Docker volume and every agent gets its own clones of the
repositories it may use (branches and pull requests, no conflicts between parallel agents). To reach
PlainBoard from other machines, set `PLAINBOARD_PASSWORD` in `.env` and put a reverse proxy with TLS
in front ([Behind a reverse proxy](#behind-a-reverse-proxy) says how, and when `PLAINBOARD_BIND` is
needed). The password is the setup password: it is asked once, when the first account is created in
the browser, so nobody else can claim a new server. Browsers offer to install PlainBoard as an app
only over HTTPS or on `localhost`.

## Run a project on another server

A project's agents can work on a server of their own instead of next to PlainBoard: more room, more
power, or projects kept apart (files, installed tools, memory). That server only needs Docker; it
connects out to PlainBoard, so it needs no open port, no TLS and no domain.

1. In **Settings > Runners**, enter a name and click **Add runner**. PlainBoard shows a command (and the
   same as a `docker-compose.yml`) with the address you opened PlainBoard at and a token. The token is
   shown only this once.
2. Run the command on the other server. A few seconds later the runner shows as connected, with its
   version, disk and memory.
3. In the project's settings, under **Runner**, choose it.

Everything of the project then runs there: its agents' turns, background jobs, check runs of its
branches, previews and the browser window (both reach PlainBoard through the runner's connection).
Moving a project is only possible while nothing of it runs (pause it first, or let it finish). Its
workspaces don't move: the new runner clones the repositories again, and each runner has its own home
(installed tools). The setup script, secrets and credentials go with every turn, as before, and only
to the project's own runner. Ticket attachments are copied to it when an agent starts a turn.

If the line breaks for a moment (network, a proxy, a restart of PlainBoard's server), the runner
connects again by itself and the work goes on: turns keep running there and pick up their stream where
it stopped, jobs and previews keep running. If the runner itself restarts, what ran on it ends as after
a restart of the built-in runner and goes on with the next turn. While it is away, new turns, check runs
and branch tidying wait until it is back. After 10 minutes away the
project's manager hears it once. A runner that runs another version than PlainBoard is marked in the
list: update it with `docker pull thoenehannes/plainboard-runner:latest` and start it again. A new token
(key icon) ends the old one at once; removing a runner is possible once no project uses it.

The address in the command is the one you opened PlainBoard at. It has to be reachable from the
other server: behind a reverse proxy that is its public address (the proxy must pass WebSockets on,
as for the UI). Plan limits, sign-ins of subscriptions and the Storage page stay with the built-in runner.

## Behind a reverse proxy

PlainBoard speaks plain HTTP on one port. For HTTPS and a name of your own, put a reverse proxy in
front (Caddy, nginx, Traefik). Where the proxy runs decides what you set in `.env`:

- **On the same machine** (the usual case): leave `PLAINBOARD_BIND` unset. PlainBoard then listens on
  `127.0.0.1` only, so nothing reaches it except through the proxy. Point the proxy at
  `127.0.0.1:8080`.
- **On another machine:** `PLAINBOARD_BIND=0.0.0.0`, so the proxy can reach the port, and close that
  port to everyone else in your firewall: on it PlainBoard is not encrypted.
- **As a container next to PlainBoard:** put it on the same Docker network and let it proxy to
  `app:8080`. No port has to be published then.

In every case set `PLAINBOARD_PASSWORD` before the first start, so nobody else can claim the new
server. If port 8080 is taken, choose another one with `PLAINBOARD_PORT=9000` and point the proxy
there (inside the container it stays 8080).

What the proxy has to do:

- **Pass WebSockets.** Live updates, the browser window and previews use them. Without them the board
  loads but never changes by itself.
- **Keep quiet connections open**, an hour or more: a WebSocket on which nothing happens is not broken.
- **Let uploads through**, up to 50 MB.
- **Give PlainBoard a name of its own** (`board.example.com`), not a path under another site
  (`example.com/board/`): the previews at `/preview/<port>/` need the root of the name.

Caddy does all of that by itself:

```
board.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

nginx needs it spelled out:

```nginx
server {
    listen 443 ssl;
    server_name board.example.com;
    # ssl_certificate and ssl_certificate_key: your certificate

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1h;
        proxy_buffering off;
    }
}
```

If it does not work:

- **A TLS or handshake error** (Cloudflare: 525): the proxy has no entry for the name you opened, so
  it has no certificate for it. Add the name to the proxy's configuration and reload it.
- **502 Bad Gateway:** the proxy cannot reach PlainBoard. With the proxy on another machine,
  `PLAINBOARD_BIND` is missing (PlainBoard listens on `127.0.0.1` only), or the port is another one
  than the proxy points at. From the proxy's machine, `curl http://<address>:<port>/api/health` must
  answer `{"status":"ok"}`.
- **The board loads but never changes by itself:** WebSockets do not get through.

## Security

**Previews.** Previews open at `/preview/<port>/` on PlainBoard's address with no setup, for the
people with a role in the project and admins (or with a share). A preview page there runs as
PlainBoard, so it could use PlainBoard as whoever opens it. To keep agents' code apart from
PlainBoard, give previews a name of their own, one line in `.env`:

```bash
PLAINBOARD_PREVIEW_ORIGIN=https://preview.example.com
```

Point that name at the same PlainBoard (a second name in the proxy, see below). A preview link on
PlainBoard's address then forwards there, with a token that is good for two minutes and one port.
The preview origin keeps it as its own cookie for that port, which ends when you sign out of
PlainBoard. It serves previews and nothing else: neither the board, the API nor PlainBoard's login.
The proxy has to pass on the `Host` the browser asked for (Caddy and Cloudflare do; nginx needs
`proxy_set_header Host $host`), else PlainBoard can't tell the two names apart.

Without a name of their own, `PLAINBOARD_PREVIEW_SANDBOX=true` puts previews on PlainBoard's address
into the browser's sandbox (`Content-Security-Policy: sandbox`, without `allow-same-origin`). The page
then has an origin of its own: it can't read PlainBoard or its API, and has no `localStorage`,
`sessionStorage`, IndexedDB or cookies. The browser also sends no cookies, not even the login,
with what such a page loads. A shared preview needs no login, so it works as long as it needs no
storage. A private one shows its first page but loads nothing of its own (scripts, styles, hot
reload); dev servers open fully in the **Browser** view.

**HTTPS behind a reverse proxy.** PlainBoard takes a request as HTTPS when the proxy says so in
`X-Forwarded-Proto: https` (Caddy, nginx with the configuration above, and Cloudflare send it). Then:

- the login cookie is `Secure`, so the browser never sends it over plain HTTP. If your proxy does
  not send the header, set `PLAINBOARD_SECURE_COOKIES=true`. `false` never marks it Secure.
- every answer carries `Strict-Transport-Security: max-age=31536000`, so the browser uses HTTPS
  only for this name for a year. To leave it out (the name will be served over HTTP again, or
  your proxy sets HSTS itself), set `PLAINBOARD_HSTS=false`.

**Other sites.** A change to PlainBoard (any request but reading) is refused when the browser says it
comes from a page of another address (`Origin`, else `Referer`). The live updates and the browser
window open only for PlainBoard's own pages too. A request without either header (scripts,
`curl`, the public API with its key) is not affected. If another address of yours has to use the
signed-in API, name it: `PLAINBOARD_ALLOWED_ORIGINS=https://other.example.com` (comma separated).

**Signing out** ends the session on the server: a copy of the login cookie is of no use afterwards.
Changing your password signs out all other browsers.

**Attachments** open in the browser only if their content is a picture (PNG, JPEG, GIF, WebP), a
PDF or plain text. Everything else, HTML and SVG included, is downloaded. A single file may be up to
50 MB, a project file up to 4 GB. A project file is also refused if it holds more than 100,000
entries or unpacks to more than 16 GB.

**Example: Cloudflare in front** (as at `https://plainboard.example.com`). Add a second name, e.g.
`preview.example.com`, proxied by Cloudflare to the same PlainBoard as the first: a DNS record
plus, with a tunnel, a second public hostname with the same service. Then set
`PLAINBOARD_PREVIEW_ORIGIN=https://preview.example.com` and restart (`docker compose up -d`).
Cloudflare sends `X-Forwarded-Proto: https`, so the cookie is Secure and HSTS is sent with no
further setting. Under SSL/TLS, *Always Use HTTPS* keeps sending plain HTTP over to HTTPS.

With nginx, the preview name is a second `server` block like the one above with `server_name
preview.example.com`.

## PostgreSQL

PlainBoard keeps its data in one SQLite file in the data volume. That needs nothing and is the right
choice for most installations. If you would rather have the data in a PostgreSQL server (your own
backups and tools, a managed database), name it in `.env`:

```bash
PLAINBOARD_DATABASE=postgresql://plainboard:change-me@db:5432/plainboard
```

and, unless you have a server already, add one to your `docker-compose.yml`:

```yaml
  db:
    image: postgres:17
    restart: unless-stopped
    environment:
      POSTGRES_USER: plainboard
      POSTGRES_PASSWORD: change-me
      POSTGRES_DB: plainboard
    volumes:
      - db:/var/lib/postgresql/data
```

(with `db:` under `volumes:` at the end of the file, and the line `PLAINBOARD_DATABASE:
${PLAINBOARD_DATABASE:-}` in the app's `environment:` if your file is older and lacks it). Then
`docker compose up -d`: an empty database is set up by itself.

- **It starts empty.** To take projects from SQLite along, download each as a project file first
  ([Moving a project](#moving-a-project-to-another-plainboard)), switch, create your account, and
  bring the files in. Users, subscriptions and the PlainBoard settings are entered again.
- **The data volume stays.** The key your secrets are encrypted with and the uploaded files are
  still there, not in the database.
- **Reading with SQL** (the read access for analysis, `scripts/remote.py sql`) works with SQLite
  only. With PostgreSQL, ask the database itself.

## Preview

Web servers that agents start (dev servers, preview builds) open through PlainBoard at
`/preview/<port>/`, e.g. <http://localhost:8080/preview/5173/>: no ports to publish, and behind the
PlainBoard sign-in. Agents post the link in the chat. A preview is private: it opens for the people
with a role in the project whose agent started the server, and for admins. Ask the manager to share
one with a password or publicly for a few days, e.g. for a customer. The server has to listen on
`0.0.0.0`, and it has to be started as a server job (`start_job` with `server`) that still runs: a
port that no such job of a project listens on opens for nobody. A preview runs on PlainBoard's own
address unless it has one of its own ([Security](#security)), so only preview code you trust like
your agents.

Which previews run right now is always shown: in the sidebar under **Previews** (a row opens the
page in a new tab), and with everything about them on the start page of the **Browser** view: its
address, who started it and when, whether it is shared, and buttons to open it there, open it in a
tab or stop its server. On narrow screens a dot on **Browser** says that one runs. Nobody has to
name a port for this: PlainBoard sees which web servers the agents' running servers have opened.

The server believes it runs at the root of `localhost:<port>`, and PlainBoard repairs what goes
wrong because of the `/preview/<port>/` path: links and requests to the root, redirects, cookies,
WebSockets. One thing can't be repaired from outside: an app that routes in the browser (React
Router, Vue Router, Next.js) reads its own address and finds no page for `/preview/5173/`. Such an
app has to know its base path: the agent sets it to `/preview-base/<port>/` (e.g. `vite --base`,
`basePath` in Next.js) and gives you that link; agents are told to. Still shared between PlainBoard
and all previews is the browser's storage, and with two previews open at once a WebSocket to the root
goes to the one opened last.

## Browser window

Some apps don't work as a preview link, whatever is repaired: they sign in through a redirect to
`localhost`, or keep things in the browser's storage that then mix with PlainBoard's. For these there
is the **Browser** view: a real browser that runs next to the agents and is shown in PlainBoard.
There the page is at `localhost:<port>`, exactly as on the machine of someone developing it.

Enter an address like `localhost:5173` (or just the port), or click the window icon next to a
preview link in the chat. You see the page and use it with mouse, keyboard and touch; on phones the
keyboard button brings up the on-screen keyboard. Each project has one browser; what you sign in to
stays signed in. It is only for you, never for someone a preview is shared with.

What it is not: it is a picture of a page, so it is a little slower than a tab of your own browser
and soft while something moves (it turns sharp when the page rests), text can be pasted in but not copied out, and it does not download or upload files.
Where a preview link works, the link is the better way; the arrow at the right of the address opens
the same page as one.

The browser needs Chromium in the runner image (about 700 MB). To leave it out, set
`PLAINBOARD_BROWSER=0` in `.env` and rebuild.

## Public API

To show progress on a website, create a key under **Project settings → Public API**. Tickets with a
customer title are then available, with their customer texts and nothing else: no internal titles,
descriptions or comments. A project can have several keys, each with a label (e.g. one per website),
so you can revoke one without breaking the others; the list shows when each was last used.

```bash
curl -H "Authorization: Bearer pb_..." https://your-plainboard/api/public/tasks
```

```json
{"project": "Kiosk", "tasks": [{"number": 12, "title": "Faster search", "description": "Search finds products despite typos.", "status": "in_progress", "updated_at": "2026-10-30T09:00:00Z"}]}
```

`status` is `backlog`, `todo`, `in_progress`, `review` or `done`. The endpoint allows calls from any
website, and responses may be cached for 60 seconds. The server must be reachable from where you call
it (see [On a server](#on-a-server)).

## How work flows

```
You ──chat──▶ Manager ──delegate──▶ Agent A ─┐
  │              ▲  ▲                         │ report (kept with the ticket)
  └─ ticket ─────┘  └──── inbox / auto-wake ──┘
```

- Tickets are **never assigned to agents directly**. Handing a ticket to the manager (button or drag
  to *In Progress*) lets it plan the work and create **assignments**: clear, self-contained work
  orders for one or more agents.
- An assignment is one piece of work with one report: it stays open while a job the member started
  for it is running, and the report is sent when it is really finished, with the time it took (work
  against waiting). Reports go to the manager's inbox and stay with their assignment, which the
  ticket lists. The manager is woken up once per batch of reports (turn off per project under
  **Project settings → Team**), starts what comes next and moves the ticket to *Done* (*Review* only if you have to
  look at something).
- A ticket with several reports shows **where it stands**: done, open, decided, tried and dropped.
  PlainBoard writes that from the reports with the small model and gives it to every agent that
  works on the ticket, so nothing is done twice and nothing that failed is tried again.
- Comments are only what someone meant to say: yours, a decision, a branch that reached the main
  branch. Reports and repairs are not repeated there. A ticket that has collected ten assignments is
  no longer one piece of work: the manager is told so when it orders more, and splits the rest into
  smaller tickets.
- Finished branches land together. A member pushes its branch and hands it over
  (`merge_when_green`). PlainBoard, without any model, checks one train of branches per repository at
  a time, so check runs follow the clock instead of the number of changes. If a train fails, one
  member gets its branches and the failures as one assignment and makes them pass together; a branch
  that needs more goes back to its author. Trains are as long as pays: while they pass, up to twelve
  branches; when they fail, fewer, so one bad branch takes fewer with it. A repository's **quick check** (e.g.
  `cargo fmt --check && cargo clippy`) runs the moment a branch is handed over, so format and lint
  never cost a check run. A repository's **check command** (e.g. `./scripts/ci.sh`) decides whether a train may land;
  this works with any git host. Without one, on GitHub, the checks of one pull request for the train
  decide.
- **At once, checked afterwards** (per repository, see [Repositories and finished work](#repositories-and-finished-work)): for a development branch
  whose checks take long and often fail. Finished branches are merged at once, the checks run on
  the base branch afterwards, once for everything that landed, and if they fail one agent repairs
  the base branch. Nothing waits for a check run and a failure takes nothing back; the base branch
  can be red for a while, so don't use it for a branch you release from. While it is red, only its
  repair lands and the other finished branches wait, at most 45 minutes: the agents go on working,
  and the next check run shows whether the repair worked.
- **Branches don't pile up.** PlainBoard deletes a branch when it lands it. What it did not land
  itself (merged by hand, by an old pull request) a repository can have removed once a day
  (*Remove branches that are fully merged*, off by default): only branches whose commits are all on
  the base branch, never one with work of its own.
- Time is measured and shown to the agents where they decide (how long a command took, work against
  waiting per assignment), because a model has no clock.
- **Your decisions**: the manager can ask you on a ticket, e.g. to choose between two design
  proposals with screenshots. Pick an option with one click, answer in your own words, or discuss it
  in chat. The ticket is marked until you have decided.
- You can chat with any agent directly; the full-page **Chat** view lists all conversations.

## Token efficiency

- **Context per piece of work**: a member starts every ticket with a fresh context (system prompt,
  assignment, its notes) instead of carrying its whole history into every turn.
- **Session reuse**: within a piece of work, turns resume the Claude Code session, so prompt caching
  applies. Claude runs ask for the one-hour cache, because turns are often minutes apart.
- **Compression**: when an agent's context exceeds *Compress at (tokens)*, older messages are
  summarised with the *summary model* (Claude Haiku 5.5 by default). The session restarts with system
  prompt + summary + the last *N* messages verbatim. Chats can grow without limit; the UI always
  shows the full history. The limit must be below the model's context window (default: 350,000);
  otherwise Claude Code compacts first, using its own threshold. A lower limit makes every turn
  cheaper and faster.
- **Coordinator toolset** for the manager: a short custom prompt instead of the full Claude Code
  prompt, plus board tools and a shell for small tasks that would cost more to delegate.
- **Inbox instead of extra runs**: reports, job results and messages between agents are prepended to
  an agent's next turn; wake-ups are coalesced. Waiting for CI costs no turn at all.
- **Read access** (PlainBoard settings): a key that can read everything through the API and change nothing,
  for someone or something that analyses how your agents work. Chats, tickets, runs, the full log
  and the database (`/api/inspect/query?sql=select ...`) are readable with it, passwords and tokens
  are not. From a checkout of PlainBoard: put the address and the key into `.remote.env` and use
  `python scripts/remote.py` (see the top of that file).
- **The full log** (Statistics, download) is everything of the chosen period as it happened: every
  turn with what it was given and what it answered, every tool call, assignments, updates, jobs,
  landing and what PlainBoard decided by itself. It holds your project's texts and code (no
  passwords or tokens), so give it only to someone you would show the project.
- **The work report** (Statistics, download) shows where turns, tokens and time went, per day,
  trigger and account: the numbers to tune a team with.
- Compact tool schemas and results, effort per agent, titles-only note listings, pinned notes only
  when needed.

## The team, notes and chat

- **Team per project**: each agent has a role, a narrow area of responsibility, its own model,
  effort level and capabilities. Agents can ask each other (`ask_agent`) or message each other
  (`message_agent`).
- **Notes** are shared knowledge; pinned notes are in every prompt. One note per topic (similar
  titles are rejected in favour of updating), list views show each note's age and size, pinned notes
  have a fixed token budget, and the manager is reminded to consolidate as the collection grows.
- **Schedules** run one-off or by cron. A schedule can leave out a run when nothing happened in the
  project since the last one, so a status report costs no turn for "nothing new". **MCP servers**
  (stdio, http, sse) can be given to all or to selected agents.
- **Clean chat by default**: only messages, with events as one-line notes. The eye icon in the chat
  header shows agent activity (tool calls, compressions, context gauge, full event cards).
- **Responsive**: desktop with a sidebar, a resizable chat panel or a full-page chat; phones get a
  bottom navigation, the board's statuses stacked and foldable, and full-screen dialogs.
- **Designs and themes**: the palette button opens the choice. A design is the look: *Classic*
  (cards, rounded corners, an indigo accent; the default) or *Fahrplan* (ruled rows, one type size). Each comes
  light and dark, following your system until you choose. The choice is kept per browser. A design
  is one stylesheet and a theme one block of colour tokens, so adding your own is a small change
  (see "Add a design or a theme" in [ARCHITECTURE.md](../ARCHITECTURE.md)).
