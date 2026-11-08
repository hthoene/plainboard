import clsx from "clsx";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLimits } from "../../api/queries";
import type { AccountLimits, LimitForecast, LimitWindow } from "../../api/types";
import { timeAgo } from "../../lib/format";

const tone = (used: number) => (used >= 0.9 ? "red" : used >= 0.75 ? "amber" : "indigo");
const stroke = { red: "text-red-500", amber: "text-amber-500", indigo: "text-indigo-500" };
const bar = { red: "bg-red-500", amber: "bg-amber-500", indigo: "bg-indigo-500" };
const percent = (used: number) => Math.round(used * 100);
const fromUnix = (seconds: number) => new Date(seconds * 1000).toISOString();

const PROVIDERS = [
  { key: "claude_code", labelKey: "settings.claude" },
  { key: "codex", labelKey: "settings.codex" },
];
const fullest = (account: AccountLimits) => Math.max(0, ...account.windows.map((w) => w.utilization));

/** Plan limits (5-hour, weekly) as one ring per provider over all its accounts; click for details. */
export function LimitsRing() {
  const { t } = useTranslation();
  const { data } = useLimits();
  if (!data) return null;
  const rings = PROVIDERS.map((p) => ({ ...p, accounts: data.accounts.filter((a) => a.provider === p.key) })).filter((p) => p.accounts.some((a) => a.windows.length));
  if (!rings.length) return <Ring accounts={data.accounts} />;
  return (
    <>
      {rings.map((ring) => (
        <Ring key={ring.key} label={t(ring.labelKey)} accounts={ring.accounts} />
      ))}
    </>
  );
}

function Ring({ label: provider, accounts }: { label?: string; accounts: AccountLimits[] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const known = accounts.filter((a) => a.windows.length);
  // Agents move to the account with the most room, so the ring shows how full the accounts are together.
  const used = known.length ? known.reduce((sum, a) => sum + Math.min(fullest(a), 1), 0) / known.length : 0;
  const slowDown = known.length > 0 && known.every((a) => a.forecasts.some((f) => f.status === "over"));
  const ringTone = tone(used) === "indigo" && slowDown ? "amber" : tone(used);
  const ringLabel = t("chat.limits.ring", { percent: percent(used) });
  const label = (provider ? `${provider}: ${ringLabel}` : ringLabel) + (slowDown ? ` · ${t("chat.limits.slowDown")}` : "");
  const several = accounts.length > 1;

  return (
    <div ref={ref} className="relative shrink-0">
      <button onClick={() => setOpen(!open)} aria-label={label} aria-expanded={open} title={label} className="flex h-6 items-center gap-1 rounded-full px-1 hover:bg-zinc-100 dark:hover:bg-zinc-800">
        <svg viewBox="0 0 20 20" className="size-4 -rotate-90" aria-hidden>
          <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeWidth="3" className="text-zinc-200 dark:text-zinc-700" />
          <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" pathLength={100} strokeDasharray={`${Math.min(percent(used), 100)} 100`} className={stroke[ringTone]} />
        </svg>
        {provider && <span className="text-[11px] text-zinc-500">{provider}</span>}
      </button>
      {open && (
        <div className="absolute right-0 bottom-full z-20 mb-2 max-h-[70vh] w-64 space-y-3 overflow-y-auto rounded-xl border border-zinc-200 bg-white p-3 text-xs shadow-lg dark:border-zinc-800 dark:bg-zinc-900">
          {provider && <p className="font-semibold">{provider}</p>}
          {!known.length && <p className="text-zinc-500">{t(accounts.some((a) => a.available) ? "chat.limits.unknown" : "chat.limits.noPlan")}</p>}
          {known.length > 0 &&
            accounts.map((account) => (
              <div key={account.account_id} className={clsx("space-y-3", several && "border-t border-zinc-100 pt-2 first:border-0 first:pt-0 dark:border-zinc-800")}>
                {several && <p className="font-medium">{account.label}</p>}
                {account.windows.map((window) => (
                  <LimitRow key={window.name} window={window} forecast={account.forecasts.find((f) => f.name === window.name)} />
                ))}
                {!account.windows.length && <p className="text-zinc-500">{t(account.available ? "chat.limits.unknownShort" : "chat.limits.apiKey")}</p>}
                {account.updated_at && <p className="text-zinc-500">{t("chat.limits.updated", { when: timeAgo(fromUnix(account.updated_at)) })}</p>}
              </div>
            ))}
        </div>
      )}
    </div>
  );
}

const forecastTone = {
  ok: "text-emerald-700 dark:text-emerald-400",
  tight: "text-amber-700 dark:text-amber-400",
  over: "text-red-700 dark:text-red-400",
};

function Forecast({ forecast }: { forecast: LimitForecast }) {
  const { t } = useTranslation();
  const headline =
    forecast.status === "over"
      ? forecast.runs_out_at
        ? t("chat.limits.forecast.over", { percent: percent(forecast.projected), when: timeAgo(fromUnix(forecast.runs_out_at)) })
        : t("chat.limits.forecast.reached")
      : t(`chat.limits.forecast.${forecast.status}`, { percent: percent(forecast.projected) });
  return (
    <div className="mt-1.5 space-y-0.5">
      <p className={clsx("font-medium", forecastTone[forecast.status])}>{headline}</p>
      <p className="text-zinc-500">{t("chat.limits.forecast.pace", { daily: percent(forecast.daily), budget: percent(forecast.budget) })}</p>
      <p className="text-zinc-500">{forecast.basis_days ? t("chat.limits.forecast.basisDays", { count: forecast.basis_days }) : t("chat.limits.forecast.basisWeek")}</p>
    </div>
  );
}

function LimitRow({ window, forecast }: { window: LimitWindow; forecast?: LimitForecast }) {
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
      {forecast && <Forecast forecast={forecast} />}
    </div>
  );
}
