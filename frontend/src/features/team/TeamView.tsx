import { GitBranch, MessageSquare, Plus, Shield } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useRepositories } from "../../api/queries";
import type { Agent } from "../../api/types";
import { AgentDot, Badge, Button } from "../../components/ui";
import { formatCost, formatTokens } from "../../lib/format";
import { useShell } from "../layout/shell-context";
import { AgentEditor } from "./AgentEditor";

export function TeamView() {
  const { t } = useTranslation();
  const { agents, openChat, project } = useShell();
  const repos = useRepositories(project.id);
  const repoNames = (agent: Agent) =>
    agent.all_repos ? (repos.data ?? []).map((r) => r.name) : (repos.data ?? []).filter((r) => agent.repository_ids.includes(r.id)).map((r) => r.name);
  const [editing, setEditing] = useState<Agent | "new" | null>(null);

  return (
    <div className="scroll-thin h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-4 sm:px-6 sm:py-6">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">{t("team.title")}</h1>
            <p className="text-sm text-zinc-500">{t("team.subtitle")}</p>
          </div>
          <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
            {t("team.add")}
          </Button>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {agents.map((agent) => (
            <article
              key={agent.id}
              onClick={() => setEditing(agent)}
              className="group cursor-pointer rounded-xl bg-white p-4 ring-1 ring-zinc-200 transition-shadow hover:shadow-md dark:bg-zinc-900 dark:ring-zinc-800"
            >
              <div className="flex items-start gap-3">
                <div className="flex size-9 shrink-0 items-center justify-center rounded-lg text-sm font-semibold text-white" style={{ background: agent.color }}>
                  {agent.name[0]?.toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <h2 className="truncate font-semibold">{agent.name}</h2>
                    {agent.is_manager && <Shield className="size-3.5 text-indigo-500" aria-label={t("team.manager")} />}
                  </div>
                  <p className="truncate text-xs text-zinc-500">{agent.role || "—"}</p>
                </div>
                <div className="flex items-center gap-1.5 text-xs text-zinc-500">
                  <AgentDot color={agent.color} status={agent.status} />
                  {t(`agentStatus.${agent.status}`)}
                </div>
              </div>
              <p className="mt-3 line-clamp-2 min-h-10 text-sm text-zinc-600 dark:text-zinc-400">{agent.description || t("team.noDescription")}</p>
              <div className="mt-3 flex flex-wrap items-center gap-1.5">
                <Badge>{agent.model}</Badge>
                {agent.effort && <Badge tone="sky">{agent.effort}</Badge>}
                <Badge tone={agent.toolset === "full" ? "indigo" : "zinc"}>{t(`toolset.${agent.toolset}`)}</Badge>
                {!agent.enabled && <Badge tone="red">{t("team.disabled")}</Badge>}
              </div>
              {!agent.is_manager && (repos.data?.length ?? 0) > 0 && (
                <p className="mt-2 flex items-center gap-1.5 truncate text-xs text-zinc-500">
                  <GitBranch className="size-3 shrink-0" />
                  {repoNames(agent).join(", ") || t("team.noRepos")}
                </p>
              )}
              <div className="mt-3 flex items-center justify-between border-t border-zinc-100 pt-3 text-xs text-zinc-500 dark:border-zinc-800">
                <span title={t("team.contextTitle")}>
                  {formatTokens(agent.context_tokens)} / {formatTokens(agent.context_limit_tokens)} · {formatCost(agent.total_cost_usd)}
                </span>
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
      {editing && <AgentEditor agent={editing === "new" ? undefined : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
