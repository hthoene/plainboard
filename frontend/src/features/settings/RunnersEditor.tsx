import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Check, ChevronRight, KeyRound, Pencil, Plus, Server, Trash2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import type { RunnerCreated, RunnerInfo } from "../../api/types";
import { Button, ErrorText, IconButton, Input, Spinner } from "../../components/ui";
import { CodeBlock } from "../../components/Markdown";
import { formatBytes, timeAgo } from "../../lib/format";

const KEY = ["runners"];
const UPDATE = "docker compose pull && docker compose up -d";

function Code({ text }: { text: string }) {
  return (
    <div className="prose-chat">
      <CodeBlock alwaysCopy text={text}>
        <code>{text}</code>
      </CodeBlock>
    </div>
  );
}

/** One numbered step of the guide; `dim` while an earlier step is still to do. */
function Step({ n, title, dim, done, children }: { n: number; title: string; dim?: boolean; done?: boolean; children?: ReactNode }) {
  return (
    <li className={clsx("flex gap-3", dim && "text-fg-3")}>
      <span
        className={clsx(
          "mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-sm font-semibold ring-1 ring-inset",
          done ? "text-ok ring-ok" : dim ? "ring-line-2" : "bg-fg text-bg ring-fg",
        )}
      >
        {done ? <Check className="size-3.5" /> : n}
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        <div className={clsx("font-medium", !dim && "text-fg")}>{title}</div>
        {children}
      </div>
    </li>
  );
}

/** External runners: a runner on another server connects out to PlainBoard with its key. The guide
 *  walks through adding one; the command is shown once and the page waits until the runner is connected. */
export function RunnersEditor() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [created, setCreated] = useState<RunnerCreated | null>(null);
  const runners = useQuery({
    queryKey: KEY,
    queryFn: () => api.get<RunnerInfo[]>("/runners"),
    // Quick while the page waits for the new runner to connect.
    refetchInterval: (query) => (created && !query.state.data?.find((r) => r.id === created.runner.id)?.connected ? 2_000 : 15_000),
  });
  const [name, setName] = useState("");
  const [run, setRun] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const change = async (action: () => Promise<void>) => {
    setError(null);
    try {
      await action();
      await qc.invalidateQueries({ queryKey: KEY });
    } catch (err) {
      setError(err);
    }
  };
  const show = (c: RunnerCreated) => {
    setCreated(c);
    setRun(false);
  };
  const add = () =>
    name.trim() &&
    change(async () => {
      show(await api.post<RunnerCreated>("/runners", { name: name.trim() }));
      setName("");
    });
  const rename = (r: RunnerInfo) => {
    const next = prompt(t("runners.renamePrompt"), r.name)?.trim();
    if (next && next !== r.name) change(async () => void (await api.patch(`/runners/${r.id}`, { name: next })));
  };
  const newKey = (r: RunnerInfo) =>
    confirm(t("runners.keyConfirm", { name: r.name })) && change(async () => show(await api.post<RunnerCreated>(`/runners/${r.id}/token`)));
  const remove = (r: RunnerInfo) =>
    confirm(t("runners.removeConfirm", { name: r.name })) &&
    change(async () => {
      await api.del(`/runners/${r.id}`);
      if (created?.runner.id === r.id) setCreated(null);
    });

  const list = runners.data ?? [];
  const live = created ? list.find((r) => r.id === created.runner.id) : undefined;
  const connected = !!live?.connected;

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <p className="text-fg-2">{t("runners.intro")}</p>
        <p className="text-fg-3">{t("runners.requirements")}</p>
      </div>

      {list.length > 0 && (
        <ul className="divide-y divide-line rounded-sm ring-1 ring-line">
          {list.map((r) => (
            <li key={r.id} className="flex items-start gap-2 px-3 py-2">
              <Server className={clsx("mt-0.5 size-4 shrink-0", r.connected ? "text-ok" : "text-fg-3")} />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{r.name}</div>
                <div className={r.connected ? "text-ok" : "text-fg-3"}>
                  {r.connected ? t("runners.connected") : r.last_seen_at ? t("runners.lastSeen", { when: timeAgo(r.last_seen_at) }) : t("runners.never")}
                </div>
                {r.connected ? (
                  <div className="text-fg-3">
                    {[
                      r.version && t("runners.version", { commit: r.version }),
                      r.disk_total > 0 && t("runners.disk", { used: formatBytes(r.disk_used), total: formatBytes(r.disk_total) }),
                      r.memory_total > 0 && t("runners.memory", { used: formatBytes(r.memory_used), total: formatBytes(r.memory_total) }),
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                ) : (
                  r.id !== created?.runner.id && (
                    <div className="text-fg-3">
                      {t("runners.offlineHint")} <code>docker compose logs</code>
                    </div>
                  )
                )}
                {!r.version_ok && <div className="text-you-fg">{t("runners.versionMismatch")}</div>}
                <div className="text-fg-3 [overflow-wrap:anywhere]">{r.projects.length ? t("runners.projects", { names: r.projects.join(", ") }) : t("runners.noProjects")}</div>
              </div>
              <IconButton label={t("runners.rename")} onClick={() => rename(r)}>
                <Pencil className="size-4" />
              </IconButton>
              <IconButton label={t("runners.newKey")} onClick={() => newKey(r)}>
                <KeyRound className="size-4" />
              </IconButton>
              <IconButton label={t("runners.remove")} onClick={() => remove(r)}>
                <Trash2 className="size-4" />
              </IconButton>
            </li>
          ))}
        </ul>
      )}

      <section className="space-y-3">
        <h3 className="caps border-b-2 border-fg pb-1">{t("runners.guideTitle")}</h3>
        {created ? (
          <p className="text-fg-2">{t("runners.step1Done", { name: created.runner.name })}</p>
        ) : (
          <div className="space-y-1">
            <label className="text-fg-2" htmlFor="runner-name">
              {t("runners.nameLabel")}
            </label>
            <div className="flex gap-2">
              <Input
                id="runner-name"
                value={name}
                maxLength={100}
                placeholder={t("runners.namePlaceholder")}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    add();
                  }
                }}
              />
              <Button size="md" variant="primary" className="shrink-0" onClick={add} disabled={!name.trim()}>
                <Plus className="size-4" />
                {t("runners.add")}
              </Button>
            </div>
          </div>
        )}
        <ol className="space-y-4">
          {(run
            ? [
                <Step key="run" n={1} title={t("runners.stepRun")} dim={!created} done={connected}>
                  {created && (
                    <div className="space-y-2">
                      <Code text={created.command} />
                      <p className="text-you-fg">{t("runners.onceNote")}</p>
                    </div>
                  )}
                </Step>,
              ]
            : [
                <Step key="file" n={1} title={t("runners.stepFile")} dim={!created} done={connected}>
                  {created && (
                    <div className="space-y-2">
                      <Code text={created.compose} />
                      <p className="text-you-fg">{t("runners.onceNote")}</p>
                    </div>
                  )}
                </Step>,
                <Step key="up" n={2} title={t("runners.stepUp")} dim={!created} done={connected}>
                  {created && <Code text="docker compose up -d" />}
                </Step>,
              ]
          ).concat(
            <Step key="wait" n={run ? 2 : 3} title={t("runners.stepWait")} dim={!created} done={connected}>
              {created &&
                (connected ? (
                  <p className="flex items-center gap-1.5 text-ok">
                    <Check className="size-4" />
                    {t("runners.nowConnected", { name: created.runner.name })}
                  </p>
                ) : (
                  <div className="space-y-1">
                    <p className="flex items-center gap-2 text-fg-2" role="status">
                      <Spinner />
                      {t("runners.waiting")}
                    </p>
                    <p className="text-fg-3">
                      {t("runners.offlineHint")} <code>docker compose logs</code>
                    </p>
                  </div>
                ))}
            </Step>,
            <Step key="choose" n={run ? 3 : 4} title={t("runners.stepChoose")} dim={!connected}>
              {connected && (
                <div className="space-y-2">
                  <p className="text-fg-2">{t("runners.stepChooseHint")}</p>
                  <p className="text-fg-3">{t("runners.updateHint")}</p>
                  <Code text={UPDATE} />
                  <Button size="md" onClick={() => setCreated(null)}>
                    {t("runners.addAnother")}
                  </Button>
                </div>
              )}
            </Step>,
          )}
        </ol>
        {created && (
          <button type="button" onClick={() => setRun(!run)} className="inline-flex items-center gap-1 text-fg-2 underline-offset-2 hover:text-fg hover:underline">
            <ChevronRight className={clsx("size-4", run && "rotate-90")} />
            {run ? t("runners.useCompose") : t("runners.useRun")}
          </button>
        )}
      </section>
      <ErrorText error={error} />
    </div>
  );
}
