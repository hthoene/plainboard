import clsx from "clsx";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLimits } from "../../api/queries";
import type { LimitWindow } from "../../api/types";
import { timeAgo } from "../../lib/format";

const tone = (used: number) => (used >= 0.9 ? "red" : used >= 0.75 ? "amber" : "indigo");
const stroke = { red: "text-red-500", amber: "text-amber-500", indigo: "text-indigo-500" };
const bar = { red: "bg-red-500", amber: "bg-amber-500", indigo: "bg-indigo-500" };
const percent = (used: number) => Math.round(used * 100);
const fromUnix = (seconds: number) => new Date(seconds * 1000).toISOString();

/** Plan limits (5-hour, weekly) as a ring; click for details. */
export function LimitsRing() {
  const { t } = useTranslation();
  const { data } = useLimits();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  if (!data) return null;
  const highest = Math.max(0, ...data.windows.map((w) => w.utilization));
  const label = t("chat.limits.ring", { percent: percent(highest) });

  return (
    <div ref={ref} className="relative shrink-0">
      <button onClick={() => setOpen(!open)} aria-label={label} aria-expanded={open} title={label} className="flex size-6 items-center justify-center rounded-full hover:bg-zinc-100 dark:hover:bg-zinc-800">
        <svg viewBox="0 0 20 20" className="size-4 -rotate-90" aria-hidden>
          <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeWidth="3" className="text-zinc-200 dark:text-zinc-700" />
          <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" pathLength={100} strokeDasharray={`${Math.min(percent(highest), 100)} 100`} className={stroke[tone(highest)]} />
        </svg>
      </button>
      {open && (
        <div className="absolute right-0 bottom-full z-20 mb-2 w-64 space-y-3 rounded-xl border border-zinc-200 bg-white p-3 text-xs shadow-lg dark:border-zinc-800 dark:bg-zinc-900">
          {data.windows.map((window) => (
            <LimitRow key={window.name} window={window} />
          ))}
          {!data.windows.length && <p className="text-zinc-500">{t(data.available ? "chat.limits.unknown" : "chat.limits.noPlan")}</p>}
          {data.windows.length > 0 && data.updated_at && <p className="text-zinc-500">{t("chat.limits.updated", { when: timeAgo(fromUnix(data.updated_at)) })}</p>}
        </div>
      )}
    </div>
  );
}

function LimitRow({ window }: { window: LimitWindow }) {
  const { t } = useTranslation();
  return (
    <div>
      <div className="flex justify-between gap-2 font-medium">
        <span>{t(`chat.limits.${window.name}`, { defaultValue: window.name })}</span>
        <span>{percent(window.utilization)}%</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
        <div className={clsx("h-full rounded-full", bar[tone(window.utilization)])} style={{ width: `${Math.min(percent(window.utilization), 100)}%` }} />
      </div>
      {window.resets_at && <p className="mt-1 text-zinc-500">{t("chat.limits.resets", { when: timeAgo(fromUnix(window.resets_at)) })}</p>}
    </div>
  );
}
