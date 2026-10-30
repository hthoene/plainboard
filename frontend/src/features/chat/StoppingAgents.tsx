import { Check, Loader2 } from "lucide-react";
import { useEffect, useReducer } from "react";
import { useTranslation } from "react-i18next";
import { useRuns } from "../../api/queries";
import type { Run } from "../../api/types";
import { useShell } from "../layout/shell-context";

const LINGER_MS = 30_000;

/** The runs of the current stop: all that were asked to stop since the earliest one still relevant. */
function currentStop(runs: Run[], now: number): Run[] {
  const asked = runs.filter((r) => r.stop_requested_at);
  const relevant = asked.filter((r) => !r.finished_at || now - Date.parse(r.finished_at) < LINGER_MS);
  if (!relevant.length) return [];
  const start = Math.min(...relevant.map((r) => Date.parse(r.stop_requested_at!)));
  return asked.filter((r) => !r.finished_at || Date.parse(r.finished_at) >= start).sort((a, b) => a.id - b.id);
}

/** Live overview while agents wrap up after being asked to stop. */
export function StoppingAgents() {
  const { t } = useTranslation();
  const { project, agentById } = useShell();
  const stop = currentStop(useRuns(project.id).data ?? [], Date.now());
  const [, tick] = useReducer((n: number) => n + 1, 0);
  const visible = stop.length > 0;

  useEffect(() => {
    if (!visible) return;
    const timer = setInterval(tick, 5000); // nothing else re-renders this when the overview should disappear
    return () => clearInterval(timer);
  }, [visible]);

  if (!visible) return null;
  const working = stop.filter((r) => !r.finished_at).length;

  return (
    <div role="status" className="mb-2 rounded-lg bg-zinc-100 px-2.5 py-2 text-xs dark:bg-zinc-800/60">
      <p className="font-medium">
        {working > 0 ? t("chat.stopping.active", { count: stop.length, done: stop.length - working }) : t("chat.stopping.finished", { count: stop.length })}
      </p>
      <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
        {stop.map((run) => {
          const agent = agentById(run.agent_id);
          return (
            <li key={run.id} className="flex items-center gap-1 text-zinc-600 dark:text-zinc-400">
              {run.finished_at ? <Check className="size-3 shrink-0 text-green-600 dark:text-green-500" /> : <Loader2 className="size-3 shrink-0 animate-spin text-indigo-500" />}
              <span>
                {agent?.name} · {t(!run.finished_at ? "chat.stopping.working" : agent?.enabled ? "chat.stopping.stopped" : "chat.stopping.paused")}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
