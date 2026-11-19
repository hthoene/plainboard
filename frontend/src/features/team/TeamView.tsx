import { GitBranch, HardDrive, ListTodo, MessageSquare, Plus, Shield, StickyNote } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMe, useRepositories, useUsage } from "../../api/queries";
import type { Agent } from "../../api/types";
import { AgentDot, AgentSquare, Badge, Button, PageHead } from "../../components/ui";
import { formatBytes, formatCost, formatTokens } from "../../lib/format";
import { useShell } from "../layout/shell-context";
import { AgentEditor } from "./AgentEditor";
import { OwnNotesDialog } from "./OwnNotesDialog";
import { TodoDialog } from "./TodoDialog";

/** The team as a ruled list: who, what for, on which model, and how full its context is. */
export function TeamView() {
  const { t } = useTranslation();
  const { agents, openChat, project } = useShell();
  const repos = useRepositories(project.id);
  const me = useMe();
  const usage = useUsage(!!me?.is_admin); // the whole installation's: for admins only
  const usageOf = (agent: Agent) => usage.data?.projects.find((p) => p.id === project.id)?.agents.find((a) => a.id === agent.id);
  const repoNames = (agent: Agent) =>
    agent.all_repos ? (repos.data ?? []).map((r) => r.name) : (repos.data ?? []).filter((r) => agent.repository_ids.includes(r.id)).map((r) => r.name);
  const [editing, setEditing] = useState<Agent | "new" | null>(null);
  const [todosOf, setTodosOf] = useState<number | null>(null);
  const todoAgent = agents.find((a) => a.id === todosOf);
  const [notesOf, setNotesOf] = useState<number | null>(null);
  const notesAgent = agents.find((a) => a.id === notesOf);

  return (
    <div className="flex h-full flex-col">
      <PageHead title={t("team.title")}>
        <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
          {t("team.add")}
        </Button>
      </PageHead>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
      <div className="max-w-5xl px-4 pt-3 pb-6">
        <p className="mb-4 text-fg-2">{t("team.subtitle")}</p>
        <div className="rule-t">
          {agents.map((agent) => (
            <article
              key={agent.id}
              onClick={() => setEditing(agent)}
              className="grid cursor-pointer grid-cols-[9px_auto_minmax(0,1fr)] gap-x-2.5 gap-y-2 border-b border-line py-3 hover:bg-sunk xl:grid-cols-[9px_auto_minmax(0,13rem)_minmax(0,1fr)_auto]"
            >
              <AgentDot status={agent.status} className="mt-1.5" />
              <AgentSquare agent={agent} className="mt-px" />
              <div className="min-w-0">
                <div className="flex items-center gap-1.5">
                  <h2 className="truncate font-extrabold">{agent.name}</h2>
                  {agent.is_manager && <Shield className="size-3.5 shrink-0 text-fg-3" aria-label={t("team.manager")} />}
                  {!agent.enabled && <Badge tone="red">{t("team.disabled")}</Badge>}
                </div>
                <p className="truncate text-fg-2">{agent.role || "—"}</p>
                <p className="text-fg-3">{t(`agentStatus.${agent.status}`)}</p>
              </div>
              <div className="col-start-3 min-w-0 xl:col-start-auto">
                <p className="line-clamp-2 text-fg-2">{agent.description || t("team.noDescription")}</p>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-fg-3">
                  <Badge>{agent.model}</Badge>
                  {agent.effort && <Badge>{agent.effort}</Badge>}
                  <Badge>{t(`toolset.${agent.toolset}`)}</Badge>
                  <span className="whitespace-nowrap" title={t("team.contextTitle")}>
                    {formatTokens(agent.context_tokens)} / {formatTokens(agent.context_limit_tokens)} · {formatCost(agent.total_cost_usd)}
                  </span>
                  {(() => {
                    const own = usageOf(agent);
                    if (!own || (own.disk == null && !own.memory)) return null;
                    return (
                      <span className="flex items-center gap-1 whitespace-nowrap" title={t("usage.agentFigures")}>
                        <HardDrive className="size-3.5 shrink-0" />
                        {[own.disk != null && formatBytes(own.disk), own.memory > 0 && formatBytes(own.memory)].filter(Boolean).join(" · ")}
                      </span>
                    );
                  })()}
                </div>
                {!agent.is_manager && (repos.data?.length ?? 0) > 0 && (
                  <p className="mt-1.5 flex items-center gap-1.5 text-fg-3">
                    <GitBranch className="size-3.5 shrink-0" />
                    <span className="truncate">{repoNames(agent).join(", ") || t("team.noRepos")}</span>
                  </p>
                )}
              </div>
              <div className="col-start-3 -ml-2.5 flex flex-wrap items-start gap-1 xl:col-start-auto xl:ml-0 xl:justify-end">
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<StickyNote className="size-3.5" />}
                  onClick={(e) => {
                    e.stopPropagation();
                    setNotesOf(agent.id);
                  }}
                >
                  {t("ownNotes.button")}
                  {agent.own_notes > 0 && <span className="text-fg-3">{agent.own_notes}</span>}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<ListTodo className="size-3.5" />}
                  onClick={(e) => {
                    e.stopPropagation();
                    setTodosOf(agent.id);
                  }}
                >
                  {t("todos.button")}
                  {agent.open_todos > 0 && <span className="text-fg-3">{agent.open_todos}</span>}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<MessageSquare className="size-3.5" />}
                  onClick={(e) => {
                    e.stopPropagation();
                    openChat(agent.id);
                  }}
                >
                  {t("team.chat")}
                </Button>
              </div>
            </article>
          ))}
        </div>
      </div>
      </div>
      {todoAgent && <TodoDialog agent={todoAgent} onClose={() => setTodosOf(null)} />}
      {notesAgent && <OwnNotesDialog agent={notesAgent} onClose={() => setNotesOf(null)} />}
      {editing && <AgentEditor agent={editing === "new" ? undefined : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
