import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useUsage } from "../../api/queries";
import type { Usage } from "../../api/types";
import { Button, ErrorText, Spinner } from "../../components/ui";
import { formatBytes, timeAgo } from "../../lib/format";

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
  const disk = (n: number | null | undefined) => (n == null ? (data.measured_at ? "–" : "") : formatBytes(n));
  const memory = (n: number) => (n ? formatBytes(n) : "");
  const shared = data.shared.slice(0, SHARED_SHOWN);
  const sharedRest = data.shared.slice(SHARED_SHOWN).reduce((sum, f) => sum + f.bytes, 0);
  const sharedTotal = data.shared.reduce((sum, f) => sum + f.bytes, 0);

  return (
    <div className="space-y-6">
      {!data.runner_ok && <p className="text-you-fg">{t("usage.runnerDown")}</p>}

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
        {data.data != null && <Row name={t("usage.data")} disk={formatBytes(data.data)} muted />}
      </section>

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

      <div className="flex flex-wrap items-center gap-3">
        <Button size="sm" onClick={measure} disabled={data.measuring || !data.runner_ok}>
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
