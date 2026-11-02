import { Check, Loader2 } from "lucide-react";
import { useEffect, useReducer } from "react";
import { useTranslation } from "react-i18next";
import { useRuns } from "../../api/queries";
import type { Agent, Run } from "../../api/types";
import { useShell } from "../layout/shell-context";

const LINGER_MS = 30_000;

/** The runs of the current stop: all that were asked to stop since the earliest one still relevant. */
function currentStop(runs: Run[], agents: Agent[], now: number): Run[] {
  const latestRun = new Map([...runs].sort((a, b) => a.id - b.id).map((r) => [r.agent_id, r.id]));
  // A job started in a later turn has nothing to do with this stop.
  const leftJobs = (run: Run) => latestRun.get(run.agent_id) === run.id && (agents.find((a) => a.id === run.agent_id)?.running_jobs ?? 0) > 0;
  const asked = runs.filter((r) => r.stop_requested_at);
  const relevant = asked.filter((r) => !r.finished_at || leftJobs(r) || now - Date.parse(r.finished_at) < LINGER_MS);
  if (!relevant.length) return [];
  const start = Math.min(...relevant.map((r) => Date.parse(r.stop_requested_at!)));
  return asked.filter((r) => !r.finished_at || Date.parse(r.finished_at) >= start).sort((a, b) => a.id - b.id);
}

/** Live overview while agents wrap up after being asked to stop, including their background jobs. */
export function StoppingAgents() {
  const { t } = useTranslation();
  const { project, agents, agentById } = useShell();
  const stop = currentStop(useRuns(project.id).data ?? [], agents, Date.now());
  // Paused while idle: no turn was told to stop, but a job can still be running.
  const pausedWithJobs = agents.filter((a) => !a.enabled && a.running_jobs > 0 && !stop.some((r) => r.agent_id === a.id));
  const [, tick] = useReducer((n: number) => n + 1, 0);
  const visible = stop.length + pausedWithJobs.length > 0;

  useEffect(() => {
    if (!visible) return;
    const timer = setInterval(tick, 5000); // nothing else re-renders this when the overview should disappear
    return () => clearInterval(timer);
  }, [visible]);

  if (!visible) return null;
  const rows = [
    ...stop.map((run) => ({ key: `run-${run.id}`, agent: agentById(run.agent_id), inTurn: !run.finished_at })),
    ...pausedWithJobs.map((agent) => ({ key: `agent-${agent.id}`, agent, inTurn: false })),
  ].map((row) => {
    const jobs = row.agent?.running_jobs ?? 0;
    const states = [...(row.inTurn ? [t("chat.stopping.working")] : []), ...(jobs > 0 ? [t("chat.jobsRunning", { count: jobs })] : [])];
    return { ...row, busy: states.length > 0, state: states.join(" · ") || t(row.agent?.enabled ? "chat.stopping.stopped" : "chat.stopping.paused") };
  });
  const working = rows.filter((row) => row.busy).length;

  return (
    <div role="status" className="mb-2 rounded-lg bg-zinc-100 px-2.5 py-2 text-xs dark:bg-zinc-800/60">
      <p className="font-medium">
        {working > 0 ? t("chat.stopping.active", { count: rows.length, done: rows.length - working }) : t("chat.stopping.finished", { count: rows.length })}
      </p>
      <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
        {rows.map((row) => (
          <li key={row.key} className="flex items-center gap-1 text-zinc-600 dark:text-zinc-400">
            {row.busy ? <Loader2 className="size-3 shrink-0 animate-spin text-indigo-500" /> : <Check className="size-3 shrink-0 text-green-600 dark:text-green-500" />}
            <span>
              {row.agent?.name} · {row.state}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
