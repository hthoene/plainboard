import { createContext, useContext } from "react";
import type { Agent, Project } from "../../api/types";

export interface ShellApi {
  project: Project;
  agents: Agent[];
  agentById: (id: number | null | undefined) => Agent | undefined;
  openChat: (agentId: number) => void;
  openTask: (taskId: number) => void;
}

export const ShellContext = createContext<ShellApi | null>(null);

export function useShell(): ShellApi {
  const ctx = useContext(ShellContext);
  if (!ctx) throw new Error("useShell outside of ShellContext");
  return ctx;
}
