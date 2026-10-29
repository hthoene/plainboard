import clsx from "clsx";
import { Shield } from "lucide-react";
import { useTranslation } from "react-i18next";
import { formatTokens } from "../../lib/format";
import { useIsDesktop } from "../../lib/hooks";
import { useShell } from "../layout/shell-context";
import { ChatPanel } from "./ChatPanel";

/** Full-page chat: agent list + large conversation (agent switcher in the header on phones). */
export function ChatView({ agentId, onSelectAgent }: { agentId: number; onSelectAgent: (id: number) => void }) {
  const { t } = useTranslation();
  const { agents } = useShell();
  const isDesktop = useIsDesktop();

  return (
    <div className="flex h-full">
      {isDesktop && (
        <aside className="scroll-thin flex w-64 shrink-0 flex-col overflow-y-auto border-r border-zinc-200 bg-zinc-50/60 p-2 dark:border-zinc-800 dark:bg-zinc-950">
          <div className="px-2 pt-1 pb-2 text-xs font-semibold tracking-wide text-zinc-500 uppercase">{t("chat.conversations")}</div>
          {agents.map((agent) => (
            <button
              key={agent.id}
              onClick={() => onSelectAgent(agent.id)}
              className={clsx(
                "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left",
                agent.id === agentId ? "bg-white shadow-sm ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-800" : "hover:bg-zinc-100 dark:hover:bg-zinc-900",
              )}
            >
              <span className="relative flex size-8 shrink-0 items-center justify-center rounded-lg text-xs font-semibold text-white" style={{ background: agent.color }}>
                {agent.name[0]?.toUpperCase()}
                {agent.status !== "idle" && (
                  <span
                    className={clsx(
                      "absolute -right-1 -bottom-1 size-3 rounded-full ring-2 ring-white dark:ring-zinc-950",
                      agent.status === "running" ? "animate-pulse bg-emerald-500" : "bg-amber-400",
                    )}
                    title={t(`agentStatus.${agent.status}`)}
                  />
                )}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1 truncate text-sm font-medium">
                  {agent.name}
                  {agent.is_manager && <Shield className="size-3 shrink-0 text-indigo-500" />}
                </span>
                <span className="block truncate text-xs text-zinc-500">
                  {agent.role || "—"} · {formatTokens(agent.context_tokens)}
                </span>
              </span>
            </button>
          ))}
        </aside>
      )}
      <section className="flex min-w-0 flex-1 flex-col bg-white dark:bg-zinc-900">
        <ChatPanel agentId={agentId} onSelectAgent={onSelectAgent} variant="page" />
      </section>
    </div>
  );
}
