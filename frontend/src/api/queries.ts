import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  Account,
  Limits,
  Agent,
  Member,
  Mentions,
  PauseStatus,
  Person,
  Preview,
  User,
  Assignment,
  CheckResult,
  Conversation,
  McpServer,
  Meta,
  Note,
  OwnNote,
  Project,
  Repository,
  Run,
  Schedule,
  Secret,
  SettingsResponse,
  FlowFinding,
  FlowHistory,
  FlowLive,
  FlowTicketBranch,
  Promotion,
  PromotionPreview,
  Idea,
  IdeasData,
  IdeaVersion,
  Stats,
  Task,
  TaskDetail,
  TodoPage,
  TodoState,
  Usage,
} from "./types";

export const qk = {
  auth: ["auth"] as const,
  users: ["users"] as const,
  directory: ["users", "directory"] as const,
  members: (pid: number) => ["members", pid] as const,
  people: (pid: number) => ["people", pid] as const,
  mentions: ["mentions"] as const,
  meta: ["meta"] as const,
  settings: ["settings"] as const,
  limits: ["limits"] as const,
  usage: ["usage"] as const,
  accounts: ["accounts"] as const,
  projects: ["projects"] as const,
  agents: (pid: number) => ["agents", pid] as const,
  tasks: (pid: number) => ["tasks", pid] as const,
  task: (id: number) => ["task", id] as const,
  todos: (agentId: number, state: TodoState) => ["todos", agentId, state] as const,
  ownNotes: (agentId: number) => ["ownNotes", agentId] as const,
  conversation: (agentId: number) => ["conversation", agentId] as const,
  runs: (pid: number) => ["runs", pid] as const,
  flow: (pid: number) => ["flow", pid] as const,
  assignments: (pid: number) => ["assignments", pid] as const,
  notes: (pid: number) => ["notes", pid] as const,
  schedules: (pid: number) => ["schedules", pid] as const,
  mcp: (pid: number) => ["mcp", pid] as const,
  repos: (pid: number) => ["repos", pid] as const,
  previews: (pid: number) => ["previews", pid] as const,
  pause: ["pause"] as const,
  secrets: (pid: number | null) => ["secrets", pid ?? "global"] as const,
  stats: (pid: number, days: number) => ["stats", pid, days] as const,
  ideas: (pid: number) => ["ideas", pid] as const,
  ideaQuestions: (pid: number) => ["ideaQuestions", pid] as const,
  ideaVersions: (id: number) => ["ideaVersions", id] as const,
};

export interface AuthStatus {
  /** No user yet: the first admin is created in the browser. */
  setup_required: boolean;
  setup_password_required: boolean;
  user: User | null;
}

export const useAuth = () => useQuery({ queryKey: qk.auth, queryFn: () => api.get<AuthStatus>("/auth/status") });
/** Who is signed in. The app renders nothing that asks before that is known. */
export const useMe = () => useAuth().data?.user ?? null;
export const useUsers = (enabled = true) => useQuery({ queryKey: qk.users, queryFn: () => api.get<User[]>("/users"), enabled });
export const useDirectory = () => useQuery({ queryKey: qk.directory, queryFn: () => api.get<Person[]>("/users/directory") });
export const useMembers = (pid: number) => useQuery({ queryKey: qk.members(pid), queryFn: () => api.get<Member[]>(`/projects/${pid}/members`) });
/** Who can be mentioned with @username in a project: everyone with access to it. */
export const usePeople = (pid: number | null) =>
  useQuery({ queryKey: qk.people(pid ?? 0), queryFn: () => api.get<Person[]>(`/projects/${pid}/people`), staleTime: 60_000, enabled: pid !== null });
/** The signed-in user's mentions, of all their projects; looked at again now and then for those of other projects. */
export const useMentions = () => useQuery({ queryKey: qk.mentions, queryFn: () => api.get<Mentions>("/mentions"), refetchInterval: 60_000 });
export function useReadMentions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ids: number[] | null) => api.post<void>("/mentions/read", { ids }),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.mentions }),
  });
}
export const useMeta = () => useQuery({ queryKey: qk.meta, queryFn: () => api.get<Meta>("/meta"), staleTime: 60_000 });
export const useSettings = () => useQuery({ queryKey: qk.settings, queryFn: () => api.get<SettingsResponse>("/settings") });
export const useAccounts = () => useQuery({ queryKey: qk.accounts, queryFn: () => api.get<Account[]>("/accounts") });
export const useSecrets = (pid: number | null) =>
  useQuery({ queryKey: qk.secrets(pid), queryFn: () => api.get<Secret[]>(pid === null ? "/secrets" : `/projects/${pid}/secrets`) });
export const useProjects = () => useQuery({ queryKey: qk.projects, queryFn: () => api.get<Project[]>("/projects") });
export const useAgents = (pid: number) =>
  useQuery({ queryKey: qk.agents(pid), queryFn: () => api.get<Agent[]>(`/projects/${pid}/agents`) });
/** Whether everything is paused. On the way to rest it is asked often: that is what tells when the server can be restarted. */
export const usePause = () =>
  useQuery({
    queryKey: qk.pause,
    queryFn: () => api.get<PauseStatus>("/pause"),
    refetchInterval: (query) => (!query.state.data?.paused ? false : query.state.data.standing ? 15_000 : 3_000),
  });
/** The servers the project's agents have left running. Asked again and again while one may run: a
 * server needs a moment to answer after its job started, and no event says when it does. */
export const usePreviews = (pid: number, enabled: boolean) =>
  useQuery({ queryKey: qk.previews(pid), queryFn: () => api.get<Preview[]>(`/projects/${pid}/previews`), enabled, refetchInterval: 10_000 });
export const useTasks = (pid: number) =>
  useQuery({ queryKey: qk.tasks(pid), queryFn: () => api.get<Task[]>(`/projects/${pid}/tasks`) });
// Shares the tasks key, so every ticket event refreshes it too.
export const useArchivedTasks = (pid: number, enabled = true) =>
  useQuery({ queryKey: [...qk.tasks(pid), "archived"], queryFn: () => api.get<Task[]>(`/projects/${pid}/tasks?archived=true`), enabled });
export const useTask = (id: number | null) =>
  useQuery({ queryKey: qk.task(id ?? 0), queryFn: () => api.get<TaskDetail>(`/tasks/${id}`), enabled: id !== null });
export const TODO_PAGE = 50;
// Paged, so long lists stay fast: the next page loads on demand.
export const useTodos = (agentId: number, state: TodoState) =>
  useInfiniteQuery({
    queryKey: qk.todos(agentId, state),
    queryFn: ({ pageParam }) => api.get<TodoPage>(`/agents/${agentId}/todos?state=${state}&limit=${TODO_PAGE}&offset=${pageParam}`),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.items.length, 0);
      return loaded < last.total ? loaded : undefined;
    },
  });
export const useOwnNotes = (agentId: number) =>
  useQuery({ queryKey: qk.ownNotes(agentId), queryFn: () => api.get<OwnNote[]>(`/agents/${agentId}/notes`) });
export const useConversation =(agentId: number | null) =>
  useQuery({
    queryKey: qk.conversation(agentId ?? 0),
    queryFn: () => api.get<Conversation>(`/agents/${agentId}/conversation`),
    enabled: agentId !== null,
  });
export const useRuns = (pid: number) =>
  useQuery({ queryKey: qk.runs(pid), queryFn: () => api.get<Run[]>(`/projects/${pid}/runs?limit=100`) });
export const useAssignments = (pid: number) =>
  useQuery({ queryKey: qk.assignments(pid), queryFn: () => api.get<Assignment[]>(`/projects/${pid}/assignments?limit=100`) });
export const useNotes = (pid: number) =>
  useQuery({ queryKey: qk.notes(pid), queryFn: () => api.get<Note[]>(`/projects/${pid}/notes`) });
export const useSchedules = (pid: number) =>
  useQuery({ queryKey: qk.schedules(pid), queryFn: () => api.get<Schedule[]>(`/projects/${pid}/schedules`) });
export const useMcpServers = (pid: number) =>
  useQuery({ queryKey: qk.mcp(pid), queryFn: () => api.get<McpServer[]>(`/projects/${pid}/mcp-servers`) });

export const useIdeas = (pid: number) => useQuery({ queryKey: qk.ideas(pid), queryFn: () => api.get<IdeasData>(`/projects/${pid}/ideas`) });
/** The AI's open questions about ideas, for "for you". */
export const useIdeaQuestions = (pid: number, enabled = true) =>
  useQuery({ queryKey: qk.ideaQuestions(pid), queryFn: () => api.get<Idea[]>(`/projects/${pid}/ideas/questions`), enabled });
export const useIdeaVersions = (pid: number, id: number, enabled = true) =>
  useQuery({ queryKey: qk.ideaVersions(id), queryFn: () => api.get<IdeaVersion[]>(`/projects/${pid}/ideas/${id}/versions`), enabled });

export const useRepositories = (pid: number) =>
  useQuery({ queryKey: qk.repos(pid), queryFn: () => api.get<Repository[]>(`/projects/${pid}/repositories`) });

export const useStats = (pid: number, days: number) =>
  useQuery({ queryKey: qk.stats(pid, days), queryFn: () => api.get<Stats>(`/projects/${pid}/stats?days=${days}`), refetchInterval: 60_000 });

export const useFlowCheck = (pid: number, hours: number) =>
  useQuery({ queryKey: ["flow-check", pid, hours], queryFn: () => api.get<FlowFinding[]>(`/projects/${pid}/flow-check?hours=${hours}`), refetchInterval: 120_000 });

/** Generic mutation that invalidates the given query keys on success. */
export function useApiMutation<TVars, TResult = unknown>(
  fn: (vars: TVars) => Promise<TResult>,
  invalidate: (vars: TVars, result: TResult) => readonly (readonly unknown[])[] = () => [],
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (result, vars) => {
      for (const key of invalidate(vars, result)) qc.invalidateQueries({ queryKey: key });
    },
  });
}

export const checks = {
  account: (id: number, model: string) => api.post<CheckResult>(`/accounts/${id}/check`, { model }),
  github: (token?: string) => api.post<CheckResult>("/settings/check/github", token ? { token } : {}),
  projectGithub: (pid: number) => api.post<CheckResult>(`/projects/${pid}/check-github`),
};

/** Disk and memory (admins only). Folder sizes change rarely; while they are measured, look more often. */
export function useUsage(enabled = true) {
  return useQuery({
    queryKey: qk.usage,
    queryFn: () => api.get<Usage>("/usage"),
    enabled,
    refetchInterval: (query) => (query.state.data?.measuring ? 15_000 : 120_000),
  });
}

export function useLimits() {
  return useQuery({ queryKey: qk.limits, queryFn: () => api.get<Limits>("/limits"), refetchInterval: 30_000 });
}

/** What works where, is checked and lands right now. Pushed as `flow.changed`; the poll only covers a missed event and the clock of a running train. */
export const useFlow = (pid: number) => useQuery({ queryKey: qk.flow(pid), queryFn: () => api.get<FlowLive>(`/projects/${pid}/flow`), refetchInterval: 60_000 });
/** The history of trains, sweeps and stuck branches, newest first, a page at a time. */
export const FLOW_PAGE = 20;
export const useFlowHistory = (pid: number, repositoryId: number | null) =>
  useInfiniteQuery({
    queryKey: [...qk.flow(pid), "history", repositoryId],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const q = new URLSearchParams({ limit: String(FLOW_PAGE) });
      if (repositoryId !== null) q.set("repository_id", String(repositoryId));
      if (pageParam) q.set("before", pageParam);
      return api.get<FlowHistory>(`/projects/${pid}/flow/history?${q}`);
    },
    getNextPageParam: (last) => last.next_before,
  });
/** What promoting a repository's base branch to its release branch would bring now (#129): read from git when the dialog opens. */
export const usePromotionPreview = (repoId: number, enabled: boolean) =>
  useQuery({ queryKey: ["promotion", repoId], queryFn: () => api.get<PromotionPreview>(`/repositories/${repoId}/promotion`), enabled, staleTime: 0, retry: false });
export const usePromote = (pid: number) =>
  useApiMutation(
    ({ repoId, tag, sha }: { repoId: number; tag: string; sha: string }) => api.post<Promotion>(`/repositories/${repoId}/promotion`, { tag, sha }),
    () => [qk.flow(pid)],
  );
/** The way a ticket's branches take to the base branch. */
export const useTaskFlow = (taskId: number) => useQuery({ queryKey: ["taskflow", taskId], queryFn: () => api.get<FlowTicketBranch[]>(`/tasks/${taskId}/flow`) });
