import { Check, Loader2 } from "lucide-react";
import { useEffect, useReducer } from "react";
import { useTranslation } from "react-i18next";
import { usePause, useRuns } from "../../api/queries";
import type { Agent, Run } from "../../api/types";
import { useShell } from "../layout/shell-context";

const LINGER_MS = 30_000;

type Row = { key: string; agent: Agent | undefined; inTurn: boolean; busy: boolean };

/** One row per agent: its latest turn that was asked to stop, while that stop still matters. A turn that
 *  started after it means the agent works again. Its background jobs are stopped only while the project
 *  rests (``resting``: a person's pause, "pause everything", the limit pause on this instance); otherwise they go on by design. */
function currentStop(runs: Run[], agents: Agent[], now: number, resting: boolean): Row[] {
  const sorted = [...runs].sort((a, b) => a.id - b.id);
  const latestRun = new Map(sorted.map((r) => [r.agent_id, r.id]));
  const asked = new Map(sorted.filter((r) => r.stop_requested_at).map((r) => [r.agent_id, r]));
  return [...asked.values()].flatMap((run) => {
    if (latestRun.get(run.agent_id) !== run.id) return [];
    const agent = agents.find((a) => a.id === run.agent_id);
    const inTurn = !run.finished_at;
    const jobsStopping = resting && (agent?.running_jobs ?? 0) > 0;
    if (!inTurn && !jobsStopping && now - Date.parse(run.finished_at!) >= LINGER_MS) return [];
    return [{ key: `run-${run.id}`, agent, inTurn, busy: inTurn || jobsStopping }];
  });
}

/** Live overview while agents wrap up after being asked to stop, including their background jobs. */
export function StoppingAgents() {
  const { t } = useTranslation();
  const { project, agents } = useShell();
  const pausedAll = !!usePause().data?.paused;
  // Before its limits, the jobs stop too where the project runs on this instance (#187).
  const resting = !!project.paused_at || pausedAll || (!!project.limit_paused_at && !project.runner_id);
  const stop = currentStop(useRuns(project.id).data ?? [], agents, Date.now(), resting);
  // Paused while idle: no turn was told to stop, but a job can still be running.
  const pausedWithJobs = agents.filter((a) => !a.enabled && a.running_jobs > 0 && !stop.some((r) => r.agent?.id === a.id));
  const [, tick] = useReducer((n: number) => n + 1, 0);
  const visible = stop.length + pausedWithJobs.length > 0;

  useEffect(() => {
    if (!visible) return;
    const timer = setInterval(tick, 5000); // nothing else re-renders this when the overview should disappear
    return () => clearInterval(timer);
  }, [visible]);

  if (!visible) return null;
  const rows = [...stop, ...pausedWithJobs.map((agent) => ({ key: `agent-${agent.id}`, agent, inTurn: false, busy: true }))].map((row) => {
    const jobs = row.agent?.running_jobs ?? 0;
    const states = [
      ...(row.inTurn ? [t("chat.stopping.working")] : row.busy ? [] : [t(row.agent?.enabled ? "chat.stopping.stopped" : "chat.stopping.paused")]),
      ...(jobs > 0 ? [t("chat.jobsRunning", { count: jobs })] : []),
    ];
    return { ...row, state: states.join(" · ") };
  });
  const working = rows.filter((row) => row.busy).length;

  return (
    <div role="status" className="mb-2 rounded-sm px-2.5 py-2 ring-1 ring-line-2 ring-inset">
      <p className="font-semibold">
        {working > 0 ? t("chat.stopping.active", { count: rows.length, done: rows.length - working }) : t("chat.stopping.finished", { count: rows.length })}
      </p>
      <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
        {rows.map((row) => (
          <li key={row.key} className="flex items-center gap-1.5 text-fg-2">
            {row.busy ? <Loader2 className="size-4 shrink-0 animate-spin text-fg" /> : <Check className="size-4 shrink-0 text-ok" />}
            <span>
              {row.agent?.name} · {row.state}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
