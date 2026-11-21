import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { KeyRound, Pencil, Server, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import type { RunnerCreated, RunnerInfo } from "../../api/types";
import { Button, ErrorText, IconButton, Input, Textarea } from "../../components/ui";
import { formatBytes, timeAgo } from "../../lib/format";

const KEY = ["runners"];

/** External runners: a runner on another server connects out to PlainBoard with its token. Adding one
 *  shows the command to run there, once; the list shows whether each is connected and its machine. */
export function RunnersEditor() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const runners = useQuery({ queryKey: KEY, queryFn: () => api.get<RunnerInfo[]>("/runners"), refetchInterval: 15_000 });
  const [name, setName] = useState("");
  const [created, setCreated] = useState<RunnerCreated | null>(null);
  const [copied, setCopied] = useState("");
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
  const add = () =>
    name.trim() &&
    change(async () => {
      setCreated(await api.post<RunnerCreated>("/runners", { name: name.trim() }));
      setName("");
    });
  const rename = (r: RunnerInfo) => {
    const next = prompt(t("runners.renamePrompt"), r.name)?.trim();
    if (next && next !== r.name) change(async () => void (await api.patch(`/runners/${r.id}`, { name: next })));
  };
  const newToken = (r: RunnerInfo) =>
    confirm(t("runners.tokenConfirm", { name: r.name })) && change(async () => setCreated(await api.post<RunnerCreated>(`/runners/${r.id}/token`)));
  const remove = (r: RunnerInfo) =>
    confirm(t("runners.removeConfirm", { name: r.name })) &&
    change(async () => {
      await api.del(`/runners/${r.id}`);
      if (created?.runner.id === r.id) setCreated(null);
    });
  const copy = async (what: string, text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(what);
  };

  return (
    <div className="space-y-4">
      <p className="text-fg-2">{t("runners.intro")}</p>
      {(runners.data?.length ?? 0) > 0 && (
        <ul className="divide-y divide-line rounded-sm ring-1 ring-line">
          {runners.data!.map((r) => (
            <li key={r.id} className="flex items-start gap-2 px-3 py-2">
              <Server className={clsx("mt-0.5 size-3.5 shrink-0", r.connected ? "text-ok" : "text-fg-3")} />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{r.name}</div>
                <div className="text-fg-3">
                  {r.connected ? t("runners.connected") : r.last_seen_at ? t("runners.lastSeen", { when: timeAgo(r.last_seen_at) }) : t("runners.never")}
                  {r.version && ` · ${t("runners.version", { commit: r.version })}`}
                  {r.disk_total > 0 && ` · ${t("runners.disk", { used: formatBytes(r.disk_used), total: formatBytes(r.disk_total) })}`}
                  {r.memory_total > 0 && ` · ${t("runners.memory", { used: formatBytes(r.memory_used), total: formatBytes(r.memory_total) })}`}
                </div>
                {!r.version_ok && <div className="text-you-fg">{t("runners.versionMismatch")}</div>}
                <div className="truncate text-fg-3">{r.projects.length ? t("runners.projects", { names: r.projects.join(", ") }) : t("runners.noProjects")}</div>
              </div>
              <IconButton label={t("runners.rename")} onClick={() => rename(r)}>
                <Pencil className="size-3.5" />
              </IconButton>
              <IconButton label={t("runners.newToken")} onClick={() => newToken(r)}>
                <KeyRound className="size-3.5" />
              </IconButton>
              <IconButton label={t("runners.remove")} onClick={() => remove(r)}>
                <Trash2 className="size-3.5" />
              </IconButton>
            </li>
          ))}
        </ul>
      )}
      {created && (
        <div className="space-y-2 rounded-sm bg-you-bg p-3">
          <p className="text-you-fg">{t("runners.createdHint", { name: created.runner.name })}</p>
          <Textarea readOnly rows={5} value={created.command} onFocus={(e) => e.target.select()} className="font-mono text-xs" />
          <Button size="md" onClick={() => copy("command", created.command)}>
            {copied === "command" ? t("projects.repoSshCopied") : t("runners.copyCommand")}
          </Button>
          <p className="pt-2 text-fg-2">{t("runners.composeHint")}</p>
          <Textarea readOnly rows={12} value={created.compose} onFocus={(e) => e.target.select()} className="font-mono text-xs" />
          <Button size="md" onClick={() => copy("compose", created.compose)}>
            {copied === "compose" ? t("projects.repoSshCopied") : t("runners.copyCompose")}
          </Button>
        </div>
      )}
      <div className="flex gap-2">
        <Input
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
        <Button size="md" className="shrink-0" onClick={add} disabled={!name.trim()}>
          {t("runners.add")}
        </Button>
      </div>
      <ErrorText error={error} />
    </div>
  );
}
