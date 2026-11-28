import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { putSettings, useApiMutation } from "../../api/queries";
import type { SearchModelOption, SearchModelState, SearchStatus } from "../../api/types";
import { Button, ErrorText, Modal, Toggle } from "../../components/ui";

const STATUS = ["search-status"] as const;

function duration(seconds: number | null, t: (key: string, options?: Record<string, unknown>) => string): string {
  if (seconds == null) return "";
  if (seconds < 90) return t("search.duration.minute");
  if (seconds < 5400) return t("search.duration.minutes", { count: Math.round(seconds / 60) });
  return t("search.duration.hours", { count: Math.round(seconds / 360) / 10 });
}

function ModelLine({ model }: { model: SearchModelState }) {
  const { t } = useTranslation();
  return (
    <>
      {model.label} · {t(`search.modelState.${model.state}`)}
      {model.progress != null && ` ${Math.round(model.progress * 100)} %`}
      {model.error && <span className="text-you-fg"> · {model.error}</span>}
    </>
  );
}

/** Semantic search for the whole installation: the switch, the embedding model and what the index holds.
 * Each project can switch it off for itself. */
export function SearchEditor({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [asked, setAsked] = useState<SearchModelOption | null>(null);
  const status = useQuery({
    queryKey: STATUS,
    queryFn: () => api.get<SearchStatus>("/search/status"),
    refetchInterval: (query) => {
      const s = query.state.data;
      const busy = s && (s.next || s.embedded < s.documents || s.model.state === "downloading" || s.model.state === "loading");
      return busy ? 3_000 : 60_000;
    },
  }).data;
  const choose = useApiMutation(
    (model: string) => api.post<SearchStatus>("/search/model", { model }),
    () => [STATUS],
  );
  if (!status) return null;
  if (!status.available) return <p className="text-fg-3">{t("search.unavailable")}</p>;
  const waiting = status.documents - status.embedded;
  const kinds = Object.entries(status.by_kind);
  const target = status.next?.name ?? status.model.name;
  const pick = async (model: SearchModelOption) => {
    setAsked(null);
    await choose.mutateAsync(model.name);
  };
  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <Toggle
          checked={enabled}
          onChange={async (v) => {
            await putSettings(qc, { semantic_search: v });
            void qc.invalidateQueries({ queryKey: STATUS });
          }}
          label={t("search.toggle")}
        />
        <p className="text-fg-3">{t("search.hint")}</p>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <dt className="text-fg-3">{t("search.model")}</dt>
        <dd>
          <ModelLine model={status.model} />
        </dd>
        {status.next && (
          <>
            <dt className="text-fg-3">{t("search.switching")}</dt>
            <dd>
              <ModelLine model={status.next} /> · {t("search.rebuilt", { done: status.next_embedded, total: status.documents })}
            </dd>
          </>
        )}
        <dt className="text-fg-3">{t("search.indexed")}</dt>
        <dd>
          {t("search.documents", { count: status.documents })}
          {waiting > 0 && ` · ${t("search.waiting", { count: waiting })}`}
        </dd>
        {(status.rate != null || status.eta_seconds != null) && (waiting > 0 || status.next) && (
          <>
            <dt className="text-fg-3">{t("search.progress")}</dt>
            <dd>
              {status.rate != null && t("search.rate", { rate: status.rate })}
              {status.eta_seconds != null && ` · ${t("search.eta", { duration: duration(status.eta_seconds, t) })}`}
            </dd>
          </>
        )}
        {kinds.map(([kind, k]) => (
          <Fragment key={kind}>
            <dt className="text-fg-3">{t(`search.kinds.${kind}`, { defaultValue: kind })}</dt>
            <dd>
              {k.total != null ? t("search.itemsOf", { items: k.items, total: k.total }) : t("search.items", { count: k.items })}
              {" · "}
              {t("search.embeddedOf", { done: k.embedded, total: k.chunks })}
            </dd>
          </Fragment>
        ))}
        {status.errors.length > 0 && (
          <>
            <dt className="text-fg-3">{t("search.errors")}</dt>
            <dd className="space-y-0.5 text-you-fg">
              {status.errors.map((e) => (
                <span key={e.at + e.message} className="block">
                  {new Date(e.at).toLocaleTimeString()} · {e.message}
                </span>
              ))}
            </dd>
          </>
        )}
      </dl>
      <section className="space-y-2">
        <h3 className="caps rule-b pb-1.5">{t("search.models")}</h3>
        <p className="text-fg-3">{t("search.modelsHint")}</p>
        <ul className="space-y-2">
          {status.models.map((m) => (
            <li key={m.name}>
              <button
                type="button"
                onClick={() => m.name !== target && setAsked(m)}
                className={clsx(
                  "w-full rounded-sm border px-3 py-2 text-left transition-colors",
                  m.name === target ? "border-fg" : "border-line hover:border-line-2",
                )}
              >
                <span className="font-semibold">{m.label}</span>
                {m.recommended && <span className="text-ok"> · {t("search.recommended")}</span>}
                {m.name === status.model.name && <span className="text-fg-3"> · {t("search.inUse")}</span>}
                {m.name === status.next?.name && <span className="text-fg-3"> · {t("search.building")}</span>}
                <span className="block text-fg-2">{m.description}</span>
                <span className="block text-fg-3">
                  {t("search.facts", { tier: t(`search.tier.${m.tier}`), dim: m.dim, tokens: m.max_tokens, download: m.download_mb, ram: m.ram_mb, licence: m.licence })}
                  {m.mmteb_retrieval != null && ` · ${t("search.quality", { score: m.mmteb_retrieval })}`}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <ErrorText error={choose.error} />
      </section>
      <Modal
        open={!!asked}
        onClose={() => setAsked(null)}
        title={t("search.confirmTitle", { model: asked?.label ?? "" })}
        footer={
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setAsked(null)}>
              {t("search.cancel")}
            </Button>
            <Button variant="primary" loading={choose.isPending} onClick={() => asked && pick(asked)}>
              {asked?.name === status.model.name ? t("search.callOff") : t("search.confirm")}
            </Button>
          </div>
        }
      >
        {asked && asked.name === status.model.name ? (
          <p>{t("search.confirmBack", { model: status.next?.label ?? "" })}</p>
        ) : (
          asked && (
            <p>
              {t("search.confirmText", {
                count: status.documents,
                duration: duration(asked.estimate_seconds, t),
                current: status.model.label,
                download: asked.download_mb,
              })}
            </p>
          )
        )}
      </Modal>
    </div>
  );
}
