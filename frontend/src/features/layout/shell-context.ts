import { createContext, useContext } from "react";
import type { Agent, Preview, Project } from "../../api/types";

export interface ShellApi {
  project: Project;
  /** The signed-in user may change things in this project (not a viewer). */
  canWrite: boolean;
  /** ... and its settings, repositories, secrets and members. */
  isOwner: boolean;
  agents: Agent[];
  agentById: (id: number | null | undefined) => Agent | undefined;
  /** The servers agents have left running, to open as a preview. */
  previews: Preview[];
  openChat: (agentId: number) => void;
  openTask: (taskId: number) => void;
  /** PlainBoard settings, at a section (admins only: others see no settings). */
  openSettings: (section?: string) => void;
  /** Show an address of the agents' environment (e.g. http://localhost:5173/) in the browser window. */
  openBrowser: (address: string) => void;
}

export const ShellContext = createContext<ShellApi | null>(null);

export function useShell(): ShellApi {
  const ctx = useContext(ShellContext);
  if (!ctx) throw new Error("useShell outside of ShellContext");
  return ctx;
}
