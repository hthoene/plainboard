import clsx from "clsx";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useStats } from "../../api/queries";
import type { StatsDay } from "../../api/types";
import { AgentDot, Spinner } from "../../components/ui";
import { useShell } from "../layout/shell-context";

const RANGES = [7, 30, 90];
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const money = (value: number) => `$${value.toFixed(2)}`;
const count = (value: number) => compact.format(value);
const shortDate = (date: string) => new Date(`${date}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
const sum = (days: StatsDay[], pick: (day: StatsDay) => number) => days.reduce((total, day) => total + pick(day), 0);

function useDuration() {
  const { t } = useTranslation();
  return (seconds: number) => {
    if (seconds < 3600) return t("stats.minutes", { value: Math.round(seconds / 60) });
    if (seconds < 172800) return t("stats.hours", { value: (seconds / 3600).toFixed(1) });
    return t("stats.days", { value: (seconds / 86400).toFixed(1) });
  };
}

export function StatsView() {
  const { t } = useTranslation();
  const { project, agentById } = useShell();
  const [range, setRange] = useState(30);
  const stats = useStats(project.id, range);
  const duration = useDuration();
  const data = stats.data;

  return (
    <div className="scroll-thin h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-4 sm:px-6 sm:py-6">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">{t("stats.title")}</h1>
            <p className="text-sm text-zinc-500">{t("stats.subtitle")}</p>
          </div>
          <div className="flex gap-0.5 rounded-lg bg-zinc-100 p-0.5 dark:bg-zinc-800">
            {RANGES.map((days) => (
              <button
                key={days}
                type="button"
                aria-pressed={range === days}
                onClick={() => setRange(days)}
                className={clsx("rounded-md px-2.5 py-1 text-xs font-medium", range === days ? "bg-white shadow-sm dark:bg-zinc-900" : "text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100")}
              >
                {t("stats.lastDays", { count: days })}
              </button>
            ))}
          </div>
        </div>
        {!data ? (
          <div className="flex justify-center py-12">
            <Spinner />
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Tile label={t("stats.ticketsDone")} value={count(sum(data.days, (d) => d.tickets_done))} />
              <Tile label={t("stats.ticketsCreated")} value={count(sum(data.days, (d) => d.tickets_created))} />
              <Tile label={t("stats.openTickets")} value={count(data.open_tickets)} />
              <Tile label={t("stats.cycleTime")} value={data.median_cycle_seconds === null ? "–" : duration(data.median_cycle_seconds)} hint={t("stats.cycleHint")} />
              <Tile label={t("stats.runs")} value={count(sum(data.days, (d) => d.runs))} hint={t("stats.failedRuns", { count: sum(data.days, (d) => d.failed_runs) })} />
              <Tile label={t("stats.tokens")} value={count(sum(data.days, (d) => d.tokens))} />
              <Tile label={t("stats.cost")} value={money(sum(data.days, (d) => d.cost_usd))} />
            </div>
            <BarChart
              title={t("stats.ticketsChart")}
              days={data.days}
              format={String}
              series={[
                { label: t("stats.ticketsDone"), color: "bg-indigo-500", pick: (d) => d.tickets_done },
                { label: t("stats.ticketsCreated"), color: "bg-emerald-600", pick: (d) => d.tickets_created },
              ]}
            />
            <div className="grid gap-4 md:grid-cols-3">
              <BarChart title={t("stats.runsChart")} days={data.days} format={String} series={[{ label: t("stats.runs"), color: "bg-indigo-500", pick: (d) => d.runs }]} />
              <BarChart title={t("stats.tokensChart")} days={data.days} format={count} series={[{ label: t("stats.tokens"), color: "bg-indigo-500", pick: (d) => d.tokens }]} />
              <BarChart title={t("stats.costChart")} days={data.days} format={money} series={[{ label: t("stats.cost"), color: "bg-indigo-500", pick: (d) => d.cost_usd }]} />
            </div>
            <section className="rounded-xl bg-white p-4 ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-800">
              <h2 className="mb-3 text-sm font-semibold">{t("stats.agents")}</h2>
              {data.agents.length === 0 ? (
                <p className="text-sm text-zinc-500">{t("stats.noAgents")}</p>
              ) : (
                <div className="scroll-thin overflow-x-auto">
                  <table className="w-full text-sm tabular-nums">
                    <thead>
                      <tr className="text-left text-xs text-zinc-500 [&>th]:pr-4 [&>th]:pb-2 [&>th]:font-medium [&>th]:whitespace-nowrap">
                        <th>{t("stats.agent")}</th>
                        <th>{t("stats.runs")}</th>
                        <th>{t("stats.failed")}</th>
                        <th>{t("stats.assignmentsDone")}</th>
                        <th>{t("stats.busy")}</th>
                        <th>{t("stats.tokens")}</th>
                        <th>{t("stats.cost")}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
                      {data.agents.map((row) => {
                        const agent = agentById(row.agent_id);
                        return (
                          <tr key={row.agent_id} className="[&>td]:py-1.5 [&>td]:pr-4 [&>td]:whitespace-nowrap">
                            <td>
                              <span className="flex items-center gap-2 font-medium">
                                {agent && <AgentDot color={agent.color} />}
                                {agent?.name ?? "?"}
                              </span>
                            </td>
                            <td>{row.runs}</td>
                            <td>{row.failed_runs}</td>
                            <td>{row.assignments_done}</td>
                            <td>{duration(row.busy_seconds)}</td>
                            <td>{count(row.tokens)}</td>
                            <td>{money(row.cost_usd)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-xl bg-white p-3 ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-800">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="text-xs text-zinc-500">{hint}</div>}
    </div>
  );
}

interface Series {
  label: string;
  color: string;
  pick: (day: StatsDay) => number;
}

function BarChart({ title, days, series, format }: { title: string; days: StatsDay[]; series: Series[]; format: (value: number) => string }) {
  const { t } = useTranslation();
  const [hovered, setHovered] = useState<number | null>(null);
  const highest = Math.max(0, ...days.flatMap((day) => series.map((s) => s.pick(day))));
  const place = hovered === null ? 0 : ((hovered + 0.5) / days.length) * 100;

  return (
    <figure className="rounded-xl bg-white p-4 ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-800">
      <figcaption className="mb-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <span className="text-sm font-semibold">{title}</span>
        {series.length > 1 && (
          <span className="flex flex-wrap gap-3 text-xs text-zinc-500">
            {series.map((s) => (
              <span key={s.label} className="flex items-center gap-1.5">
                <span className={clsx("size-2 rounded-sm", s.color)} />
                {s.label}
              </span>
            ))}
          </span>
        )}
      </figcaption>
      <div className="relative" aria-hidden onMouseLeave={() => setHovered(null)}>
        <div className="absolute inset-x-0 top-0 border-t border-dashed border-zinc-200 dark:border-zinc-800" />
        <span className="absolute top-0.5 right-0 text-[10px] text-zinc-400 tabular-nums">{format(highest)}</span>
        <div className="flex h-28 items-stretch border-b border-zinc-200 dark:border-zinc-700">
          {days.map((day, index) => (
            <div key={day.date} onMouseEnter={() => setHovered(index)} onClick={() => setHovered(index)} className={clsx("flex min-w-0 flex-1 items-end justify-center gap-px rounded-t px-px", hovered === index && "bg-zinc-100 dark:bg-zinc-800/60")}>
              {series.map((s) => {
                const value = s.pick(day);
                return <div key={s.label} className={clsx("max-w-3 min-w-0 flex-1 rounded-t-[3px]", s.color)} style={{ height: highest ? `${(value / highest) * 100}%` : 0, minHeight: value > 0 ? 2 : 0 }} />;
              })}
            </div>
          ))}
        </div>
        {hovered !== null && (
          <div className="pointer-events-none absolute top-1 z-10 rounded-lg bg-white px-2.5 py-1.5 text-xs whitespace-nowrap shadow-lg ring-1 ring-zinc-200 dark:bg-zinc-800 dark:ring-zinc-700" style={{ left: `${place}%`, transform: `translateX(-${place}%)` }}>
            <div className="font-medium">{shortDate(days[hovered].date)}</div>
            {series.map((s) => (
              <div key={s.label} className="flex items-center gap-1.5 text-zinc-600 dark:text-zinc-300">
                <span className={clsx("size-2 rounded-sm", s.color)} />
                {s.label}: <span className="font-medium tabular-nums">{format(s.pick(days[hovered]))}</span>
              </div>
            ))}
          </div>
        )}
        <div className="mt-1 flex justify-between text-[10px] text-zinc-400">
          <span>{shortDate(days[0].date)}</span>
          <span>{shortDate(days[days.length - 1].date)}</span>
        </div>
      </div>
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th>{t("stats.date")}</th>
            {series.map((s) => (
              <th key={s.label}>{s.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.date}>
              <td>{shortDate(day.date)}</td>
              {series.map((s) => (
                <td key={s.label}>{format(s.pick(day))}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
