// MOCK page for comparing the landing-state variants. Not part of the app.
import { DndContext } from "@dnd-kit/core";
import { SortableContext } from "@dnd-kit/sortable";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/sofia-sans-semi-condensed";
import "@fontsource-variable/inter";
import "./i18n";
import "./index.css";
import "./designs/classic.css";
import "./designs/slop2gether.css";
import { DESIGNS } from "./lib/theme";
import type { Agent, Project, Task } from "./api/types";
import { TaskCard } from "./features/board/TaskCard";
import { ShellContext, type ShellApi } from "./features/layout/shell-context";

const q = new URLSearchParams(location.search);
const design = DESIGNS.find((d) => d.id === q.get("d")) ?? DESIGNS[0];
const root = document.documentElement;
root.dataset.design = design.id;
root.dataset.theme = (q.get("m") === "light" ? design.light : design.dark).theme;
root.dataset.landingVariant = q.get("v") ?? "a";

const agent = { id: 1, name: "Frontend Engineer", color: "#3b82f6" } as Agent;
const project = { id: 1, label_colors: { ui: "blue", backend: "green" } } as unknown as Project;
const shell = { project, canWrite: false, agents: [agent], agentById: (id: number) => (id === 1 ? agent : undefined), openTask: () => {} } as unknown as ShellApi;
const now = new Date().toISOString();
const base = { project_id: 1, description: "", customer_title: "", customer_description: "", customer_texts: {}, status: "in_progress", position: 0, priority: "medium", labels: [], user_action: "", archived_at: null, created_by_agent_id: null, created_at: now, updated_at: now, comment_count: 0, attachment_count: 0, active_agent_ids: [], manager_active: false, open_decisions: 0 } as unknown as Task;
const tasks = [
  { ...base, id: 1, number: 61, title: "Retry failed webhook deliveries", labels: ["backend"], comment_count: 2 },
  { ...base, id: 2, number: 70, title: "Ticket card shows that the work is finished and only checked", priority: "high", labels: ["ui"], comment_count: 3, attachment_count: 1, landing: { phase: "checking", since: now } },
  { ...base, id: 3, number: 68, title: "Fix the sorting of the stats table", landing: { phase: "landing", since: now } },
  { ...base, id: 4, number: 66, title: "Settings: add a section for runners", labels: ["ui"], active_agent_ids: [1] },
  { ...base, id: 5, number: 64, title: "Which database for the pilot?", open_decisions: 1 },
] as Task[];

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } })}>
    <ShellContext.Provider value={shell}>
      <DndContext>
        <SortableContext items={tasks.map((t) => `t-${t.id}`)}>
          <div className="flex h-screen flex-col bg-bg">
            <h2 className="caps rule-b px-2.5 py-3">In progress</h2>
            <div data-part="column-body" className="flex-1">
              {tasks.map((t) => <TaskCard key={t.id} task={t} />)}
            </div>
          </div>
        </SortableContext>
      </DndContext>
    </ShellContext.Provider>
  </QueryClientProvider>,
);
