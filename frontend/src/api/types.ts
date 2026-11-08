// Mirrors app/plainboard/schemas.py

export type TaskStatus = "backlog" | "todo" | "in_progress" | "review" | "done";
export type Priority = "low" | "medium" | "high" | "urgent";
export type Toolset = "full" | "board";

export interface Project {
  id: number;
  name: string;
  slug: string;
  description: string;
  workspace_path: string | null;
  has_github_token: boolean;
  has_public_api_key: boolean;
  auto_wake_manager: boolean;
  auto_archive_days: number;
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

export interface Repository {
  id: number;
  project_id: number;
  name: string;
  url: string;
  default_branch: string | null;
  description: string;
  agent_ids: number[];
  created_at: string;
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
  max_turns: number | null;
  enabled: boolean;
  color: string;
  mcp_server_ids: number[];
  all_repos: boolean;
  repository_ids: number[];
  account_id: number | null;
  status: "idle" | "queued" | "running";
  running_jobs: number;
  open_todos: number;
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
}

export interface Comment {
  id: number;
  task_id: number;
  author_kind: "user" | "agent" | "system";
  author_agent_id: number | null;
  body: string;
  created_at: string;
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
  comments: Comment[];
  attachments: Attachment[];
  assignments: Assignment[];
  decisions: Decision[];
}

export interface Message {
  id: number;
  conversation_id: number;
  role: "user" | "assistant" | "system";
  kind: "text" | "tool" | "summary" | "event" | "error";
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

export interface Run {
  id: number;
  project_id: number;
  agent_id: number;
  trigger: "chat" | "ticket" | "assignment" | "ask" | "message" | "schedule" | "wake";
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  stop_requested_at: string | null;
  input_text: string;
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
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: number | null;
  created_at: string;
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
}

export interface Meta {
  providers: ProviderInfo[];
  runner_ok: boolean;
  statuses: TaskStatus[];
  pinned_note_budget_chars: number;
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
}

export interface StatsDay {
  date: string;
  tickets_created: number;
  tickets_done: number;
  runs: number;
  failed_runs: number;
  tokens: number;
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
}
