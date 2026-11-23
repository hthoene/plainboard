// Mirrors app/plainboard/schemas.py

export type TaskStatus = "backlog" | "todo" | "in_progress" | "review" | "done";
export type Priority = "low" | "medium" | "high" | "urgent";
export type Toolset = "full" | "board";

export interface Project {
  id: number;
  name: string;
  slug: string;
  description: string;
  /** What agents write to people in, e.g. "German". Empty: the language each person writes in. */
  language: string;
  /** Languages of the customer texts (codes like "en"); the first is the default. */
  customer_languages: string[];
  workspace_path: string | null;
  has_github_token: boolean;
  auto_wake_manager: boolean;
  /** When nothing in To do can start, the team goes on with the Backlog. */
  continue_with_backlog: boolean;
  fast_checks: boolean;
  semantic_search: boolean;
  /** Paused for good by a person, since then; null: not paused. */
  paused_at: string | null;
  paused_by: string;
  parallel_agents: number | null;
  /** The subscription the project's agents run on; null: the one with the most room. */
  account_id: number | null;
  /** The only subscriptions it may use, set by an admin; empty: any. */
  account_ids: number[];
  runner_id?: number | null;
  /** The signed-in user's role in the project. */
  role: ProjectRole | null;
  auto_archive_days: number;
  review_days: number;
  attachment_retention_days: number;
  attachment_retention_min_mb: number;
  /** What answers quick questions (Ctrl+Enter in the chat); "" : the default, Claude Haiku with effort high. */
  ask_provider: string;
  ask_model: string;
  ask_effort: string;
  /** What sorts the project's ideas into topics; "" : the same as its questions. */
  idea_provider: string;
  idea_model: string;
  idea_effort: string;
  label_colors: Record<string, string>;
  created_at: string;
}

export interface Secret {
  id: number;
  project_id: number | null;
  name: string;
  description: string;
  updated_at: string;
}

/** A key for a project's read-only public API. `key` is only set right after creating it. */
/** A key that is shown once, when it is created (`key`), and by its last characters afterwards. */
export interface PersonalToken {
  id: number;
  label: string;
  hint: string;
  read_only: boolean;
  expires_at: string | null;
  created_at: string;
  last_used_at: string | null;
  token: string | null;
}

export interface ApiKey {
  id: number;
  label: string;
  hint: string;
  created_at: string;
  last_used_at: string | null;
  key: string | null;
}

export interface PublicApiKey extends ApiKey {
  project_id: number;
}

/** How git reaches a repository: the general GitHub token (or none), its own token, or its own SSH key. */
export type RepoAuth = "default" | "token" | "ssh";

export interface Repository {
  id: number;
  project_id: number;
  name: string;
  url: string;
  default_branch: string | null;
  description: string;
  check_command: string;
  quick_check_command: string;
  land_first: boolean;
  check_target_seconds?: number;
  checks_verdict?: string;
  /** Branches that are fully merged are removed, once a day. */
  tidy_branches: boolean;
  agent_ids: number[];
  auth: RepoAuth;
  token_user: string;
  has_token: boolean;
  ssh_public_key: string;
  /** The base branch, while its checks fail. */
  red_base?: string | null;
  created_at: string;
}

/** Everything is paused (all projects), and what still moves on the way to rest. */
export interface PauseStatus {
  paused: boolean;
  by: string;
  since: string | null;
  /** Paused, and nothing moves any more: PlainBoard can be restarted. */
  standing: boolean;
  moving: { what: "turn" | "job" | "check"; project: string; agent: string; detail: string }[];
  /** Servers agents left running: they end if the runner restarts. */
  servers: number;
}

/** A web server an agent has started and left running. */
export interface Preview {
  port: number;
  /** Of the page it starts with; may be empty. */
  title: string;
  /** Listens on 127.0.0.1 only: it opens in the browser window, not at /preview/<port>/. */
  local: boolean;
  /** It has /preview-base/<port>/ as its base path: that is where it opens. */
  base: boolean;
  job_id: number;
  agent_id: number;
  command: string;
  started_at: string;
  /** Who opens it without the login. */
  access: "private" | "password" | "public";
  shared_until: string | null;
}

export interface Agent {
  id: number;
  project_id: number;
  name: string;
  slug: string;
  role: string;
  is_manager: boolean;
  description: string;
  instructions: string;
  provider: string;
  model: string;
  effort: string | null;
  toolset: Toolset;
  context_limit_tokens: number;
  keep_recent_messages: number;
  summary_model: string;
  summary_effort: string | null;
  max_turns: number | null;
  enabled: boolean;
  color: string;
  mcp_server_ids: number[];
  all_repos: boolean;
  repository_ids: number[];
  account_id: number | null;
  status: "idle" | "queued" | "running" | "background";
  running_jobs: number;
  job_commands: string[];
  open_todos: number;
  own_notes: number;
  context_tokens: number;
  base_context_tokens: number;
  total_cost_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
}

export interface Task {
  id: number;
  project_id: number;
  number: number;
  title: string;
  description: string;
  customer_title: string;
  customer_description: string;
  /** Customer texts of the other customer languages; customer_title/description are the default language's. */
  customer_texts: Record<string, { title: string; description: string }>;
  status: TaskStatus;
  position: number;
  priority: Priority;
  labels: string[];
  user_action: string;
  archived_at: string | null;
  created_by_agent_id: number | null;
  created_at: string;
  updated_at: string;
  comment_count: number;
  attachment_count: number;
  active_agent_ids: number[];
  manager_active: boolean;
  open_decisions: number;
  /** Work is finished and only waits to land: its branch waits for a train, is checked, or is repaired. */
  landing: "waiting" | "checking" | "repairing" | "held" | null;
}

export interface Comment {
  id: number;
  task_id: number;
  author_kind: "user" | "agent" | "system";
  author_agent_id: number | null;
  body: string;
  created_at: string;
}

/** A team member's own note: always in that member's prompt. */
export interface OwnNote {
  id: number;
  project_id: number;
  agent_id: number;
  text: string;
  created_by_agent_id: number | null;
  created_at: string;
  updated_at: string;
}

export type TodoState = "open" | "done" | "archived";

export interface Todo {
  id: number;
  project_id: number;
  agent_id: number;
  title: string;
  description: string;
  done_at: string | null;
  created_by_agent_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface TodoPage {
  items: Todo[];
  total: number;
}

export interface Attachment {
  id: number;
  task_id: number | null;
  filename: string;
  content_type: string;
  size: number;
  uploaded_by_agent_id: number | null;
  created_at: string;
  purged_at?: string | null;
  purged_after_days?: number | null;
}

export interface RetentionPreview {
  now: { count: number; bytes: number };
  soon: { count: number; bytes: number };
  soon_days: number;
}

export interface Assignment {
  id: number;
  project_id: number;
  task_id: number | null;
  agent_id: number;
  created_by_agent_id: number | null;
  title: string;
  instructions: string;
  effort: string | null;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  result: string | null;
  run_id: number | null;
  created_at: string;
  finished_at: string | null;
  scratchpad?: string | null;
}

export interface DecisionOption {
  label: string;
  description: string;
  attachment_ids: number[];
}

export interface Decision {
  id: number;
  task_id: number;
  requested_by_agent_id: number | null;
  question: string;
  details: string;
  options: DecisionOption[];
  attachment_ids: number[];
  status: "open" | "answered" | "withdrawn";
  answer_option: number | null;
  answer_text: string;
  answered_by: "user" | "agent" | null;
  answered_at: string | null;
  created_at: string;
}

export interface TaskDetail extends Task {
  /** Where the ticket stands, kept by PlainBoard from the reports; "" while it has only a few. */
  state: string;
  state_updated_at: string | null;
  comments: Comment[];
  attachments: Attachment[];
  assignments: Assignment[];
  decisions: Decision[];
}

export interface Message {
  id: number;
  conversation_id: number;
  role: "user" | "assistant" | "system";
  kind: "text" | "tool" | "summary" | "event" | "error" | "files";
  content: string;
  meta: Record<string, any>;
  run_id: number | null;
  created_at: string;
}

export interface Conversation {
  id: number;
  agent_id: number;
  provider_session_id: string | null;
  context_tokens: number;
  session_base_tokens: number;
  context_limit_tokens: number;
  compressions: number;
  has_summary: boolean;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: number;
  messages: Message[];
  has_more: boolean;
}

/** One message of a chat run: several wait for the same turn when people write before it starts. */
export interface ChatPart {
  id: number;
  author: string | null;
  text: string;
  attachment_ids: number[];
  at: string;
}

export interface Run {
  id: number;
  project_id: number;
  agent_id: number;
  trigger: "chat" | "ticket" | "assignment" | "ask" | "message" | "schedule" | "wake";
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  stop_requested_at: string | null;
  /** A waiting chat message: when its busy agent read it (before the turn that answers it). */
  read_at: string | null;
  input_text: string;
  /** A chat message: the person who wrote it. */
  author_name: string | null;
  /** A chat run: the messages waiting for it, each shown on its own. Null on older runs. */
  parts: ChatPart[] | null;
  attachment_ids: number[];
  effort: string | null;
  task_id: number | null;
  assignment_id: number | null;
  schedule_id: number | null;
  source_agent_id: number | null;
  parent_run_id: number | null;
  result_text: string | null;
  error: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  cost_usd: number;
  num_turns: number;
  context_tokens: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Note {
  id: number;
  project_id: number;
  title: string;
  content: string;
  pinned: boolean;
  created_by_agent_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface Schedule {
  id: number;
  project_id: number;
  agent_id: number;
  name: string;
  prompt: string;
  kind: "once" | "cron";
  run_at: string | null;
  cron: string | null;
  timezone: string;
  enabled: boolean;
  /** A run is left out when nothing happened in the project since the last one. */
  skip_quiet: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: number | null;
  created_at: string;
}

export interface Skill {
  id: number;
  name: string;
  description: string;
  enabled: boolean;
  all_agents: boolean;
  agent_ids: number[];
  source: "upload" | "git";
  source_url: string;
  source_ref: string;
  source_path: string;
  content_hash: string;
  size: number;
  file_count: number;
  updated_at: string;
}

export interface McpServer {
  id: number;
  project_id: number | null;
  name: string;
  transport: "stdio" | "http" | "sse";
  command: string | null;
  args: string[];
  url: string | null;
  env: Record<string, string>;
  headers: Record<string, string>;
  enabled: boolean;
  all_agents: boolean;
  agent_ids: number[];
}

export interface ModelInfo {
  id: string;
  label: string;
  tier: string;
}

export interface ProviderInfo {
  name: string;
  label: string;
  models: ModelInfo[];
  efforts: string[];
  /** Set for a provider that is an address (a local model server, a model host): its usual address, whether it wants a key, what it is. */
  endpoint?: { url: string; key: boolean; hint: string } | null;
}

export interface Meta {
  /** The providers agents can run on. One that is an endpoint is here once it has an account. */
  providers: ProviderInfo[];
  /** All providers that are an endpoint, set up or not. */
  endpoints: ProviderInfo[];
  runner_ok: boolean;
  statuses: TaskStatus[];
  pinned_note_budget_chars: number;
  /** Semantic search can exist here: PostgreSQL with pgvector. */
  semantic_search: boolean;
  /** The running commit and how many newer ones exist; null: the build does not know, or the check is off. */
  version: { commit: string; behind: number | null; url: string } | null;
  database: "sqlite" | "postgresql";
}

/** GET /database (admins). `move`: whether the bundled PostgreSQL can take the move; null: there is none. */
export interface DatabaseStatus {
  database: "sqlite" | "postgresql";
  auto: boolean;
  move: "ready" | "not_empty" | "unreachable" | "requested" | null;
  failed: string | null;
}

export interface SettingsResponse {
  values: Record<string, any>;
  secrets: string[];
  env_fallbacks: Record<string, boolean>;
}

export interface CheckResult {
  ok: boolean;
  detail: string;
}

export interface ProviderAccount {
  signed_in: boolean;
  detail: string;
  login_url: string | null;
  login_code: string | null;
  login_error: string | null;
  needs_code: boolean;
}

export interface LimitWindow {
  name: string;
  utilization: number;
  resets_at: number | null;
  status: string | null;
}

export interface LimitForecast {
  name: string;
  status: "ok" | "tight" | "over";
  daily: number;
  budget: number;
  projected: number;
  runs_out_at: number | null;
  basis_days: number;
}

export interface AccountLimits {
  account_id: number;
  provider: string;
  label: string;
  available: boolean;
  windows: LimitWindow[];
  forecasts: LimitForecast[];
  updated_at: number | null;
  /** The plan as the provider names it and its size (a multiple of its smallest plan), where it can tell. */
  plan: string | null;
  plan_size: number | null;
}

export interface Limits {
  accounts: AccountLimits[];
}

/** A subscription or API key of a provider. */
export interface Account {
  id: number;
  provider: string;
  label: string;
  auth_mode: string;
  enabled: boolean;
  secret_set: boolean;
  secret_from_env: boolean;
  /** Of a provider that is an endpoint: its address (empty: the usual one) and the models it offers. Of a Codex
   * subscription: the models it may use, as its last read of the limits found them. */
  endpoint_url: string;
  models: string[];
  /** How many tokens its models take at once; 0: not said. */
  context_tokens: number;
  /** The size of its plan as the user gave it; null: as the provider says. */
  plan_size: number | null;
  /** null: the installation's; else a person's own, shared with nobody, some people or everyone. */
  owner_id: number | null;
  owner_name: string;
  share: "private" | "people" | "everyone";
  shared_with: number[];
  /** The viewer may change, test, share and delete it (its owner, an admin). */
  can_manage: boolean;
  /** The projects its owner gave it to themselves (to its managers only). */
  project_ids: number[] | null;
}

/** A person's own subscription in a project's list of what members gave it. Never a secret. */
export interface ProjectAccount {
  id: number;
  provider: string;
  label: string;
  enabled: boolean;
  owner_id: number | null;
  owner_name: string;
  /** The viewer's own: they give it to the project or take it back. */
  mine: boolean;
  given: boolean;
  /** The project's owner switched it off for the project. */
  blocked: boolean;
  /** The project's runs may use it now (also one not given: shared with its owner). */
  used: boolean;
}

export interface StatsDay {
  date: string;
  tickets_created: number;
  tickets_done: number;
  runs: number;
  failed_runs: number;
  tokens: number;
  cost_usd: number;
  branches_landed: number;
  /** Median from handing a branch over to it being on the base branch; null: none landed that day. */
  landing_seconds: number | null;
  check_runs: number;
  check_runs_failed: number;
  assignments_done: number;
  /** Of the finished assignments: repairs for landing (a failed check run, a conflict). */
  repairs: number;
  /** Times PlainBoard itself started with another version that day. */
  updates: number;
}

/** Something PlainBoard's own checks found that slows the team down. */
export interface FlowFinding {
  key: string;
  title: string;
  detail: string;
  action: string;
}

/** What a period brought and cost; given for the period shown and the one right before it. */
export interface StatsPeriod {
  tickets_done: number;
  branches_landed: number;
  landing_seconds: number | null;
  check_runs: number;
  check_runs_failed: number;
  assignments_done: number;
  repairs: number;
  cost_usd: number;
}

export interface StatsAgent {
  agent_id: number;
  runs: number;
  failed_runs: number;
  assignments_done: number;
  busy_seconds: number;
  tokens: number;
  cost_usd: number;
}

export interface Stats {
  days: StatsDay[];
  agents: StatsAgent[];
  open_tickets: number;
  median_cycle_seconds: number | null;
  current: StatsPeriod;
  previous: StatsPeriod;
  versions: { at: string; commit: string }[];
}

export type ProjectRole = "viewer" | "member" | "owner";

export interface User {
  id: number;
  username: string;
  name: string;
  is_admin: boolean;
  may_create_projects: boolean;
  /** What the projects this person owns may use; null: no limit. */
  max_projects: number | null;
  account_ids: number[] | null;
  /** 0: the built-in runner. */
  runner_ids: number[] | null;
  max_parallel: number | null;
  /** The global secrets, the installation's GitHub token and SSH key, the global MCP servers. */
  may_use_shared: boolean;
  enabled: boolean;
  /** 0: no profile picture. */
  avatar_version: number;
  /** Browser notifications for mentions; they are recorded either way. */
  show_mentions: boolean;
  created_at: string;
}

/** What everyone signed in may know about a user. */
export interface Person {
  id: number;
  username: string;
  name: string;
  avatar_version: number;
}

/** Someone named the signed-in user with @username. */
export interface Mention {
  id: number;
  project_id: number;
  project_name: string;
  kind: "chat" | "comment" | "decision";
  task_id: number | null;
  task_number: number | null;
  task_title: string;
  /** The chat it is in. */
  agent_id: number | null;
  author: string;
  excerpt: string;
  read_at: string | null;
  created_at: string;
}

export interface Mentions {
  unread: number;
  items: Mention[];
}

export interface Member {
  user_id: number;
  username: string;
  name: string;
  role: ProjectRole;
  /** The person whose limits hold for the project (not the role "owner"). */
  project_owner: boolean;
}

/** What changes for a project if this person owned it (GET /projects/{id}/owner/{user_id}). */
export interface OwnerChange {
  user_id: number;
  name: string;
  /** The project's runner; "": the built-in one. */
  runner: string;
  runner_allowed: boolean;
  lost_accounts: string[];
  /** "the project" and agents whose chosen subscription is then chosen automatically. */
  unpinned: string[];
  loses_shared: boolean;
  max_parallel_before: number | null;
  max_parallel_after: number | null;
}

/** Disk and memory of the installation (GET /usage, admins). Sizes in bytes. */
export interface UsageVolume {
  kinds: string[];
  total: number;
  used: number;
  free: number;
  low: boolean;
}

export interface AgentUsage {
  id: number;
  name: string;
  disk: number | null;
  memory: number;
  processes: number;
  cpu: number;
  cpu_avg: number[];
  cpu_history: number[];
}

export interface ProjectUsage {
  id: number;
  name: string;
  agents: AgentUsage[];
  uploads: number;
  other_disk: number;
  other_memory: number;
  other_cpu: number;
  other_cpu_avg: number[];
  other_cpu_history: number[];
}

/** What one runner's machine uses, with the projects that run on it. */
export interface MachineUsage {
  runner_ok: boolean;
  measured_at: string | null;
  measuring: boolean;
  volumes: UsageVolume[];
  projects: ProjectUsage[];
  shared: { name: string; bytes: number }[];
  other_disk: number;
  memory_total: number;
  memory_used: number;
  memory_other: number;
  /** CPU in cores: now, averaged over the last 1, 6 and 24 hours (cpu_avg), and per slot of cpu_step seconds from cpu_start (…_history). */
  cpu_cores: number;
  cpu_step: number;
  cpu_start: string | null;
  cpu_load: number[];
  cpu_load_history: number[];
  cpu_other: number;
  cpu_other_avg: number[];
  cpu_other_history: number[];
}

/** An external runner: connected, its numbers are current; away, the last known ones (known_at; null: none). */
export interface RunnerUsage extends MachineUsage {
  id: number;
  name: string;
  connected: boolean;
  last_seen_at: string | null;
  known_at: string | null;
}

/** The built-in runner's machine and the app's, and the external runners'. */
export interface Usage extends MachineUsage {
  data: number | null;
  runners: RunnerUsage[];
  storage: UsageStorage | null;
  cleanups: UsageCleanup[];
  servers_stopped: UsageCleanup | null;
}

/** A notice to managers that the disk is above its target, or will be soon. */
export interface StorageNotice {
  at: string;
  reason: "over" | "soon" | "emergency";
  percent: number;
  rate: number | null;
  hours_target: number | null;
  hours_full: number | null;
  projects: string[];
}

/** The agents' volume against its target: trend, forecast and the last notices. */
export interface UsageStorage {
  notify_percent: number;
  emergency_percent: number;
  percent: number | null;
  rate: number | null;
  hours_target: number | null;
  hours_95: number | null;
  hours_full: number | null;
  notices: StorageNotice[];
}

/** What a cleanup removed (by the manager, in an emergency by itself, shared caches trimmed above the target), or which servers it stopped (level "memory"). */
export interface UsageCleanup {
  at: string;
  level: "manager" | "emergency" | "trim" | "memory";
  freed: number;
  free_before: number;
  total: number;
  items: { path: string; what: "build" | "cache" | "folder" | "logs" | "home" | "server"; bytes: number }[];
  kept: string[];
  told: boolean;
  by: string;
  project: string;
}

export interface RunnerInfo {
  id: number;
  name: string;
  connected: boolean;
  version: string;
  version_ok: boolean;
  updating: boolean;
  restart_by?: string | null;
  last_seen_at: string | null;
  created_at: string;
  projects: string[];
  disk_total: number;
  disk_used: number;
  memory_total: number;
  memory_used: number;
}

export interface RunnerCreated {
  runner: RunnerInfo;
  token: string;
  command: string;
  compose: string;
}

/** Work in the project's workspaces on its current runner that is on no remote: it stays behind on a move. */
export interface ProjectUnpushed {
  runner: string;
  checked: boolean;
  detail: string;
  items: { agents: string[]; repo: string; problems: string[] }[];
}

export interface ProjectRunner {
  runner_id: number | null;
  runners: { id: number | null; name: string; connected: boolean }[];
  busy: string;
}

export interface SearchModelState {
  name: string;
  label: string;
  state: "absent" | "downloading" | "loading" | "ready" | "failed";
  /** While downloading: the share done. */
  progress: number | null;
  error: string;
}

export interface SearchModelOption {
  name: string;
  label: string;
  tier: "small" | "balanced" | "large";
  description: string;
  dim: number;
  max_tokens: number;
  licence: string;
  download_mb: number;
  ram_mb: number;
  mmteb: number | null;
  mmteb_retrieval: number | null;
  recommended: boolean;
  /** A full index of what is indexed now, with this model, on this server. */
  estimate_seconds: number | null;
}

export interface SearchStatus {
  available: boolean;
  enabled: boolean;
  /** What search uses. */
  model: SearchModelState;
  /** The model the index is being made anew for; search switches to it when it is complete. */
  next: SearchModelState | null;
  documents: number;
  embedded: number;
  next_embedded: number;
  by_kind: Record<string, SearchKind>;
  /** Chunks embedded per second here, lately. */
  rate: number | null;
  /** Until every chunk has its vector (of the next model, while switching). */
  eta_seconds: number | null;
  /** The indexer's last failures. */
  errors: { at: string; message: string }[];
  models: SearchModelOption[];
}

export interface SearchKind {
  /** Items indexed. */
  items: number;
  /** Items in the source; null: not known (commits). */
  total: number | null;
  chunks: number;
  /** Chunks with a vector of the current model. */
  embedded: number;
}

// --- flow: what works where, is checked and lands (GET /projects/{id}/flow, services/flow.py) ---

export interface FlowRepo {
  id: number;
  name: string;
  base: string;
  /** Branches land before their checks (a development branch). */
  land_first: boolean;
  /** The median of the last check runs that landed; null until there is one. */
  typical_check_seconds: number | null;
  check_target_seconds: number;
  /** The base branch fails its checks. */
  red: boolean;
}
export interface FlowJob {
  id: number;
  command: string;
  started_at: string;
}
export interface FlowWork {
  agent_id: number;
  status: "running" | "queued" | "background";
  run_id: number | null;
  trigger: string | null;
  since: string | null;
  task_id: number | null;
  task_number: number | null;
  /** The work order, if the turn is one. */
  title: string;
  /** PlainBoard ordered it: a branch or train failed its checks. */
  repair: boolean;
  branch: string | null;
  jobs: FlowJob[];
}
export interface FlowBranch {
  id: number;
  repository_id: number;
  branch: string;
  title: string;
  agent_id: number | null;
  task_id: number | null;
  task_number: number | null;
  status: "waiting" | "checking" | "repairing" | "merged" | "stuck" | "closed";
  train_id: number | null;
  failures: number;
  since: string;
  merged_at: string | null;
  conflict_files: string[];
  /** It does not land until its author hands it over again ("message") or the manager releases it ("manager"). */
  held: "message" | "manager" | null;
  held_reason: string;
}
export interface FlowTrain {
  id: number;
  repository_id: number;
  status: "checking" | "blocked" | "landed" | "failed" | "aborted";
  /** Its branches landed first; the checks look at the base branch afterwards. */
  direct: boolean;
  /** Branches on it; 0: a look at the base branch. */
  size: number;
  started_at: string;
  finished_at: string | null;
  check_seconds: number;
  failed_checks: string[];
  repaired_group: boolean;
  escalated: boolean;
  branches: string[];
}
export interface FlowLive {
  now: string;
  repositories: FlowRepo[];
  work: FlowWork[];
  branches: FlowBranch[];
  trains: FlowTrain[];
}
export interface FlowRepair {
  agent_id: number | null;
  assignment_id: number | null;
  at: string;
}
export interface FlowHistoryItem {
  id: string;
  kind: "train" | "sweep" | "stuck";
  at: string;
  repository_id: number | null;
  train: FlowTrain | null;
  repairs: FlowRepair[];
  detail: string;
}
export interface FlowHistory {
  items: FlowHistoryItem[];
  next_before: string | null;
}
export interface FlowStep {
  key: "handed" | "failed" | "repair" | "checking" | "landed" | "stuck";
  state: "done" | "now" | "todo";
  at: string | null;
  detail: string;
}
export interface FlowTicketBranch {
  branch: FlowBranch;
  repository: string;
  train: FlowTrain | null;
  steps: FlowStep[];
}

/** A hit of the project's search by meaning (the command palette). */
export interface SearchHit {
  kind: "ticket" | "report" | "decision" | "comment" | "note" | "commit";
  ref: string;
  title: string;
  snippet: string;
  at: string;
  changed_at: string;
  ticket_number: number | null;
  task_id: number | null;
  note_id: number | null;
  repo: string | null;
  sha: string | null;
  url: string | null;
}

export interface ProjectSearch {
  /** ok: by meaning and words; preparing: by words until the model is ready; off / unavailable: no hits. */
  state: "ok" | "preparing" | "off" | "unavailable";
  hits: SearchHit[];
}

/** The project's ideas (#91): a planning space, nothing in it starts work. */
export interface IdeaOption {
  label: string;
  description: string;
}

export interface Idea {
  id: number;
  project_id: number;
  title: string;
  text: string;
  author: string;
  status: "open" | "taken" | "dismissed" | "merged";
  topic_id: number | null;
  /** Placed by a person: the AI leaves its topic alone. */
  topic_manual: boolean;
  task_id: number | null;
  task_number: number | null;
  merged_into_id: number | null;
  hint: string;
  sort_state: "pending" | "sorted" | "failed";
  x: number | null;
  y: number | null;
  votes: number;
  similar: number[];
  duplicate_of: number | null;
  question: string;
  /** The first is recommended; a free answer is possible too. */
  question_options: IdeaOption[];
  question_status: "" | "open" | "answered" | "withdrawn";
  question_answer: string;
  question_at: string | null;
  closed_at: string | null;
  closed_reason: string;
  created_at: string;
  updated_at: string;
}

export interface IdeaTopic {
  id: number;
  project_id: number;
  name: string;
  /** 0–9: the design's --chart-1 … --chart-10. */
  color: number;
  hint: string;
  manual: boolean;
  x: number | null;
  y: number | null;
}

export interface IdeaChange {
  id: number;
  project_id: number;
  kind: "new" | "several" | "amend" | "merge" | "question" | "edit" | "reorder";
  source: "input" | "answer" | "user" | "auto" | "button";
  author: string;
  summary: Record<string, any>;
  created_at: string;
  undone_at: string | null;
  /** The ideas it touched, as they are now (only in the answer to an action). */
  ideas: Idea[];
}

export interface IdeaVersion {
  id: number;
  idea_id: number;
  title: string;
  text: string;
  source: "user" | "ai" | "undo";
  created_at: string;
}

export interface IdeasData {
  topics: IdeaTopic[];
  ideas: Idea[];
  /** The AI is sorting ideas right now. */
  sorting: boolean;
}
