import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Check, Plus, Trash2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useOptimistic } from "../../lib/hooks";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { checks, qk, useAccounts, useDirectory, useLimits, useMe, useMeta, useProjects, useSettings } from "../../api/queries";
import type { Account, CheckResult } from "../../api/types";
import { Button, ErrorText, IconButton, Input, Toggle } from "../../components/ui";
import { smallModel } from "../../lib/models";
import { doneCount, StatusMark, useAccountStatus, type StatusItem } from "./AccountStatus";
import { CheckBadge } from "./SettingsDialog";
import { SignIn } from "./SignIn";

const MODES: Record<string, string[]> = {
  claude_code: ["api_key", "oauth_token", "runner_login"],
  codex: ["chatgpt", "api_key"],
  gemini_cli: ["google", "api_key"],
};
// The text of a sign-in kind. A sign-in in the browser is kept by PlainBoard and works on every runner.
const modeText = (provider: string, mode: string) => `${MODE_TEXTS[provider]}.${mode}`;
const SHARES = ["private", "people", "everyone"] as const;
// i18n group of the sign-in kinds per provider
const MODE_TEXTS: Record<string, string> = { claude_code: "settings.auth", codex: "settings.codexAuth", gemini_cli: "settings.geminiAuth" };
// The note under a sign-in with one's own account, per provider
const OWN_LOGIN_NOTICES: Record<string, string> = { codex: "settings.codexOwnLoginNotice", gemini_cli: "settings.geminiOwnLoginNotice" };
const SECRET_LABELS: Record<string, string> = {
  "claude_code:oauth_token": "settings.oauthToken",
  "claude_code:api_key": "settings.apiKey",
  "codex:api_key": "settings.openaiApiKey",
  "gemini_cli:api_key": "settings.geminiApiKey",
};

/** The accounts of one provider: the installation's, or with ``own`` people's own ones (a non-admin:
 *  their own, which they add here; an admin: everyone's, without adding). Saved right away; secrets are write-only. */
export function AccountsEditor({ provider, own = false }: { provider: string; own?: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const me = useMe();
  const accounts = useAccounts();
  const [error, setError] = useState<unknown>(null);
  const allowed = useSettings().data?.values.own_accounts ?? true; // #138: an admin may turn adding one's own off
  const adds = !own || (!me?.is_admin && allowed);
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
        <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => run(() => api.post("/accounts", { provider, auth_mode: MODES[provider][0] }))}>
          {t("accounts.add")}
        </Button>
      )}
      {own && me?.is_admin && !mine.length && <p className="text-fg-3">{t("accounts.noneOwn")}</p>}
      <ErrorText error={error} />
    </div>
  );
}

function AccountCard({ account, onChange }: { account: Account; onChange: (action: () => Promise<unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const meta = useMeta();
  const me = useMe();
  const modes = MODES[account.provider]; // a person's own has every sign-in too: one in the browser is kept for it alone
  const limits = useLimits();
  const detected = limits.data?.accounts.find((entry) => entry.account_id === account.id);
  const [label, setLabel] = useState(account.label);
  const [size, setSize] = useState(account.plan_size?.toString() ?? "");
  const [secret, setSecret] = useState("");
  const [check, setCheck] = useState<CheckResult | null>(null);
  useEffect(() => setLabel(account.label), [account.label]);
  useEffect(() => setSize(account.plan_size?.toString() ?? ""), [account.plan_size]);
  const status = useAccountStatus(account);


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

  const head = (
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
  );
  const ownedBy = account.owner_id != null && account.owner_id !== me?.id && <p className="text-fg-3">{t("accounts.ownedBy", { name: account.owner_name })}</p>;
  const sharing = account.owner_id != null && <Sharing account={account} patch={patch} />;
  const give = account.owner_id != null && account.owner_id === me?.id && <GiveToProjects account={account} onChange={onChange} />;
  const signIn = (
    <div className="space-y-3" data-part="account-signin">
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
            <div className="font-medium">{t(modeText(account.provider, mode))}</div>
            <div className="text-fg-3">{t(`${modeText(account.provider, mode)}Hint`)}</div>
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
      {account.auth_mode === "runner_login" && <SignIn accountId={account.id} provider={account.provider} />}
      {((account.provider === "codex" && account.auth_mode === "chatgpt") || account.auth_mode === "google") && <SignIn accountId={account.id} provider={account.provider} />}
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
        <p className="text-fg-2">{t(OWN_LOGIN_NOTICES[account.provider] ?? "settings.ownLoginNotice")}</p>
      )}
      <div className="flex items-center gap-3">
        <Button size="sm" onClick={test}>
          {t("settings.test")}
        </Button>
        <CheckBadge result={check} />
      </div>
    </div>
  );
  return (
    <div className={clsx("space-y-3 rounded-sm p-3 ring-1 ring-line", !account.enabled && "opacity-60")}>
      {head}
      {ownedBy}
      <p className="text-fg-3">{t("accountStatus.progress", doneCount(status))}</p>
      <ol data-part="account-steps">
        <Step n={1} item={status[0]} title={t("accountStatus.step.login")}>
          {signIn}
        </Step>
        <Step n={2} item={status[1]} title={t("accountStatus.step.share")}>
          {sharing || <p className="text-fg-3">{t("accountStatus.share.installationHint")}</p>}
        </Step>
        <Step n={3} item={status[2]} title={t("accountStatus.step.projects")}>
          {give}
          <p className="text-fg-3">{t(account.owner_id == null ? "accountStatus.projects.installationHint" : "accountStatus.projects.ownHint")}</p>
        </Step>
        <Step n={4} item={status[3]} title={status[3].label ?? t("accountStatus.step.limits")} last>
          <p className="text-fg-3">{t(account.auth_mode === "api_key" ? "accountStatus.use.hint" : "accountStatus.limits.hint")}</p>
        </Step>
      </ol>
    </div>
  );
}

/** One numbered step of setting a subscription up, checked once it is in order (#140). */
export function Step({ n, item, title, last, children }: { n: number; item: StatusItem; title: string; last?: boolean; children: ReactNode }) {
  return (
    <li className="grid grid-cols-[1.5rem_minmax(0,1fr)] gap-x-3" data-step={item.key}>
      <div className="flex flex-col items-center">
        <span
          className={clsx(
            "flex size-6 flex-none items-center justify-center rounded-full font-medium ring-1",
            item.tone === "done" ? "text-ok ring-ok" : item.tone === "todo" ? "bg-you-bg text-you-fg ring-you" : "text-fg-3 ring-line-2",
          )}
        >
          {item.tone === "done" ? <Check className="size-3.5" /> : n}
        </span>
        {!last && <span className="my-1 w-px flex-1 bg-line" />}
      </div>
      <div className={clsx("min-w-0 space-y-2", !last && "pb-5")}>
        <div className="flex min-h-6 flex-wrap items-center gap-x-2">
          <span className="font-semibold">{title}</span>
          <span className={clsx("flex min-w-0 items-center gap-1.5", item.tone === "todo" ? "text-you-fg" : "text-fg-3")}>
            <StatusMark tone={item.tone} />
            <span className="min-w-0">{item.text}</span>
          </span>
        </div>
        {children}
      </div>
    </li>
  );
}

/** Who else may use a person's own subscription: nobody, some people, or everyone here. */
export function Sharing({ account, patch }: { account: Account; patch: (body: Record<string, unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const people = (useDirectory().data ?? []).filter((p) => p.id !== account.owner_id);
  // Shown at once, saved behind it (#188).
  const [chosen, choose] = useOptimistic(account.share);
  const [sharedWith, shareWith] = useOptimistic(account.shared_with);
  const toggle = (id: number) =>
    void shareWith(sharedWith.includes(id) ? sharedWith.filter((x) => x !== id) : [...sharedWith, id], (shared_with) => patch({ shared_with }));

  return (
    <div className="space-y-1.5" data-part="account-sharing">
      <span className="block font-medium text-fg-2">{t("accounts.share.label")}</span>
      <div className="flex flex-wrap gap-1" role="radiogroup" aria-label={t("accounts.share.label")}>
        {SHARES.map((share) => (
          <button
            key={share}
            role="radio"
            aria-checked={chosen === share}
            onClick={() => share !== chosen && void choose(share, () => patch({ share }))}
            className={clsx("rounded-sm px-2.5 py-1 ring-1 transition-colors", chosen === share ? "bg-sunk ring-fg" : "ring-line hover:bg-sunk")}
          >
            {t(`accounts.share.${share}`)}
          </button>
        ))}
      </div>
      {chosen === "people" &&
        (people.length ? (
          <ul className="flex flex-wrap gap-x-3 gap-y-0.5">
            {people.map((p) => (
              <li key={p.id}>
                <label className="flex cursor-pointer items-center gap-1.5 py-0.5">
                  <input type="checkbox" checked={sharedWith.includes(p.id)} onChange={() => toggle(p.id)} />
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

/** The projects one works in that get one's own subscription for their runs, whoever it is shared with. */
function GiveToProjects({ account, onChange }: { account: Account; onChange: (action: () => Promise<unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const projects = (useProjects().data ?? []).filter((p) => p.role === "member" || p.role === "owner");
  const [given, give] = useOptimistic(account.project_ids ?? []);
  if (!projects.length) return null;
  const toggle = (projectId: number) =>
    void give(given.includes(projectId) ? given.filter((id) => id !== projectId) : [...given, projectId], () =>
      onChange(async () => {
        await api.put(`/projects/${projectId}/given-accounts/${account.id}`, { given: !given.includes(projectId) });
        await Promise.all([qc.invalidateQueries({ queryKey: ["project-accounts", projectId] }), qc.invalidateQueries({ queryKey: ["given-accounts", projectId] })]);
      }),
    );

  return (
    <div className="space-y-1.5" data-part="account-projects">
      <span className="block font-medium text-fg-2">{t("givenAccounts.toProjects")}</span>
      <ul className="flex flex-wrap gap-x-3 gap-y-0.5">
        {projects.map((p) => (
          <li key={p.id}>
            <label className="flex cursor-pointer items-center gap-1.5 py-0.5">
              <input type="checkbox" checked={given.includes(p.id)} onChange={() => toggle(p.id)} />
              <span className="truncate">{p.name}</span>
            </label>
          </li>
        ))}
      </ul>
      <p className="text-fg-3">{t("givenAccounts.toProjectsHint")}</p>
    </div>
  );
}
