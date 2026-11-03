import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useSecrets } from "../../api/queries";
import type { Secret } from "../../api/types";
import { Button, ErrorText, IconButton, Input } from "../../components/ui";
import { timeAgo } from "../../lib/format";

const toName = (value: string) => value.toUpperCase().replace(/[^A-Z0-9_]/g, "_");

/** Environment variables for agents. Saved right away; values are write-only. projectId null = all projects. */
export function SecretsEditor({ projectId }: { projectId: number | null }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const secrets = useSecrets(projectId);
  const [draft, setDraft] = useState({ name: "", value: "", description: "" });
  const [error, setError] = useState<unknown>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: qk.secrets(projectId) });
  const run = async (action: () => Promise<unknown>) => {
    setError(null);
    try {
      await action();
      await refresh();
      return true;
    } catch (err) {
      setError(err);
      return false;
    }
  };

  const add = async () => {
    const url = projectId === null ? "/secrets" : `/projects/${projectId}/secrets`;
    if (await run(() => api.post(url, draft))) setDraft({ name: "", value: "", description: "" });
  };

  return (
    <div className="space-y-2">
      {(secrets.data ?? []).map((secret) => (
        <SecretRow key={secret.id} secret={secret} onChange={run} />
      ))}
      <div className="grid gap-2 rounded-lg p-2 ring-1 ring-zinc-200 sm:grid-cols-[1fr_1fr] dark:ring-zinc-800">
        <Input placeholder="NPM_TOKEN" className="font-mono" value={draft.name} onChange={(e) => setDraft({ ...draft, name: toName(e.target.value) })} />
        <Input type="password" autoComplete="off" placeholder={t("secrets.value")} value={draft.value} onChange={(e) => setDraft({ ...draft, value: e.target.value })} />
        <Input className="sm:col-span-2" placeholder={t("secrets.description")} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
        <div className="sm:col-span-2">
          <Button size="sm" icon={<Plus className="size-3.5" />} onClick={add} disabled={!draft.name || !draft.value}>
            {t("secrets.add")}
          </Button>
        </div>
      </div>
      <ErrorText error={error} />
    </div>
  );
}

function SecretRow({ secret, onChange }: { secret: Secret; onChange: (action: () => Promise<unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const [value, setValue] = useState<string | null>(null);

  const remove = () => confirm(t("secrets.confirmDelete", { name: secret.name })) && onChange(() => api.del(`/secrets/${secret.id}`));
  const replace = async () => {
    if (value && (await onChange(() => api.patch(`/secrets/${secret.id}`, { value })))) setValue(null);
  };

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg px-2 py-1.5 ring-1 ring-zinc-200 dark:ring-zinc-800">
      <KeyRound className="size-3.5 shrink-0 text-zinc-400" />
      <div className="min-w-0 flex-1">
        <div className="truncate font-mono text-xs font-medium">{secret.name}</div>
        <div className="truncate text-xs text-zinc-500">
          {secret.description ? `${secret.description} · ` : ""}
          {t("secrets.updated", { when: timeAgo(secret.updated_at) })}
        </div>
      </div>
      {value === null ? (
        <Button size="sm" variant="ghost" onClick={() => setValue("")}>
          {t("secrets.replace")}
        </Button>
      ) : (
        <div className="flex w-full gap-2 sm:w-auto">
          <Input autoFocus type="password" autoComplete="off" placeholder={t("secrets.newValue")} value={value} onChange={(e) => setValue(e.target.value)} />
          <Button size="sm" variant="primary" onClick={replace} disabled={!value}>
            {t("common.save")}
          </Button>
          <Button size="sm" onClick={() => setValue(null)}>
            {t("common.cancel")}
          </Button>
        </div>
      )}
      <IconButton label={t("secrets.delete")} onClick={remove}>
        <Trash2 className="size-3.5" />
      </IconButton>
    </div>
  );
}
