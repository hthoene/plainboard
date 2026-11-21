import type { ReactElement } from "react";
import { useTranslation } from "react-i18next";
import type { MachineUsage } from "../../api/types";

const WINDOWS = [1, 6, 24];
const SHADES = [0.85, 0.55, 0.35, 0.7, 0.45, 0.25];
const H = 40;

/** Cores as a share of the machine: "12 %", "<1 %" for a trace, "–" for nothing. */
function percent(cores: number | undefined, machine: number): string {
  if (!cores || !machine) return "–";
  const p = (cores / machine) * 100;
  return p < 1 ? "<1 %" : `${Math.round(p)} %`;
}

/** Stacked bars, one per slot, one series per agent (and the rest of the project); the scale is given, so charts compare. */
function Stack({ series, max, label }: { series: number[][]; max: number; label: (slot: number) => string }) {
  const slots = Math.max(0, ...series.map((s) => s.length));
  const bars: ReactElement[] = [];
  for (let i = 0; i < slots; i++) {
    let y = H;
    series.forEach((s, j) => {
      const h = ((s[i] ?? 0) / max) * H;
      if (h < 0.15) return;
      y -= h;
      bars.push(<rect key={`${i}-${j}`} x={i} y={y} width={1} height={h} fill="currentColor" opacity={SHADES[j % SHADES.length]} />);
    });
  }
  return (
    <svg viewBox={`0 0 ${Math.max(slots, 1)} ${H}`} preserveAspectRatio="none" className="h-10 w-full text-fg-2" role="img" aria-label="CPU">
      <line x1={0} x2={slots} y1={H - 0.25} y2={H - 0.25} stroke="currentColor" strokeOpacity={0.3} strokeWidth={0.5} vectorEffect="non-scaling-stroke" />
      {bars}
      {Array.from({ length: slots }, (_, i) => (
        <rect key={i} x={i} y={0} width={1} height={H} fill="transparent">
          <title>{label(i)}</title>
        </rect>
      ))}
    </svg>
  );
}

/** The host's load average over 24 hours as a line, against its number of cores (the dashed line). */
function LoadLine({ load, cores, label }: { load: number[]; cores: number; label: (slot: number) => string }) {
  const max = Math.max(cores, ...load) * 1.05;
  const n = Math.max(load.length, 2);
  const points = load.map((v, i) => `${i},${(H - (v / max) * H).toFixed(2)}`).join(" ");
  const top = H - (cores / max) * H;
  return (
    <svg viewBox={`0 0 ${n} ${H}`} preserveAspectRatio="none" className="h-14 w-full text-fg" role="img" aria-label="Load">
      <line x1={0} x2={n} y1={top} y2={top} stroke="currentColor" strokeOpacity={0.35} strokeWidth={1} strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth={1.25} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      {load.map((_, i) => (
        <rect key={i} x={i} y={0} width={1} height={H} fill="transparent">
          <title>{label(i)}</title>
        </rect>
      ))}
    </svg>
  );
}

const sum = (series: number[][], i: number) => series.reduce((a, s) => a + (s[i] ?? 0), 0);

/** CPU per project and agent: now and averaged over 1, 6 and 24 hours, a stacked chart per project, and the host's load. */
export function CpuUsage({ data }: { data: MachineUsage }) {
  const { t } = useTranslation();
  const cores = data.cpu_cores;
  if (!cores) return null;
  const start = data.cpu_start ? new Date(data.cpu_start).getTime() : null;
  const time = (i: number) => (start == null ? "" : new Date(start + i * data.cpu_step * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  const rows = data.projects.map((p) => {
    const series = [...p.agents.map((a) => a.cpu_history), p.other_cpu_history];
    const slots = Math.max(0, ...series.map((s) => s.length));
    const peak = Math.max(0, ...Array.from({ length: slots }, (_, i) => sum(series, i)));
    return { p, series, peak, now: p.agents.reduce((a, x) => a + x.cpu, 0) + p.other_cpu, avg: WINDOWS.map((_, k) => p.agents.reduce((a, x) => a + (x.cpu_avg[k] ?? 0), 0) + (p.other_cpu_avg[k] ?? 0)) };
  });
  const max = Math.max(0.05, ...rows.map((r) => r.peak));
  const empty = start == null;
  const figures = (now: number, avg: number[], className = "") => (
    <>
      <span className={`text-right whitespace-nowrap ${className}`}>{percent(now, cores)}</span>
      {avg.map((v, k) => (
        <span key={k} className="w-10 text-right whitespace-nowrap text-fg-2">
          {percent(v, cores)}
        </span>
      ))}
    </>
  );
  const grid = "grid grid-cols-[minmax(0,1fr)_auto_auto_auto_auto] gap-x-3";
  return (
    <section>
      <div className={`${grid} rule-b pb-1.5`}>
        <h3 className="caps">{t("usage.cpu")}</h3>
        <span className="caps w-10 text-right">{t("usage.cpuNow")}</span>
        {WINDOWS.map((h) => (
          <span key={h} className="caps w-10 text-right">
            {t("usage.cpuWindow", { hours: h })}
          </span>
        ))}
      </div>
      {empty && <p className="py-1.5 text-fg-3">{t("usage.cpuNone")}</p>}
      {data.cpu_load.length > 0 && (
        <div className="border-b border-line py-2">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3">
            <span className="font-semibold">{t("usage.cpuLoadFigures", { load: data.cpu_load[0].toFixed(2), cores })}</span>
            <span className="whitespace-nowrap text-fg-2">{data.cpu_load.map((v) => v.toFixed(2)).join(" · ")}</span>
          </div>
          {!empty && <LoadLine load={data.cpu_load_history} cores={cores} label={(i) => `${time(i)}: ${(data.cpu_load_history[i] ?? 0).toFixed(2)}`} />}
        </div>
      )}
      {rows.map(({ p, series, peak, now, avg }) => (
        <div key={p.id} className="pt-3">
          <div className={`${grid} border-b border-line pb-1.5`}>
            <h4 className="truncate font-extrabold">{p.name}</h4>
            {figures(now, avg, "w-10 font-semibold")}
          </div>
          {!empty && peak > 0 && (
            <div className="border-b border-line py-1.5">
              <Stack series={series} max={max} label={(i) => t("usage.cpuSlot", { time: time(i), percent: Math.round((sum(series, i) / cores) * 100) })} />
              <p className="text-fg-3">{t("usage.cpuPeak", { percent: Math.max(1, Math.round((peak / cores) * 100)) })}</p>
            </div>
          )}
          {p.agents.map((a) => (
            <div key={a.id} className={`${grid} border-b border-line py-1.5`}>
              <span className="truncate">{a.name}</span>
              {figures(a.cpu, a.cpu_avg, "w-10")}
            </div>
          ))}
          {(p.other_cpu > 0 || p.other_cpu_avg.some((v) => v > 0)) && (
            <div className={`${grid} border-b border-line py-1.5 text-fg-2`}>
              <span className="truncate">{t("usage.projectOther")}</span>
              {figures(p.other_cpu, p.other_cpu_avg, "w-10")}
            </div>
          )}
        </div>
      ))}
      {(data.cpu_other > 0 || data.cpu_other_avg.some((v) => v > 0)) && (
        <div className={`${grid} border-b border-line py-1.5 text-fg-2`}>
          <span className="truncate">{t("usage.cpuRest")}</span>
          {figures(data.cpu_other, data.cpu_other_avg, "w-10")}
        </div>
      )}
      {!empty && (
        <div className="flex justify-between pt-1 text-fg-3">
          <span>{t("usage.cpuAxis")}</span>
          <span>{t("usage.cpuAxisNow")}</span>
        </div>
      )}
      <p className="mt-2 text-fg-3">{t("usage.cpuHint", { cores })}</p>
    </section>
  );
}
