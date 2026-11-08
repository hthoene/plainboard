import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { checks, qk, useAccounts, useMeta } from "../../api/queries";
import type { Account, CheckResult } from "../../api/types";
import { Button, ErrorText, IconButton, Input, Toggle } from "../../components/ui";
import { smallModel } from "../../lib/models";
import { CheckBadge } from "./SettingsDialog";
import { CodexSignIn } from "./CodexSignIn";

const MODES: Record<string, string[]> = {
  claude_code: ["api_key", "oauth_token", "runner_login"],
  codex: ["chatgpt", "api_key"],
};
// i18n group of the sign-in kinds per provider
const MODE_TEXTS: Record<string, string> = { claude_code: "settings.auth", codex: "settings.codexAuth" };
const SECRET_LABELS: Record<string, string> = {
  "claude_code:oauth_token": "settings.oauthToken",
  "claude_code:api_key": "settings.apiKey",
  "codex:api_key": "settings.openaiApiKey",
};

/** The accounts of one provider. Saved right away; secrets are write-only. */
export function AccountsEditor({ provider }: { provider: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const accounts = useAccounts();
  const [error, setError] = useState<unknown>(null);
  const mine = (accounts.data ?? []).filter((a) => a.provider === provider);

  const run = async (action: () => Promise<unknown>) => {
    setError(null);
    try {
      await action();
      await Promise.all([qc.invalidateQueries({ queryKey: qk.accounts }), qc.invalidateQueries({ queryKey: qk.limits })]);
      return true;
    } catch (err) {
      setError(err);
      return false;
    }
  };

  return (
    <div className="space-y-3">
      {mine.map((account) => (
        <AccountCard key={account.id} account={account} onChange={run} />
      ))}
      {mine.length > 1 && <p className="text-xs text-zinc-500">{t("accounts.rotationHint")}</p>}
      <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => run(() => api.post("/accounts", { provider, auth_mode: MODES[provider][0] }))}>
        {t("accounts.add")}
      </Button>
      <ErrorText error={error} />
    </div>
  );
}

function AccountCard({ account, onChange }: { account: Account; onChange: (action: () => Promise<unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const meta = useMeta();
  const [label, setLabel] = useState(account.label);
  const [secret, setSecret] = useState("");
  const [check, setCheck] = useState<CheckResult | null>(null);
  useEffect(() => setLabel(account.label), [account.label]);

  const patch = (body: Record<string, unknown>) => onChange(() => api.patch(`/accounts/${account.id}`, body));
  const secretLabel = SECRET_LABELS[`${account.provider}:${account.auth_mode}`];
  const remove = () => confirm(t("accounts.confirmDelete", { name: account.label })) && onChange(() => api.del(`/accounts/${account.id}`));
  const saveSecret = async () => {
    if (secret && (await patch({ secret }))) setSecret("");
  };
  const test = async () => {
    setCheck({ ok: true, detail: t("settings.testing") });
    try {
      const model = smallModel(meta.data?.providers.find((p) => p.name === account.provider)) ?? "";
      setCheck(await checks.account(account.id, model));
    } catch (err) {
      setCheck({ ok: false, detail: errorMessage(err) });
    }
  };

  return (
    <div className={clsx("space-y-3 rounded-lg p-3 ring-1 ring-zinc-200 dark:ring-zinc-800", !account.enabled && "opacity-60")}>
      <div className="flex items-center gap-2">
        <Input
          aria-label={t("accounts.label")}
          className="min-w-0 flex-1 font-medium"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          onBlur={() => label.trim() && label !== account.label && patch({ label })}
        />
        <Toggle checked={account.enabled} onChange={(enabled) => patch({ enabled })} label={<span className="text-xs">{t("accounts.enabled")}</span>} />
        <IconButton label={t("accounts.delete")} onClick={remove}>
          <Trash2 className="size-3.5" />
        </IconButton>
      </div>
      <div className={clsx("grid gap-2", MODES[account.provider].length === 3 ? "sm:grid-cols-3" : "grid-cols-2")}>
        {MODES[account.provider].map((mode) => (
          <button
            key={mode}
            onClick={() => mode !== account.auth_mode && patch({ auth_mode: mode })}
            className={clsx(
              "rounded-lg px-3 py-2 text-left text-xs ring-1 transition-colors",
              account.auth_mode === mode ? "bg-indigo-50 ring-indigo-400 dark:bg-indigo-950/40" : "ring-zinc-200 hover:bg-zinc-50 dark:ring-zinc-800 dark:hover:bg-zinc-800",
            )}
          >
            <div className="font-medium">{t(`${MODE_TEXTS[account.provider]}.${mode}`)}</div>
            <div className="text-zinc-500">{t(`${MODE_TEXTS[account.provider]}.${mode}Hint`)}</div>
          </button>
        ))}
      </div>
      {secretLabel && (
        <div className="space-y-1">
          <span className="block text-xs font-medium text-zinc-600 dark:text-zinc-400">{t(secretLabel)}</span>
          <div className="flex gap-2">
            <Input type="password" autoComplete="off" placeholder="••••••••" value={secret} onChange={(e) => setSecret(e.target.value)} />
            <Button size="md" variant="primary" onClick={saveSecret} disabled={!secret}>
              {t("common.save")}
            </Button>
            {account.secret_set && (
              <Button size="md" variant="ghost" onClick={() => patch({ secret: "" })}>
                {t("settings.clear")}
              </Button>
            )}
          </div>
          <span className="block text-xs text-zinc-500">
            {account.secret_set ? t("settings.secretSet") : account.secret_from_env ? t("settings.secretFromEnv") : t("settings.secretUnset")}
            {account.auth_mode === "oauth_token" && (
              <>
                {" · "}
                <code>claude setup-token</code>
              </>
            )}
          </span>
        </div>
      )}
      {account.auth_mode === "runner_login" && (
        <p className="rounded-md bg-zinc-50 p-3 text-xs text-zinc-600 dark:bg-zinc-800/50 dark:text-zinc-400">
          {t("settings.runnerLoginHint")} <code className="font-mono">docker compose exec -it runner claude</code>
        </p>
      )}
      {account.provider === "codex" && account.auth_mode === "chatgpt" && <CodexSignIn accountId={account.id} />}
      {account.auth_mode !== "api_key" && (
        <p className="text-xs text-amber-700 dark:text-amber-400">{t(account.provider === "codex" ? "settings.codexOwnLoginNotice" : "settings.ownLoginNotice")}</p>
      )}
      <div className="flex items-center gap-3">
        <Button size="sm" onClick={test}>
          {t("settings.test")}
        </Button>
        <CheckBadge result={check} />
      </div>
    </div>
  );
}
