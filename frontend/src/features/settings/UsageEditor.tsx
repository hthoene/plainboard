import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useUsage } from "../../api/queries";
import type { MachineUsage, RunnerUsage, Usage, UsageCleanup, UsageStorage } from "../../api/types";
import { Button, ErrorText, Spinner } from "../../components/ui";
import { formatBytes, timeAgo } from "../../lib/format";
import { CpuUsage } from "./CpuUsage";

const SHARED_SHOWN = 8;

/** One ruled row: what at the left, the figures at the right. */
function Row({ name, disk, memory, muted }: { name: string; disk?: string; memory?: string; muted?: boolean }) {
  return (
    <div className={clsx("grid grid-cols-[minmax(0,1fr)_auto_auto] gap-x-3 border-b border-line py-1.5", muted && "text-fg-2")}>
      <span className="truncate">{name}</span>
      <span className="text-right whitespace-nowrap">{disk ?? ""}</span>
      <span className="w-20 text-right whitespace-nowrap text-fg-2">{memory ?? ""}</span>
    </div>
  );
}

function Meter({ used, total, low }: { used: number; total: number; low: boolean }) {
  return (
    <div className="mt-1 h-1 w-full bg-line" aria-hidden>
      <div className={clsx("h-full", low ? "bg-you" : "bg-fg-2")} style={{ width: `${total ? Math.min(100, (used / total) * 100) : 0}%` }} />
    </div>
  );
}

/** Hours as "40 min" or "2 h 10 min". */
function hours(h: number): string {
  const minutes = Math.round(h * 60);
  return minutes < 90 ? `${minutes} min` : `${Math.floor(minutes / 60)} h${minutes % 60 ? ` ${minutes % 60} min` : ""}`;
}

/** The target, the trend and its forecast, and the last notices to managers. */
function StorageTarget({ storage }: { storage: UsageStorage }) {
  const { t } = useTranslation();
  const over = storage.percent != null && storage.percent >= storage.notify_percent;
  const growing = storage.rate != null && storage.rate > 0;
  const etas = growing
    ? (
        [
          [`${storage.notify_percent} %`, storage.hours_target],
          ["95 %", storage.hours_95],
          [t("cleanup.full"), storage.hours_full],
        ] as const
      )
        .filter(([, h]) => h != null && h > 0)
        .map(([what, h]) => t("cleanup.eta", { what, time: hours(h as number) }))
    : [];
  const trend = storage.rate == null ? t("cleanup.trendUnknown") : growing ? t("cleanup.growing", { rate: formatBytes(storage.rate) }) : t("cleanup.notGrowing");
  return (
    <section>
      <h3 className="caps rule-b pb-1.5">{t("cleanup.target", { percent: storage.notify_percent })}</h3>
      <p className={clsx("py-1.5", over ? "text-you-fg" : "text-fg-2")}>
        {[storage.percent != null && t("cleanup.now", { percent: Math.round(storage.percent) }), trend, ...etas].filter(Boolean).join(" · ")}
      </p>
      {storage.notices.length > 0 && <h4 className="border-b border-line pt-2 pb-1.5 font-extrabold">{t("cleanup.notices")}</h4>}
      {storage.notices.map((n) => (
        <Row key={n.at} name={t(`cleanup.notice.${n.reason}`, { when: timeAgo(n.at), percent: Math.round(n.percent), projects: n.projects.join(", ") })} muted />
      ))}
    </section>
  );
}

/** One cleanup: when, by whom, how much, the biggest items, what it left alone. */
function Cleanup({ cleanup }: { cleanup: UsageCleanup }) {
  const { t } = useTranslation();
  const memory = cleanup.level === "memory";
  return (
    <div className="pt-2">
      <p className="py-1.5 text-fg-2">
        {memory
          ? t("cleanup.stoppedSummary", { when: timeAgo(cleanup.at), count: cleanup.items.length })
          : t(`cleanup.summary.${cleanup.level}`, { when: timeAgo(cleanup.at), freed: formatBytes(cleanup.freed), free: formatBytes(cleanup.free_before), by: cleanup.by, project: cleanup.project })}
      </p>
      {cleanup.items.map((item) => (
        <Row key={item.path} name={`${t(`cleanup.what.${item.what}`)} · ${item.path}`} disk={memory ? undefined : formatBytes(item.bytes)} muted />
      ))}
      {cleanup.kept.map((reason) => (
        <Row key={reason} name={reason} muted />
      ))}
    </div>
  );
}

/** One runner's machine: volumes and memory, CPU, projects with their agents, and what the agents share. */
function Machine({ data, appData }: { data: MachineUsage; appData?: number | null }) {
  const { t } = useTranslation();
  const disk = (n: number | null | undefined) => (n == null ? (data.measured_at ? "–" : "") : formatBytes(n));
  const memory = (n: number) => (n ? formatBytes(n) : "");
  const shared = data.shared.slice(0, SHARED_SHOWN);
  const sharedRest = data.shared.slice(SHARED_SHOWN).reduce((sum, f) => sum + f.bytes, 0);
  const sharedTotal = data.shared.reduce((sum, f) => sum + f.bytes, 0);

  return (
    <div className="space-y-6">
      <section>
        <h3 className="caps rule-b pb-1.5">{t("usage.volumes")}</h3>
        {data.volumes.map((v) => (
          <div key={v.kinds.join()} className="border-b border-line py-2">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="font-semibold">{v.kinds.map((k) => t(`usage.kind.${k}`)).join(" · ")}</span>
              <span className={clsx("whitespace-nowrap", v.low ? "text-you-fg" : "text-fg-2")}>
                {t("usage.volumeFigures", { used: formatBytes(v.used), total: formatBytes(v.total), free: formatBytes(v.free) })}
              </span>
            </div>
            <Meter used={v.used} total={v.total} low={v.low} />
            {v.low && <p className="mt-1 text-you-fg">{t("usage.low")}</p>}
          </div>
        ))}
        {data.memory_total > 0 && (
          <div className="border-b border-line py-2">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="font-semibold">{t("usage.memory")}</span>
              <span className="whitespace-nowrap text-fg-2">{t("usage.memoryFigures", { used: formatBytes(data.memory_used), total: formatBytes(data.memory_total) })}</span>
            </div>
            <Meter used={data.memory_used} total={data.memory_total} low={false} />
          </div>
        )}
        {appData != null && <Row name={t("usage.data")} disk={formatBytes(appData)} muted />}
      </section>

      <CpuUsage data={data} />

      <section>
        <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-x-3 rule-b pb-1.5">
          <h3 className="caps">{t("usage.projects")}</h3>
          <span className="caps text-right">{t("usage.disk")}</span>
          <span className="caps w-20 text-right">{t("usage.ram")}</span>
        </div>
        {data.projects.map((p) => (
          <div key={p.id} className="pt-3">
            <h4 className="border-b border-line pb-1.5 font-extrabold">{p.name}</h4>
            {p.agents.map((a) => (
              <Row key={a.id} name={a.name} disk={disk(a.disk)} memory={memory(a.memory)} />
            ))}
            <Row name={t("usage.uploads")} disk={formatBytes(p.uploads)} muted />
            {(p.other_disk > 0 || p.other_memory > 0) && <Row name={t("usage.projectOther")} disk={disk(p.other_disk)} memory={memory(p.other_memory)} muted />}
          </div>
        ))}
        <p className="mt-2 text-fg-3">{t("usage.projectsHint")}</p>
      </section>

      <section>
        <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-x-3 rule-b pb-1.5">
          <h3 className="caps">{t("usage.shared")}</h3>
          <span className="text-right font-semibold">{data.measured_at ? formatBytes(sharedTotal) : ""}</span>
          <span className="w-20" />
        </div>
        {shared.map((f) => (
          <Row key={f.name} name={f.name} disk={formatBytes(f.bytes)} />
        ))}
        {sharedRest > 0 && <Row name={t("usage.sharedRest", { count: data.shared.length - SHARED_SHOWN })} disk={formatBytes(sharedRest)} muted />}
        {data.other_disk > 0 && <Row name={t("usage.otherFolders")} disk={formatBytes(data.other_disk)} muted />}
        {data.memory_other > 0 && <Row name={t("usage.otherMemory")} memory={formatBytes(data.memory_other)} muted />}
        <p className="mt-2 text-fg-3">{t("usage.sharedHint")}</p>
      </section>
    </div>
  );
}

/** An external runner: its machine, or while it is away the last known numbers. */
function RunnerMachine({ runner }: { runner: RunnerUsage }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-6">
      <h3 className="caps rule-b pb-1.5 text-fg">{runner.name}</h3>
      {!runner.connected && (
        <p className="text-you-fg">
          {runner.last_seen_at ? t("usage.runnerAway", { when: timeAgo(runner.last_seen_at) }) : t("usage.runnerNeverConnected")}
          {" "}
          {runner.known_at ? t("usage.runnerLastKnown", { when: timeAgo(runner.known_at) }) : t("usage.runnerNothingKnown")}
        </p>
      )}
      {(runner.connected || runner.known_at) && <Machine data={runner} />}
    </div>
  );
}

/** Disk and memory of the installation: per volume, per project and agent, and what all agents share. */
export function UsageEditor() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const usage = useUsage();
  const [error, setError] = useState<unknown>(null);
  const data = usage.data;
  if (!data) return usage.error ? <ErrorText error={usage.error} /> : <Spinner />;

  const measure = async () => {
    setError(null);
    try {
      qc.setQueryData(qk.usage, await api.post<Usage>("/usage/measure"));
    } catch (err) {
      setError(err);
    }
  };
  const external = data.runners.length > 0;
  const connected = [data, ...data.runners].some((m) => m.runner_ok);

  return (
    <div className="space-y-6">
      {!data.runner_ok && <p className="text-you-fg">{t("usage.runnerDown")}</p>}
      {external && <h3 className="caps rule-b pb-1.5 text-fg">{t("runners.builtin")}</h3>}
      <Machine data={data} appData={data.data} />
      {data.runners.map((r) => (
        <RunnerMachine key={r.id} runner={r} />
      ))}

      {data.storage && <StorageTarget storage={data.storage} />}
      {data.cleanups.length > 0 && (
        <section>
          <h3 className="caps rule-b pb-1.5">{t("cleanup.cleanups")}</h3>
          {data.cleanups.map((c) => (
            <Cleanup key={c.at} cleanup={c} />
          ))}
        </section>
      )}
      {data.servers_stopped && (
        <section>
          <h3 className="caps rule-b pb-1.5">{t("cleanup.lastServers")}</h3>
          <Cleanup cleanup={data.servers_stopped} />
        </section>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Button size="sm" onClick={measure} disabled={data.measuring || !connected}>
          {t("usage.measure")}
        </Button>
        <span className="text-fg-3">
          {data.measuring ? t("usage.measuring") : data.measured_at ? t("usage.measuredAt", { when: timeAgo(data.measured_at) }) : t("usage.notMeasured")}
        </span>
        <ErrorText error={error} />
      </div>
    </div>
  );
}
