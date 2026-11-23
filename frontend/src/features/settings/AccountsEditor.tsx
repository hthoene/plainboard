import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { checks, qk, useAccounts, useDirectory, useLimits, useMe, useMeta } from "../../api/queries";
import type { Account, CheckResult } from "../../api/types";
import { Button, ErrorText, IconButton, Input, Toggle } from "../../components/ui";
import { smallModel } from "../../lib/models";
import { CheckBadge } from "./SettingsDialog";
import { AntigravitySignIn } from "./AntigravitySignIn";
import { CodexSignIn } from "./CodexSignIn";

const MODES: Record<string, string[]> = {
  claude_code: ["api_key", "oauth_token", "runner_login"],
  codex: ["chatgpt", "api_key"],
  antigravity: ["google", "api_key"],
};
// A person's own subscription takes a token or a key: a sign-in on the runner is the installation's (services/accounts.OWN_MODES).
const OWN_MODES = ["oauth_token", "api_key"];
const modesOf = (provider: string, own: boolean) => MODES[provider].filter((mode) => !own || OWN_MODES.includes(mode));
const SHARES = ["private", "people", "everyone"] as const;
// i18n group of the sign-in kinds per provider
const MODE_TEXTS: Record<string, string> = {
  claude_code: "settings.auth",
  codex: "settings.codexAuth",
  antigravity: "settings.antigravityAuth",
};
const SECRET_LABELS: Record<string, string> = {
  "claude_code:oauth_token": "settings.oauthToken",
  "claude_code:api_key": "settings.apiKey",
  "codex:api_key": "settings.openaiApiKey",
  "antigravity:api_key": "settings.geminiApiKey",
};

/** The accounts of one provider: the installation's, or with ``own`` people's own ones (a non-admin:
 *  their own, which they add here; an admin: everyone's, without adding). Saved right away; secrets are write-only. */
export function AccountsEditor({ provider, own = false }: { provider: string; own?: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const me = useMe();
  const accounts = useAccounts();
  const [error, setError] = useState<unknown>(null);
  const adds = !own || !me?.is_admin;
  const mine = (accounts.data ?? []).filter((a) => a.provider === provider && (own ? a.owner_id != null && a.can_manage : a.owner_id == null));

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
      {mine.length > 1 && <p className="text-fg-3">{t("accounts.rotationHint")}</p>}
      {adds && (
        <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => run(() => api.post("/accounts", { provider, auth_mode: modesOf(provider, own)[0] }))}>
          {t("accounts.add")}
        </Button>
      )}
      {!adds && !mine.length && <p className="text-fg-3">{t("accounts.noneOwn")}</p>}
      <ErrorText error={error} />
    </div>
  );
}

function AccountCard({ account, onChange }: { account: Account; onChange: (action: () => Promise<unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const meta = useMeta();
  const me = useMe();
  const modes = modesOf(account.provider, account.owner_id != null);
  const limits = useLimits();
  const detected = limits.data?.accounts.find((entry) => entry.account_id === account.id);
  const [label, setLabel] = useState(account.label);
  const [size, setSize] = useState(account.plan_size?.toString() ?? "");
  const [secret, setSecret] = useState("");
  const [check, setCheck] = useState<CheckResult | null>(null);
  useEffect(() => setLabel(account.label), [account.label]);
  useEffect(() => setSize(account.plan_size?.toString() ?? ""), [account.plan_size]);

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
    <div className={clsx("space-y-3 rounded-sm p-3 ring-1 ring-line", !account.enabled && "opacity-60")}>
      <div className="flex items-center gap-2">
        <Input
          aria-label={t("accounts.label")}
          className="min-w-0 flex-1 font-medium"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          onBlur={() => label.trim() && label !== account.label && patch({ label })}
        />
        <Toggle checked={account.enabled} onChange={(enabled) => patch({ enabled })} label={<span>{t("accounts.enabled")}</span>} />
        <IconButton label={t("accounts.delete")} onClick={remove}>
          <Trash2 className="size-3.5" />
        </IconButton>
      </div>
      {account.owner_id != null && account.owner_id !== me?.id && <p className="text-fg-3">{t("accounts.ownedBy", { name: account.owner_name })}</p>}
      {account.owner_id != null && <Sharing account={account} patch={patch} />}
      <div className={clsx("grid gap-2", modes.length === 3 ? "sm:grid-cols-3" : modes.length === 2 ? "grid-cols-2" : "grid-cols-1")}>
        {modes.map((mode) => (
          <button
            key={mode}
            onClick={() => mode !== account.auth_mode && patch({ auth_mode: mode })}
            className={clsx(
              "rounded-sm px-3 py-2 text-left ring-1 transition-colors",
              account.auth_mode === mode ? "bg-sunk ring-fg" : "ring-line hover:bg-sunk",
            )}
          >
            <div className="font-medium">{t(`${MODE_TEXTS[account.provider]}.${mode}`)}</div>
            <div className="text-fg-3">{t(`${MODE_TEXTS[account.provider]}.${mode}Hint`)}</div>
          </button>
        ))}
      </div>
      {secretLabel && (
        <div className="space-y-1">
          <span className="block font-medium text-fg-2">{t(secretLabel)}</span>
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
          <span className="block text-fg-3">
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
        <p className="rounded-sm bg-sunk p-3 text-fg-2">
          {t("settings.runnerLoginHint")} <code className="font-mono">docker compose exec -it runner claude</code>
        </p>
      )}
      {account.provider === "codex" && account.auth_mode === "chatgpt" && <CodexSignIn accountId={account.id} />}
      {account.provider === "antigravity" && account.auth_mode === "google" && <AntigravitySignIn accountId={account.id} />}
      {account.auth_mode !== "api_key" && (
        <label className="block space-y-1">
          <span className="block font-medium text-fg-2">{t("accounts.planSize")}</span>
          <Input
            type="number"
            min="0"
            step="any"
            className="w-32"
            placeholder={detected?.plan_size?.toString() ?? ""}
            value={size}
            onChange={(e) => setSize(e.target.value)}
            onBlur={() => Number(size || 0) !== (account.plan_size ?? 0) && patch({ plan_size: Number(size) || 0 })}
          />
          <span className="block text-fg-3">
            {detected?.plan && `${t("accounts.planDetected", { plan: detected.plan })} · `}
            {t("accounts.planSizeHint")}
          </span>
        </label>
      )}
      {account.auth_mode !== "api_key" && (
        <p className="text-fg-2">
          {t(
            account.provider === "codex"
              ? "settings.codexOwnLoginNotice"
              : account.provider === "antigravity"
                ? "settings.antigravityOwnLoginNotice"
                : "settings.ownLoginNotice",
          )}
        </p>
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

/** Who else may use a person's own subscription: nobody, some people, or everyone here. */
function Sharing({ account, patch }: { account: Account; patch: (body: Record<string, unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const people = (useDirectory().data ?? []).filter((p) => p.id !== account.owner_id);
  const toggle = (id: number) =>
    patch({ shared_with: account.shared_with.includes(id) ? account.shared_with.filter((x) => x !== id) : [...account.shared_with, id] });

  return (
    <div className="space-y-1.5" data-part="account-sharing">
      <span className="block font-medium text-fg-2">{t("accounts.share.label")}</span>
      <div className="flex flex-wrap gap-1" role="radiogroup" aria-label={t("accounts.share.label")}>
        {SHARES.map((share) => (
          <button
            key={share}
            role="radio"
            aria-checked={account.share === share}
            onClick={() => share !== account.share && patch({ share })}
            className={clsx("rounded-sm px-2.5 py-1 ring-1 transition-colors", account.share === share ? "bg-sunk ring-fg" : "ring-line hover:bg-sunk")}
          >
            {t(`accounts.share.${share}`)}
          </button>
        ))}
      </div>
      {account.share === "people" &&
        (people.length ? (
          <ul className="flex flex-wrap gap-x-3 gap-y-0.5">
            {people.map((p) => (
              <li key={p.id}>
                <label className="flex cursor-pointer items-center gap-1.5 py-0.5">
                  <input type="checkbox" checked={account.shared_with.includes(p.id)} onChange={() => toggle(p.id)} />
                  <span className="truncate">{p.name || p.username}</span>
                </label>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-fg-3">{t("accounts.share.nobody")}</p>
        ))}
      <p className="text-fg-3">{t("accounts.share.hint")}</p>
    </div>
  );
}
