import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  Agent,
  Assignment,
  CheckResult,
  Conversation,
  McpServer,
  Meta,
  Note,
  Project,
  Repository,
  Run,
  Schedule,
  SettingsResponse,
  Task,
  TaskDetail,
} from "./types";

export const qk = {
  auth: ["auth"] as const,
  meta: ["meta"] as const,
  settings: ["settings"] as const,
  projects: ["projects"] as const,
  agents: (pid: number) => ["agents", pid] as const,
  tasks: (pid: number) => ["tasks", pid] as const,
  task: (id: number) => ["task", id] as const,
  conversation: (agentId: number) => ["conversation", agentId] as const,
  runs: (pid: number) => ["runs", pid] as const,
  assignments: (pid: number) => ["assignments", pid] as const,
  notes: (pid: number) => ["notes", pid] as const,
  schedules: (pid: number) => ["schedules", pid] as const,
  mcp: (pid: number) => ["mcp", pid] as const,
  repos: (pid: number) => ["repos", pid] as const,
};

export interface AuthStatus {
  password_required: boolean;
  authenticated: boolean;
}

export const useAuth = () => useQuery({ queryKey: qk.auth, queryFn: () => api.get<AuthStatus>("/auth/status") });
export const useMeta = () => useQuery({ queryKey: qk.meta, queryFn: () => api.get<Meta>("/meta"), staleTime: 60_000 });
export const useSettings = () => useQuery({ queryKey: qk.settings, queryFn: () => api.get<SettingsResponse>("/settings") });
export const useProjects = () => useQuery({ queryKey: qk.projects, queryFn: () => api.get<Project[]>("/projects") });
export const useAgents = (pid: number) =>
  useQuery({ queryKey: qk.agents(pid), queryFn: () => api.get<Agent[]>(`/projects/${pid}/agents`) });
export const useTasks = (pid: number) =>
  useQuery({ queryKey: qk.tasks(pid), queryFn: () => api.get<Task[]>(`/projects/${pid}/tasks`) });
export const useTask = (id: number | null) =>
  useQuery({ queryKey: qk.task(id ?? 0), queryFn: () => api.get<TaskDetail>(`/tasks/${id}`), enabled: id !== null });
export const useConversation = (agentId: number | null) =>
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

export const useRepositories = (pid: number) =>
  useQuery({ queryKey: qk.repos(pid), queryFn: () => api.get<Repository[]>(`/projects/${pid}/repositories`) });

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
  provider: (provider = "claude_code") => api.post<CheckResult>("/settings/check/provider", { provider }),
  github: (token?: string) => api.post<CheckResult>("/settings/check/github", token ? { token } : {}),
  projectGithub: (pid: number) => api.post<CheckResult>(`/projects/${pid}/check-github`),
};
