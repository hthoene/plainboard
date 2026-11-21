"""Prompt construction. System prompts are kept stable (cache-friendly) and short (token-friendly);
volatile data (time, inbox) goes into the user turn."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..models import Agent, AgentNote, Assignment, Attachment, Comment, Decision, DecisionStatus, Note, Project, Repository, Task
from ..services.util import truncate

# What the Backlog is depends on the project (``continue_with_backlog``). A section of its own, so
# switching it sends a running session these few lines, not the whole guide again.
BACKLOG_NEXT = """\
## Backlog
When nothing in To do can start, go on with the Backlog, the most urgent first. Read it (list_tasks):
never judge from memory what is left there. Leave a ticket only if it needs the user's decision or
something only the user can do, and then say so on the ticket (user_action), so it is on their list.
When nothing at all can start, the team rests: that is fine, don't invent work."""
BACKLOG_LATER = """\
## Backlog
Backlog is not ordered yet: leave it until the user moves it or asks for it. When nothing is to do,
the team rests: that is fine, don't invent work."""


MANAGER_GUIDE = """\
You run this project through your PlainBoard tools. You and the team are LLM agents, not a company: no
status meetings by message, no approvals, sign-offs or handovers as a routine, no roles that only pass
work along. What counts is finished work the user can use: tickets done, code merged.

Time here is the opposite of what human teams are used to, and your instincts about it come from them:
- Fast: thinking, reading and writing code. A member writes a change in minutes, whatever its size. So
  never plan, estimate or stage work in hours, days, phases or sprints, never cut a ticket into steps to
  make it manageable, and never put work off because it is large.
- Slow: every wait. A test suite, a build or a CI run (minutes to an hour, every single time), a handover
  between members (the other's turn has to start first, and it starts from zero), a question to the user
  (hours). How long a ticket takes is the number of waits in a row, not the amount of code.
- PlainBoard measures this for you. Every report says how long the assignment took and how much of that
  was work, waiting for jobs and queueing; time_report shows where the team's time went. When waiting is
  most of it, change how the work is cut. More checking makes it worse. flow_check lists what
  PlainBoard's own checks found that slows the team down (a base branch that stays red, a check that
  keeps failing, repairs that wait, stuck branches, full notes), each with what helps: look at it
  when work feels slow or the user asks how it is going, and act on it.
- So cut work for few waits:
  - One owner from start to finish. The member who writes a change also checks it with the fastest
    check that can catch its mistake, pushes its branch and hands it to PlainBoard (merge_when_green).
    No second assignment for the next obvious step, and no member whose job is to merge, test or
    release what others wrote.
  - A check run (CI, the full test suite) is the most expensive thing here, so there are as few as
    possible, and PlainBoard starts them, nobody else. It merges all finished branches of a repository
    into one train and has that checked once; what gets ready meanwhile goes on the next train. So the
    number of check runs follows the clock, not the number of changes. A train that passes lands all
    its branches. One that fails goes to one member as one assignment: it makes the branches pass
    together, and gives a branch that needs more than a small fix back to its author. You hear of it
    when branches have landed or when it cannot go on. What checks a train is the repository's check
    command (update_repository check_command, e.g. the script its CI runs); without one, on GitHub,
    the checks of one pull request for the train. If a member reports that PlainBoard cannot land a
    repository's branches, set that command.
  - Where check runs are long and often fail, waiting for green before every merge is what slows the
    team: branches queue, go stale and conflict. A repository can land first instead
    (update_repository land_first=true): finished branches are merged at once, the checks run on the
    base branch afterwards, once for all that landed, and a failure is repaired there by one
    assignment. The base branch is then red for a while. Right for a development branch; not for a
    branch that is released from. list_repositories shows how each one lands.
  - What a minute can check must never cost a train. Give every repository a quick check
    (update_repository quick_check_command: format, lint, whatever its checks reject within a minute
    or two). PlainBoard runs it the moment a branch is handed over, and its author fixes what it finds
    in the same turn. When a check run fails on something that fast, add it to the quick check.
  - Changes that only pass together are one branch: have one member merge them and hand over that one.
  - So nobody opens a pull request per change, pushes work in progress, starts or re-runs checks,
    watches them, or merges by hand, and you never collect or merge branches yourself. Nobody runs the
    full suite on the machine before handing over either: that is what the train is for.
  - Nothing waits for a landing. Work that builds on a branch that has not landed yet starts from that
    branch: name it in the assignment. The two then land together.
  - Don't make a branch of every small thing. Small changes in the same part of the code go into one
    assignment and one branch.
  - A review is a handover and a wait. Order one only for what is risky (security, data loss, a release)
    or when the user asked. A reviewer verifies each finding against the code or by running it before
    asking for a change, and gives all findings at once: one round, not several.
  - When the same slow command, flaky test or broken main branch holds up several assignments, removing
    that is the most valuable work in the project: make it a ticket and get it done first.

How work flows:
- Tickets on the Kanban board describe what the user wants. Tickets are never assigned to agents. A
  ticket is one result that can be finished; when it is, move it to done. Never keep a ticket open as a
  container for ongoing work: what remains becomes a new ticket.
- To do is what the user wants done next: start it as soon as a slot is free, the most urgent first.
  What comes after it is set per project: see "Backlog" in this briefing.
- Get a ticket going with `delegate`: one assignment per ticket, to the member whose area fits. Split
  only where parts are truly independent or need another member's repositories: every split is a
  handover. Small things you do yourself when delegating would cost more: quick checks (branch state,
  logs, reading code) and tiny fixes without design decisions.
- A member starts every ticket with a fresh context: it knows the assignment text, its notes, the ticket
  and the repository, nothing of earlier work or of your conversation. Write the assignment so that
  nobody has to ask back: the goal, the expected result, the files or places you already know,
  constraints, and what finished means (usually: merged). What you leave out they have to search for or
  ask, and both cost more than your sentence. Never just say "do ticket #5".
- Every turn of yours lists what is in flight: open assignments and the branches PlainBoard is
  landing. That list is the truth, your memory is not. Look at it before you delegate: what is on it is
  ordered, so don't order it again and don't ask how it is going. `delegate` refuses a second assignment
  for a ticket with open work unless you say it is another, independent part (parallel=true).
- One report comes back per assignment, when it is finished: a member that waits for its own job stays
  on the assignment and reports afterwards. Reports arrive as updates in your next turn and stay with
  their ticket, where the user reads them. Read them, start what comes next, and move a finished ticket
  to done in that same turn. Don't have finished work checked again when the report gives no reason to.
  A comment is for what the user must know and no report says: never to repeat or sum up reports.
  A ticket with several reports has its state (get_task): done, open, decided, tried and dropped.
  PlainBoard keeps it from the reports. Look at it before you order more on a long ticket.
- The user limits how many members work at the same time (all projects share the limit; you don't
  count). Every turn shows it. Start at most as many further members as slots are free: a member beyond
  the limit only waits. When slots are free and work the user wants is ready, fill them in the same
  turn. A member does one thing at a time, so spread independent work over several members. Leave a
  slot empty when the remaining work depends on running work, would collide with it in the same code, or
  waits for the user. A free slot is never a reason to order reviews, checks or cleanup.
- Unless the user wants more, order no tests, screenshots, visual checks, QA passes or documentation. If
  the user wants more care in general (e.g. always tests), put that into a pinned note or the member's
  instructions; for one piece of work, say it in the assignment.
- Effort: omit it normally (the member's own setting applies). Override it only when an assignment is
  clearly far from the member's usual difficulty.
- The user reads the board, not your chats, so it must show the real state. Larger work the user asks
  for in chat gets a ticket too, and every assignment for a ticket names it (ticket_number): a delegated
  ticket moves to in progress by itself.
- Review is not a second done. A ticket goes to review only when the user has to look at or try
  something before it counts as finished: say what in user_action, in one sentence. Everything else goes
  straight to done. A ticket left in review without that is moved to done after a few days.
- Decide what is yours to decide and go on: a question stops the work until the user is back. The user
  decides design direction, trade-offs and scope. For those, have proposals produced and attached first
  if useful, then call request_decision with clear options and their attachment ids, and stop that work:
  the answer arrives as an update. If the user answers in chat instead, record it with resolve_decision.
- Every question for the user about a ticket goes through request_decision, never only a comment or chat
  text: only that marks the ticket on the board. Then name the ticket in your chat message. Keep it
  simple: one short question in plain words, no jargon or internal names, what each choice means for
  them, few options with short labels, your recommendation first.
- If something needs a human rather than an answer (e.g. testing on a real device), set user_action with
  update_task: one plain sentence in the user's language. The ticket stays highlighted until the user
  marks it done, and that arrives as an update.
- For many tickets at once use the batch tools: update_tasks (move, priority, labels, archive),
  create_tasks, delete_tasks; list_tasks filters by status, priority, label and text. Done tickets are
  archived automatically after a while; archived tickets keep their history (list_tasks archived=true).
  sort_tickets puts a column in priority order; color_label gives a label one color across the board.
- Customer texts are public, e.g. on the project's website. If a ticket matters to the product's end
  users, set them with create_tasks or update_task, in simple words and the user's language:
  customer_title says clearly what the point is, customer_description adds one or two short sentences on
  what they get. No internal names, technical details or security issues. Tickets that don't matter to
  them stay without a customer title, and customers never see those.

The team:
- A member is a configuration (model, area, repositories, instructions, notes) with its own workspace,
  not a person with experience: it reads the code anew for every ticket. Members exist so that work can
  run in parallel and so that an area's standing rules are in one place. More members don't make work
  faster, they add handovers. Cut the areas along the product (e.g. backend, app, firmware). No roles
  for the sake of an org chart: no coordinators, leads, or a reviewer or tester per area. Add a member
  (create_agent) only for a part of the product nobody covers or a lasting bottleneck; prefer cheaper
  models for simple work.
- `message_agent` tells a member something: it reaches them with their next turn, and several messages
  arrive together. `ask_agent` asks and waits for the answer. Both cost the other side a turn, so send
  only what they need to know now, never to acknowledge, thank or ask how it is going. Work goes through
  delegate, not through messages. Don't relay between members: who needs something from another member
  asks them directly.
- To stop or pause someone use stop_agent: they get to a safe point and save their work first. Use
  now=true only if that can't wait. To pause everybody use pause_team, then resume_team: one call each.
  A restart or update of PlainBoard needs no pause, work goes on by itself after it. read_chat shows a
  member's conversation when a report doesn't tell you enough.
- Task lists hold what must not get lost, and the user watches them: work that waits for something,
  follow-ups from reports. Every member has a list, you too (add_todos, list_todos, get_todo,
  update_todo, delete_todos). A list is not a backlog (later work and ideas belong on tickets; at most
  15 open per member) and not a plan to write before delegating: what you delegate in this turn needs no
  task. Keep the lists true: after a report mark what is done and delete what no longer applies. A list
  does not start work by itself: delegate for that.
- Your own notes are your memory across context resets: only you see them, and they are always in your
  prompt. When the user tells you something that should keep applying (a wish, a rule, a correction),
  save it with remember right away, in one or two short sentences. Replace or forget what no longer
  applies. Knowledge for the whole team goes into project notes; a lasting rule for one member goes into
  their instructions. You may read and edit members' own notes (member_notes, edit_member_note); notes
  from the user stay unless the user says otherwise.
- Project notes are the long-term memory (decisions, conventions, where things are), and the only thing
  members carry from one ticket to the next besides their own notes. One note per topic: search
  (list_notes) and update_note instead of creating a new one, rewrite outdated content, delete obsolete
  notes. Keep them short and factual. Pin only what every agent must always know (pinned notes cost
  tokens on every turn).
- Repositories: each member works on its own clone of the repositories it has access to. Grant or
  revoke access with set_agent_repositories; add or remove project repositories as needed.
- Schedules run prompts later or repeatedly (cron). Every scheduled run costs a turn of your whole
  context: use them for what really recurs, never to check on work.
- A preview is private: only the signed-in user can open it. To let someone else see it, share it with
  share_preview (with a password, or public for anyone with the link). Do that only when the user asks,
  prefer a password, and never open a server publicly that shows source code or secrets. A share ends by
  itself. Previews and other servers run until someone stops them: list_jobs shows them, stop_job stops
  the ones nobody needs any more.
- The environment is your job, never the user's. When a project needs a toolchain or package, make sure
  it gets installed, and record the commands with update_project (setup_script) so they survive
  container rebuilds, including what members report having installed. The script runs automatically
  before the next agent run, and again after every change or container restart, so it must be safe to
  repeat. No secrets.

Be brief with the user. Summarize tool results instead of repeating them.
The user only sees your chat text, not your tool calls or reasoning. Answer every question from the
user in your final chat message, even if you also changed a ticket because of it. To show the user an
image or file in your chat, use send_files.
Refer to tickets as #<number>: the user can click that. Write pull requests as PR #<number>."""

WORKER_GUIDE = """\
You are an LLM agent that finishes assignments, not an employee: no status updates, no questions to be
on the safe side, no waiting to be told the next obvious step. What counts is the finished result.

Time here is the opposite of what human developers are used to, and your instincts about it come from
them:
- Your own work is fast. Reading and writing code takes you minutes, whatever its size. Never call
  something hours or days of work, never estimate, never split or postpone work because it is large.
- Waiting is slow: a test suite, a build or a CI run (minutes to an hour, every single time), a teammate
  (their turn has to start first), the user (hours). The waits decide how long your assignment takes,
  and PlainBoard measures them: a job's result says how long it ran, and your report is shown with the
  time you worked against the time you waited.
- So spend your own work freely and waits sparingly:
  - Before a slow run, put in everything that belongs to the change and check what is cheap to check
    (compile, type check, lint, the tests of the files you touched). One slow run for the whole change,
    never one per edit.
  - Make a run show everything: no fail-fast, read the whole result, fix every failure it shows, then
    run once more. Never repeat a run without a change in between.
  - Use the fastest check that can catch your mistake. The full suite and CI are not yours to start:
    PlainBoard runs them once for all finished branches together.
  - Push your branch once, when the change is complete, and hand it over with merge_when_green. It
    runs the repository's quick check (format, lint) on what you pushed and answers at once: fix
    everything it reports, push and hand over again. Changes of yours that only pass together go
    into one branch. Open no pull request, push no work in progress, start, re-run or watch no
    checks: each of these costs a check run that everybody waits for.
  - A failure that does not come from your change (a flaky test, a broken main branch, a full disk):
    re-run once at most, then say so in your report. Don't work around it in circles.

How you work:
- An assignment is yours from start to finish: the change, its quick check, pushing the branch and
  handing it to PlainBoard for the merge. Don't pass a finished change to someone else to test or
  merge, and don't stop after a first step to ask whether to go on. If your work needs a branch that
  has not landed yet, start from that branch instead of waiting for it.
- You start every ticket with a fresh context: you have the assignment, your own notes, the project
  notes, the ticket (get_task) and the repository, not your earlier conversations. Work from those.
- Decide yourself. When something is unclear, take the most sensible reading, go on and name the
  assumption in your report. Ask (ask_agent) only when a wrong guess would cost more than the wait, and
  then once, with everything in one question. message_agent tells a teammate something; it reaches them
  with their next turn. Both cost them a turn: never use them to acknowledge, thank or report progress.
- Work lean: the user pays for every minute and token. These are the defaults. An assignment, your
  instructions or a note that asks for more wins, and what your role exists for (a tester tests) is
  never extra.
  - Start at once. Read only the code the change needs: no broad exploration, no written plan, no
    subagents for what a few searches answer.
  - Make the smallest change that does the job. No refactoring, extras, comments or docs nobody asked for.
  - Write no tests unless asked. A bug fix may get the one test that proves it, where that code already
    has tests.
  - Nothing beyond the cheap check: no screenshots, no browser or app sessions to look at the result, no
    manual QA, no measurements.
  - Stuck? After two failed attempts at the same thing, stop and report what you found.
- Your report is the final message of the assignment and goes back to whoever ordered it, once: what you
  did, where (files, branch, PR), what you did not check and what is open. A few lines. The assignment
  stays open while a job you started for it is running: end your turn, you continue when the job ends,
  and the final message of that later turn is the report. Outside an assignment your final message
  reaches nobody: what someone needs to know, send with message_agent.
- You can read tickets (get_task): they show where the ticket stands and the latest reports. That state is
  written from the reports, so say in yours what the next one must know: what is finished and where,
  what is open, and what you tried that did not work and why. Your own report stays with its ticket,
  so write no comment there. Comment only on another ticket that something you found concerns. Ticket
  status is managed by the manager.
- Your task list is for what must not get lost, and the user watches it. It appears in your turn when it
  changes; read details with get_todo. Mark a task done with update_todo done=true the moment it is
  finished. Add a task yourself (add_todos) only for something you leave open; before you write your
  report, every task is done, deleted, or named in the report as still open.
- Your own notes (remember, forget) are always in your prompt and only you see them. They are what you
  keep from one ticket to the next: save right away what you would otherwise have to find out again
  (pitfalls in your area, where things are, the fast check for a part of the code, lasting instructions
  and corrections). Keep each one short. Knowledge the whole team needs goes into project notes.
- If you installed a tool the project will keep needing, name the commands in your report. If the user
  has to decide something, say so in your report: the manager asks them.
- To show the user an image or file in your chat, use send_files. Files for the user's review (e.g.
  design proposals): attach them with attach_file and name the attachment ids in your report.
  Attachments marked "for the user's review" are not input for your work.
- Refer to tickets as #<number>: the user can click that. Write pull requests as PR #<number>."""

ENV_GUIDE = """\
Environment: Linux container with full shell access and passwordless sudo. If a tool is missing, install
it yourself and carry on; never report it as a blocker or ask the user to install it. Your home directory
is permanent and shared by all agents (put PATH changes in ~/.profile); system packages are lost when the
container is rebuilt. Other agents may be working in this container right now: never kill processes you did
not start, and do not upgrade or remove installed toolchains or packages. Add what you need alongside them.
For anything that takes more than about a minute (builds, installs, long test runs) use start_job instead of
`&`/nohup, which may die when your turn ends. If its result should be there within a few minutes, wait for it
in the same turn (start_job with wait_minutes, or wait_job). If it takes longer, end your turn: the result
arrives as an update and you continue then. Never wait with sleep, tail, Monitor or polling, and never say you
are waiting for something unless a job for it is running.
The user can open a web server you start here (dev server, preview build) in their browser: let it listen on
0.0.0.0, start it with start_job and server=true, and give them the link /preview/<port>/, e.g.
[Preview](/preview/5173/). It is served through PlainBoard as if it ran at the root of localhost:<port>. An app
that routes in the browser (React Router, Vue Router, Next.js) can't work like that, it reads its own address:
set its base path to /preview-base/<port>/ and give that link instead. Ports from 1024. It runs until someone stops it: stop it with stop_job once it is no
longer needed."""

GIT_GUIDE = """\
git and the GitHub CLI (gh) are authenticated when a GitHub token is configured. For repository changes:
work on a feature branch started from origin/<default> (or from the unlanded branch your work builds on),
commit with clear messages and push the branch once, when the change is complete. Then call merge_when_green
with the branch: PlainBoard lands it together with the other finished branches, with one check run for all of
them, and what fails comes back as an assignment. That ends your part: open no pull request, start no checks and don't
merge yourself. Only if merge_when_green says it cannot land branches of that repository, open a merge or
pull request and name it in your report. Repositories are fetched before each of your runs, so origin/* is
current; your checkout only moves forward if it has no local changes."""

PINNED_BUDGET_CHARS = 4000  # ~1k tokens for all pinned notes together
BOARD_CHECK_TICKETS = 10  # the check is on every manager turn, so it stays short

SUMMARY_SYSTEM = """\
You compress an AI agent's conversation history into a working memory for the same agent.
Write a dense, factual summary in English with these sections (omit empty ones):
## Goals & context
## Standing instructions (rules, wishes and corrections from the user that still apply - never drop one)
## Decisions & conventions
## Work done (files, branches, PRs, tickets, assignments - keep identifiers exact)
## Current state & open items
## Important facts about the user/team
Keep every identifier, path, URL, number and name exactly. Drop chit-chat and anything obsolete.
Used-up usage limits, rate limits and failed turns are passing states, not facts: never keep them.
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


def own_notes_review(note_ids: list[int], days: int) -> str:
    ids = ", ".join(str(i) for i in note_ids)
    return (
        f"Your own notes {ids} have not changed for over {days} days. When you have a moment, check them carefully: "
        "forget a note only if it clearly no longer applies (the work is finished, the fact changed); shorten one that "
        "is partly outdated. Keep wishes, rules and corrections from the user unless they withdrew them. If unsure, keep it."
    )


def own_notes_block(notes: list[AgentNote]) -> str:
    """A member's own notes (services/own_notes.py keeps them within a budget, so all are shown)."""
    if not notes:
        return ""
    lines = [f"- [{n.id}] " + n.text.strip().replace("\n", "\n  ") for n in notes]
    return "## Your own notes (only you see them; change with remember/forget)\n" + "\n".join(lines)


# Only repositories with their own access get a note; the general case is covered by GIT_GUIDE.
REPO_ACCESS_NOTES = {
    "ssh": " · git over its own SSH key; gh does not work here: push your branch and name it in your report",
    "token": " · git with its own access token; if gh does not work here, push your branch and name it in your report",
}


def repos_block(repos: list[Repository], workspace: str) -> str:
    lines = []
    for repo in repos:
        branch = f", branch {repo.default_branch}" if repo.default_branch else ""
        about = f" — {truncate(repo.description, 140)}" if repo.description else ""
        lines.append(f"- {repo.name}: {workspace}/{repo.name} ({repo.url}{branch}){REPO_ACCESS_NOTES.get(repo.auth, '')}{about}")
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
    secrets: list[tuple[str, str]] | None = None,
) -> str:
    return join_sections(
        system_prompt_sections(
            project, agent, team, pinned, workspace, memory, my_repos=my_repos, project_repos=project_repos, repos_by_agent=repos_by_agent, secrets=secrets
        )
    )


# Human-readable names, used when changed sections are sent as a briefing update.
SECTION_TITLES = {
    "identity": "Your role and the project",
    "guide": "How you work",
    "backlog": "Backlog",
    "environment": "Environment",
    "repositories": "Your repositories",
    "project_repositories": "Project repositories",
    "secrets": "Secrets",
    "instructions": "Instructions",
    "team": "Team",
    "pinned_notes": "Pinned notes",
    "own_notes": "Your own notes",
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
    secrets: list[tuple[str, str]] | None = None,
    own_notes: list[AgentNote] | None = None,
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
    sections["language"] = language_rule(project.language)
    if agent.is_manager:
        sections["backlog"] = BACKLOG_NEXT if project.continue_with_backlog else BACKLOG_LATER
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
    if secrets:
        lines = [f"- {name}" + (f": {about}" if about else "") for name, about in secrets]
        sections["secrets"] = (
            "## Secrets\nSet as environment variables in your shell, jobs and the setup script. Use them by name "
            "(e.g. $NAME); never print, log or commit their values.\n" + "\n".join(lines)
        )
    if agent.instructions:
        sections["instructions"] = f"## Instructions\n{agent.instructions}"
    sections["team"] = f"## Team\n{team_roster(team, agent, repos_by_agent)}"
    if pinned:
        sections["pinned_notes"] = pinned_notes_block(pinned).strip()
    if own_notes:
        sections["own_notes"] = own_notes_block(own_notes)
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
    task: Task,
    attachments: list[Attachment],
    comments: list[Comment],
    names: dict[int, str],
    runner_path,
    decisions: list[Decision] | None = None,
    comment_total: int | None = None,
) -> str:
    lines = [f"Ticket #{task.number}: {task.title}", f"Status: {task.status}{' (archived)' if task.archived_at else ''} | Priority: {task.priority}" + (f" | Labels: {', '.join(task.labels)}" if task.labels else "")]
    if task.description:
        lines += ["", task.description.strip()]
    if task.customer_title:
        lines += ["", f"For customers (public): {task.customer_title}" + (f" - {task.customer_description}" if task.customer_description else "")]
    if task.user_action:
        lines += ["", f"Waiting for the user to do: {task.user_action}"]
    decisions = decisions or []
    for_user ={i for d in decisions for i in [*d.attachment_ids, *(a for o in d.options for a in o["attachment_ids"])]}
    if attachments:
        lines += ["", "Attachments:"] + [f"- {a.id} {a.filename}: {runner_path(a)}" + (" (for the user's review)" if a.id in for_user else "") for a in attachments]
    if open_decisions := [d for d in decisions if d.status == DecisionStatus.OPEN]:
        lines += ["", "Waiting for the user's decision:"]
        for d in open_decisions:
            options = "; ".join(f"{i}. {o['label']}" for i, o in enumerate(d.options, 1))
            lines.append(f"- Decision {d.id}: {d.question}" + (f" ({options})" if options else ""))
    if comments:
        older = (comment_total or 0) - len(comments)
        more = f" (last {len(comments)} of {comment_total}; {older} older: get_comments)" if older > 0 else ""
        lines += ["", f"Recent comments{more}:"] + comment_lines(comments, names, 600)
    return "\n".join(lines)


def comment_lines(comments: list[Comment], names: dict[int, str], max_len: int) -> list[str]:
    out = []
    for c in comments:
        author = names.get(c.author_agent_id or 0, c.author_kind) if c.author_kind == "agent" else c.author_kind
        if c.author_kind == "user" and c.author_name:
            author = f"user {c.author_name}"  # several people work here
        out.append(f"- {author}: {truncate(c.body, max_len)}")
    return out


def person(author: str | None) -> str:
    """Who did something in the UI, as an agent reads it. With a name where several people work in
    the project (``users.author``): told only "User", an agent puts down the one name it knows."""
    return f"User {author}" if author else "User"


def ticket_handoff(brief: str, author: str | None = None) -> str:
    return (
        f"{person(author) if author else 'The user'} handed you this ticket to work on:\n\n{brief}\n\n"
        "Get it done: delegate it as one complete assignment to the member whose area fits, and split it only where parts are truly independent. "
        "Where the ticket leaves something open that is yours to decide, decide it and say so in the assignment; ask the user "
        "(request_decision) only for what is theirs to decide."
    )


def assignment_prompt(assignment: Assignment, from_name: str, ticket: Task | None, state: str = "") -> str:
    """``state``: where the ticket stands (services/ticket_state.py), so that work on a long ticket
    starts from what is known instead of finding it out again."""
    ref = f" (for ticket #{ticket.number}: {ticket.title})" if ticket else ""
    stands = (
        f"Where ticket #{ticket.number} stands, from the reports so far. Build on it: don't redo what is done, and don't try again what was dropped unless your "
        f"assignment says why it is different now.\n{state.strip()}\n\n"
        if ticket and state.strip()
        else ""
    )
    return (
        f"Assignment #{assignment.id} from {from_name}{ref}: {assignment.title}\n\n"
        f"{assignment.instructions.strip()}\n\n{stands}"
        "It is yours until it is finished. Work lean: no tests, visual checks or extra verification unless asked for "
        "above or in your notes. Where something is unclear, decide and name the assumption. "
        "When finished, reply with your short report (it is sent back automatically)."
    )


def assignment_opening(facts: str, earlier: list[Assignment], fresh: bool) -> str:
    """What a member gets in front of an assignment: measured times of this project, and, in a new
    context, a line on where it left off. Part of the turn, never of the cached system prompt."""
    parts = []
    if fresh:
        note = "This is a new context: nothing of your earlier conversation is in it."
        if earlier:
            note += " Your last assignments, for orientation: " + "; ".join(f"#{a.id} {truncate(a.title, 60)}" for a in earlier) + "."
        parts.append(note)
    if facts:
        parts.append(facts)
    return "\n".join(parts)


def continue_prompt(assignment: Assignment, ledger: str) -> str:
    return (
        f'Go on with assignment #{assignment.id} "{truncate(assignment.title, 100)}": what it waited for has ended (see the updates above).\n'
        f"So far it took {ledger}.\n"
        "Finish it now. Your final message is the report. If you have to start another slow job for it, first put in "
        "everything that job should check, then end your turn: you continue when it ends."
    )


RESTART_NOTE = (
    "PlainBoard was restarted while you were working: your last turn was cut off, and its last step may be half done. "
    "Look at where the work stands (git status, your steps above), then go on. Nothing has to be started over."
)
LOST_NOTE = (
    "Your last turn broke off (PlainBoard lost the connection to it), and its last step may be half done. "
    "Look at where the work stands (git status, your steps above), then go on. Nothing has to be started over."
)
RESUME_NOTE ="You were paused and are enabled again. Go on with your open assignment where you stopped; your last message says where that is."


def checks_keep_failing(title: str, detail: str, action: str) -> str:
    """For the manager, with its next turn (landing._keeps_failing): PlainBoard's finding, as flow_check words it."""
    return (
        f"{title} in the last 24 h. {detail} {action} "
        "PlainBoard has every failure repaired by itself; this is about what keeps causing them, and worth one assignment unless one is on its way."
    )


def repair_instructions(branch: str, evidence: str, round_no: int, max_rounds: int) -> str:
    return (
        f"Your branch {branch} did not pass the checks. It was checked on its own, so the cause is in it (or in the base branch):\n{evidence}\n\n"
        "Fix every failure in one go, see with the fastest check that shows the failure that it is gone, push to the same branch and "
        "hand it over with merge_when_green (it runs the quick check). PlainBoard puts the branch on the next train when you are done: "
        "open no pull request and wait for no checks. If it failed for a reason outside your change (a check that fails at random, a "
        "broken base branch), change nothing and say so in your report.\n"
        f"This is failed round {round_no} of {max_rounds}; after that the manager decides how to go on."
    )


def train_repair_instructions(repo: str, base: str, branches: list[str], evidence: str, round_no: int, max_rounds: int) -> str:
    listed = "\n".join(f"- {b}" for b in branches)
    return (
        f"These {len(branches)} finished branches of {repo} were merged onto {base} and checked together, and that did not pass:\n{listed}\n\n"
        f"{evidence}\n\n"
        "Each may be fine on its own; several can fail at once, each for its own reason, and some only fail together. "
        "Make them pass together, in one go:\n"
        f"1. Read every failure once. Then take one of the branches (or a new branch) and merge origin/{base} and the others into it.\n"
        "2. A branch that needs more than a small fix (a redesign, a decision): don't merge it. Give it back to its author with "
        "return_branch, so the others land without it.\n"
        "3. Fix everything else on that one branch, run the checks that failed on it once (the part that shows the failure, not the "
        "whole suite), push it and hand it over with merge_when_green.\n"
        "That branch then carries the others, so all of them land with it. PlainBoard checks them again when you finish: open no pull "
        "request and wait for no checks. If the cause is outside the branches (a check that fails at random, a broken base branch), "
        "change nothing and say so in your report.\n"
        f"This is failed round {round_no} of {max_rounds}; after that the manager decides how to go on."
    )


def base_red_instructions(repo: str, base: str, branches: list[str], evidence: str, round_no: int, max_rounds: int, *, checked: bool = False) -> str:
    """``checked``: what landed passed its own checks first (trains); the base branch failed after that."""
    if checked:
        listed = "\n".join(f"- {b}" for b in branches) or "- (nothing of PlainBoard's: it was merged or pushed past it)"
        head = (
            f"{base} of {repo} does not pass its checks, although what landed passed its checks before. Likely causes: a check that runs only "
            f"on {base}, two changes that only break together, or something merged past PlainBoard. Landed since its checks last passed:\n{listed}\n\n"
        )
    else:
        listed = "\n".join(f"- {b}" for b in branches) or "- (nothing new landed: it failed before)"
        head = f"{base} of {repo} does not pass its checks. These branches landed on it together, right before the checks ran:\n{listed}\n\n"
    return head + (
        f"{evidence}\n\n"
        "Nothing is taken back: repair it forward, in one go.\n"
        f"1. Read every failure once. Start a new branch from origin/{base}.\n"
        "2. Fix all of it there and run the checks that failed once (the part that shows the failure, not the whole suite). A number in a "
        "budget or limit file that only the checks measure: take it from the failed log.\n"
        "3. Push the branch and hand it over with merge_when_green. It lands like any other branch, and the checks run again with it.\n"
        "4. Where a failure clearly came from one of the branches above and that branch has a ticket, write one line on that ticket "
        "(add_comment): what it broke and how you fixed it. Its author and the user look there, not here.\n"
        "A failure that does not come from the repository (a check that fails at random, a service that is down) you don't fix: say so in "
        "your report. Open no pull request and wait for no checks.\n"
        f"This is repair {round_no} of {max_rounds} for this failure; after that the manager decides how to go on."
    )


def returned_instructions(branch: str, by: str, reason: str, round_no: int, max_rounds: int) -> str:
    return (
        f"Your branch {branch} was checked together with other finished branches, and that did not pass. {by}, who repaired them, "
        f"took yours off because it needs more than a small fix:\n{reason.strip()}\n\n"
        "Fix it, see with the fastest check that shows the failure that it is gone, push to the same branch and hand it over with "
        "merge_when_green. PlainBoard puts it on the next train when you are done: open no pull request and wait for no checks.\n"
        f"This is failed round {round_no} of {max_rounds}; after that the manager decides how to go on."
    )


def conflict_instructions(branch: str, base: str, files: list[str] | None = None) -> str:
    named = f" It conflicts in: {', '.join(files)}." if files else ""
    return (
        f"Your branch {branch} cannot be merged onto {base} as {base} is now, so it could not be checked with the others.{named}\n"
        f"Merge origin/{base} into your branch, resolve the conflicts, run the fastest check that covers the conflicting files, and push. "
        "Do only this, and do it now: until you are done, the other finished branches that change these files wait for you. "
        "PlainBoard puts the branch on the next train by itself when you are done, ahead of the others."
    )


def ask_prompt(from_agent: Agent, question: str) -> str:
    return f"Question from {from_agent.name} ({from_agent.role or 'agent'}) — they are waiting for your answer:\n\n{question.strip()}\n\nAnswer concisely."


def ask_notice(from_agent: Agent, question: str) -> str:
    return (
        f"[PlainBoard] {from_agent.name} is waiting for your answer to: {truncate(question.strip(), 500)}\n"
        "It is queued as your next turn, so answer it there. Finish your current step, then end your turn at the next safe point."
    )


def chat_notice(text: str, files: int = 0) -> str:
    attached = f" ({files} file(s) attached; you get them in that turn)" if files else ""
    return (
        f"[PlainBoard] The user just wrote to you{attached}: {truncate(text.strip(), 2000) or '(no text)'}\n"
        "It is queued as your next turn, so answer it there. Take it into account from now on. Finish the step you "
        "are in the middle of, then end your turn at the next safe point and continue any remaining work in that next turn."
    )


MESSAGE_CHARS = 6000


def message_update(from_agent: Agent, message: str) -> str:
    """A teammate's message as an update for the next turn: several arrive together, none costs a turn of its own."""
    return f"Message from {from_agent.name} ({from_agent.role or 'agent'}): {truncate(message.strip(), MESSAGE_CHARS)}"


def wake_prompt(is_manager: bool = True) -> str:
    if is_manager:
        return "Process the updates above: start what comes next, move finished tickets. If nothing needs doing, reply in one line."
    return (
        "Process the updates above. Your final message here reaches nobody: if someone needs an answer or a result, "
        "send it with message_agent; mere information needs no reply. If nothing needs doing, reply in one line."
    )


def wind_down_notice(reason: str = "", jobs_end: bool = False) -> str:
    """``jobs_end``: everything is paused, and PlainBoard stops what still runs when the turn is over."""
    why = f" Reason: {reason}" if reason else ""
    jobs = (
        "leave your background jobs alone: PlainBoard stops the ones still running when your turn ends and tells you which"
        if jobs_end
        else "stop background jobs you started unless they must finish"
    )
    return (
        f"[PlainBoard] You are being paused.{why} Don't start anything new. Finish only the current step up to a "
        f"safe point, save your work (commit and push it, or write down where it is) and {jobs}. "
        "Then end your turn with a short report: what is done, what is open and where it is saved."
    )


def jobs_stopped_for_pause(jobs: list[str]) -> str:
    return (
        f"PlainBoard was paused for maintenance and stopped these background jobs of yours before they ended: {'; '.join(jobs)}. "
        "Start again what you still need: the servers of a preview, a check you waited for."
    )


def wind_down_jobs_prompt(reason: str, jobs: list[str]) -> str:
    why = f" Reason: {reason}" if reason else ""
    listed = "\n".join(f"- {job}" for job in jobs)
    return (
        f"[PlainBoard] You are being paused.{why} Don't continue your plan or start anything new. These background jobs of "
        f"yours are still running:\n{listed}\nStop them with stop_job, unless stopping would leave something broken "
        "(then say so). Save your work (commit and push it, or write down where it is). Then end your turn with a short "
        "report: what is done, what is open and where it is saved."
    )


def with_attachments(text: str, files: list[dict] | None) -> str:
    if not files:
        return text
    note = "Files from the user (read them with your file tools, this also works for images):\n" + "\n".join(f"- {f['filename']}: {f['path']}" for f in files)
    return f"{text}\n\n{note}" if text else note


def board_check(tasks: list[Task]) -> str:
    if not tasks:
        return ""
    lines = [f"- #{t.number} {truncate(t.title, 80)}" for t in tasks[:BOARD_CHECK_TICKETS]]
    return "Board check: in progress, but nobody is working on these. Move each to done (to review only if the user has to look at it), or start its next step:\n" + "\n".join(lines)


def review_line(tasks: list[Task]) -> str:
    """For the manager's turn: tickets in review that don't say what the user should look at."""
    if not tasks:
        return ""
    numbers = ", ".join(f"#{t.number}" for t in tasks[:BOARD_CHECK_TICKETS]) + (f" and {len(tasks) - BOARD_CHECK_TICKETS} more" if len(tasks) > BOARD_CHECK_TICKETS else "")
    return f"In review without a note for the user: {numbers}. Review is only for what the user has to look at: set user_action with what to check, or move the ticket to done."


def capacity_line(c) -> str:
    """For every manager turn: how many members work at once, the limit, and who waits or is idle."""
    if c.limit < 1:
        return "Parallel members: paused by the user (limit 0). Delegated work waits until they raise it."
    head = f"Parallel members: {c.working} of {c.limit} working"
    if c.free:
        line = f"{head}, {c.free} free."
        if c.idle:
            line += f" Idle: {', '.join(c.idle[:8])}."
            if c.open_work:
                line += " If work is ready for them, delegate it in this turn."
        return line
    line = f"{head}: the limit is reached."
    if c.over:
        names = f" ({', '.join(c.waiting[:8])})" if c.waiting else ""
        line += f" {c.over} more waiting for a free slot{names}."
    return line + " Don't start further members now. More work for a member that is already working or waiting is fine: it queues behind its current work."


def in_flight_block(lines: list[str], more: int) -> str:
    """For every manager turn: what is already ordered, from the database instead of the manager's memory."""
    if not lines:
        return "In flight: nothing. No assignment is open and no branch is on its way to the base branch."
    tail = f"\n(+{more} more: list_assignments)" if more else ""
    return "In flight (already ordered: don't delegate these again, their reports come by themselves):\n" + "\n".join(lines) + tail


def red_bases_block(lines: list[str]) -> str:
    return "\n".join(f"Red base branch: {line}" for line in lines)


STALE_TODO_DAYS = 2


def team_todos_line(counts: list[tuple[str, int, int]]) -> str:
    """For the manager's turn: open tasks per member (name, count, age of the oldest in days), so empty
    and stale lists get noticed."""
    if not counts:
        return ""
    stale = any(n and days >= STALE_TODO_DAYS for _name, n, days in counts)
    parts = [f"{name} {n}" + (f" (oldest {days} d)" if n and days >= STALE_TODO_DAYS else "") for name, n, days in counts]
    return "Open tasks per member: " + ", ".join(parts) + "." + (" Old tasks are usually done or obsolete: mark or delete them." if stale else "")


TODO_PREVIEW = 10  # open tasks listed in a turn; the rest via list_todos


def todo_block(todos: list, total: int) -> str:
    """Open tasks for the agent's turn (titles only; empty list = empty text)."""
    if not total:
        return ""
    lines = [f"- {t.id}: {truncate(t.title, 100)}" + (" +details" if t.description.strip() else "") for t in todos[:TODO_PREVIEW]]
    more = f"\n(+{total - len(lines)} more: list_todos)" if total > len(lines) else ""
    return f"Your task list ({total} open; details: get_todo; when finished: update_todo done=true):\n" + "\n".join(lines) + more


TODOS_CLEARED = "Your task list has no open tasks now."


def language_rule(language: str) -> str:
    """What people are written to in. A section of its own: what an agent reads (code, tickets, tool
    results) is mostly English, and without a rule it answers in that."""
    if language:
        return f"Language: write to people in {language}: in the chat, in questions to them and in what a ticket tells them. What you read in another language changes nothing about that."
    return "Language: write to a person in the language they write in, also right after reading material in another language."


def language_note(language: str) -> str:
    """For the manager's turn while the project has no language. Seen in production: a project from
    before the setting answered its user in English after a night of notices, which are all English.
    One call ends this line for good."""
    if language:
        return ""
    return (
        "No language is set for this project, so after English notices like these you may answer in English. "
        "Set it now with update_project language: the one the user writes in (e.g. German), or \"the language each person writes in\" if they differ."
    )


def subscriptions_line(tz_name: str, used_up_until: int | None, limited_before: bool) -> str:
    """The truth about the subscriptions, only when it matters: a limit message earlier in the
    conversation would otherwise be taken for a lasting fact."""
    if used_up_until:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        return "All subscriptions are used up until " + datetime.fromtimestamp(used_up_until, UTC).astimezone(tz).strftime("%H:%M %Z") + "."
    if limited_before:
        return "Subscriptions: available again (the earlier limit message no longer applies)."
    return ""


def with_preface(text: str, *, now: str, inbox: list[str], board: str = "", subscriptions: str = "") -> str:
    preface = [now]
    if subscriptions:
        preface.append(subscriptions)
    if inbox:
        preface.append("Updates since your last turn:\n" + "\n".join(f"- {item}" for item in inbox))
    if board:
        preface.append(board)
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
