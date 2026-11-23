import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { checks, qk, useAccounts, useMeta, useSettings } from "../../api/queries";
import type { Account, ProviderInfo } from "../../api/types";
import { Field, FieldRow, Input, Select, Spinner, Toggle } from "../../components/ui";
import { equivalentModel, smallModel } from "../../lib/models";
import { AccountsEditor } from "../settings/AccountsEditor";
import { EndpointAccounts } from "../settings/EndpointAccounts";
import { SecretsEditor } from "../settings/SecretsEditor";
import { SshKeyEditor } from "../settings/SettingsDialog";
import { UsersEditor } from "../settings/UsersEditor";
import { DefaultModels } from "../settings/DefaultModels";
import { GithubTokenField } from "../settings/GithubTokenField";
import { Advice, Wizard, type WizardStep } from "./Wizard";

type Values = Record<string, any>;

const SUBSCRIPTIONS = ["claude_code", "codex", "antigravity"];
/** An account agents can work with: it has its secret, or is signed in on the runner itself. An
    endpoint may need no key (a model server of one's own); whether it answers, the check says. */
export const usable = (a: Account) =>
  a.enabled && (a.secret_set || a.secret_from_env || a.auth_mode === "runner_login" || a.auth_mode === "chatgpt" || a.auth_mode === "google" || !SUBSCRIPTIONS.includes(a.provider));

const byTier = (provider: ProviderInfo | undefined, tier: string) => provider?.models.find((m) => m.tier === tier)?.id;
/** The models PlainBoard would choose: the manager plans and decides, members do the work, summaries are routine. */
export const recommended = (provider?: ProviderInfo) => ({
  default_manager_model: byTier(provider, "large") ?? byTier(provider, "medium") ?? provider?.models[0]?.id ?? "",
  default_worker_model: byTier(provider, "medium") ?? provider?.models[0]?.id ?? "",
  default_summary_model: byTier(provider, "small") ?? provider?.models[0]?.id ?? "",
});

/** The basic settings of a new installation, once, for its admin: each step is one decision. */
export function SetupWizard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const settings = useSettings();
  const meta = useMeta();
  const accounts = useAccounts();
  const [values, setValues] = useState<Values | null>(null);
  const [githubToken, setGithubToken] = useState("");

  useEffect(() => {
    if (settings.data && values === null) {
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      setValues({ ...settings.data.values, ...(zone ? { timezone: zone } : {}) });
    }
  }, [settings.data, values]);

  // A new installation starts with the models PlainBoard recommends, as it does with the browser's
  // time zone: the admin changes what differs, instead of having to press "Use these" first.
  const advised = useRef(false);
  useEffect(() => {
    const found = (meta.data?.providers ?? []).find((p) => p.name === (values?.default_provider ?? "claude_code"));
    if (values === null || advised.current || !found) return;
    advised.current = true;
    setValues((v) => ({ ...v, ...recommended(found) }));
  }, [meta.data, values]);

  if (!settings.data || values === null) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }
  const set = (patch: Values) => setValues((v) => ({ ...v, ...patch }));
  const providers = meta.data?.providers ?? [];
  const provider = providers.find((p) => p.name === (values.default_provider ?? "claude_code"));
  const save = async (keys: string[], more: Values = {}) => {
    await api.put("/settings", { values: { ...Object.fromEntries(keys.map((k) => [k, values[k]])), ...more } });
    await qc.invalidateQueries({ queryKey: qk.settings });
  };
  const hasAccount = (accounts.data ?? []).some(usable);
  const advice = recommended(provider);
  const asAdvised = Object.entries(advice).every(([k, v]) => values[k] === v);
  const label = (id: string) => provider?.models.find((m) => m.id === id)?.label ?? id;

  const steps: WizardStep[] = [
    {
      id: "subscriptions",
      label: t("setup.subscriptions.label"),
      title: t("setup.subscriptions.title"),
      intro: t("setup.subscriptions.intro"),
      ready: hasAccount,
      missing: t("setup.subscriptions.missing"),
      canSkipFailure: true,
      // One of them has to answer: a subscription that does not work is found here, not by the first agent.
      onNext: async () => {
        const failures: string[] = [];
        for (const account of (accounts.data ?? []).filter(usable)) {
          const model = account.models[0] ?? smallModel(providers.find((p) => p.name === account.provider));
          const result = model ? await checks.account(account.id, model) : { ok: false, detail: t("status.runnerDown") };
          if (result.ok) return;
          failures.push(`${account.label}: ${result.detail}`);
        }
        throw new Error(t("setup.subscriptions.failed", { detail: failures.join(" · ") }));
      },
      content: (
        <div className="space-y-5">
          <Advice>{t("setup.subscriptions.advice")}</Advice>
          <AccountsEditor provider="claude_code" />
          {providers.some((p) => p.name === "codex") && (
            <div className="space-y-2">
              <h2 className="font-semibold">{t("settings.codex")}</h2>
              <AccountsEditor provider="codex" />
            </div>
          )}
          {providers.some((p) => p.name === "antigravity") && (
            <div className="space-y-2">
              <h2 className="font-semibold">{t("settings.antigravity")}</h2>
              <AccountsEditor provider="antigravity" />
            </div>
          )}
          {!!meta.data?.endpoints?.length && (
            <div className="space-y-2">
              <h2 className="font-semibold">{t("endpoints.title")}</h2>
              <p className="text-fg-3">{t("endpoints.hint")}</p>
              <EndpointAccounts />
            </div>
          )}
        </div>
      ),
    },
    {
      id: "models",
      label: t("setup.models.label"),
      title: t("setup.models.title"),
      intro: t("setup.models.intro"),
      ready: !!values.default_manager_model && !!values.default_worker_model && !!values.default_summary_model,
      missing: t("setup.models.missing"),
      onNext: () => save(["default_provider", "default_manager_model", "default_worker_model", "default_summary_model", "default_manager_effort", "default_worker_effort", "default_summary_effort"]),
      content: (
        <div className="space-y-4">
          {provider && (
            <Advice action={asAdvised ? undefined : t("setup.models.useAdvice")} onAction={() => set(advice)}>
              {t("setup.models.advice", { manager: label(advice.default_manager_model), worker: label(advice.default_worker_model), summary: label(advice.default_summary_model) })}
            </Advice>
          )}
          {providers.length > 1 && (
            <Field label={t("settings.defaultProvider")}>
              <Select
                value={values.default_provider ?? "claude_code"}
                onChange={(e) =>
                  set({
                    default_provider: e.target.value,
                    default_manager_model: equivalentModel(providers, values.default_manager_model ?? "", e.target.value),
                    default_worker_model: equivalentModel(providers, values.default_worker_model ?? "", e.target.value),
                    default_summary_model: equivalentModel(providers, values.default_summary_model ?? "", e.target.value),
                  })
                }
              >
                {providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.label}
                  </option>
                ))}
              </Select>
            </Field>
          )}
          <DefaultModels provider={provider} values={values} set={set} />
        </div>
      ),
    },
    {
      id: "code",
      label: t("setup.code.label"),
      title: t("setup.code.title"),
      intro: t("setup.code.intro"),
      onNext: async () => {
        await save(["git_author_name", "git_author_email"], githubToken ? { github_token: githubToken } : {});
        setGithubToken("");
      },
      content: (
        <div className="space-y-4">
          <Advice>{t("setup.code.advice")}</Advice>
          <Field
            group
            label={t("settings.githubToken")}
            hint={settings.data.values.github_token ? t("settings.secretSet") : settings.data.env_fallbacks.github_token ? t("settings.secretFromEnv") : t("setup.code.tokenHint")}
          >
            <GithubTokenField label={t("settings.githubToken")} value={githubToken} onChange={setGithubToken} />
          </Field>
          <SshKeyEditor publicKey={settings.data.values.git_ssh_public_key ?? ""} />
          <FieldRow>
            <Field label={t("settings.gitName")} hint={t("setup.code.authorHint")}>
              <Input value={values.git_author_name ?? ""} onChange={(e) => set({ git_author_name: e.target.value })} />
            </Field>
            <Field label={t("settings.gitEmail")}>
              <Input value={values.git_author_email ?? ""} onChange={(e) => set({ git_author_email: e.target.value })} />
            </Field>
          </FieldRow>
        </div>
      ),
    },
    {
      id: "pace",
      label: t("setup.pace.label"),
      title: t("setup.pace.title"),
      intro: t("setup.pace.intro"),
      onNext: () => save(["max_parallel_runs", "manager_parallel_limit", "timezone", "update_check"]),
      content: (
        <div className="space-y-4">
          <Advice>{t("setup.pace.advice")}</Advice>
          <FieldRow>
            <Field label={t("settings.maxParallel")} hint={t("settings.maxParallelHint")}>
              <Input type="number" min={0} max={20} value={values.max_parallel_runs ?? 3} onChange={(e) => set({ max_parallel_runs: Number(e.target.value) })} />
            </Field>
            <Field label={t("settings.managerParallel")} hint={t("settings.managerParallelHint")}>
              <Input type="number" min={0} max={20} value={values.manager_parallel_limit ?? 6} onChange={(e) => set({ manager_parallel_limit: Number(e.target.value) })} />
            </Field>
          </FieldRow>
          <Field label={t("settings.timezone")} hint={t("setup.pace.timezoneHint")}>
            <Input value={values.timezone ?? ""} onChange={(e) => set({ timezone: e.target.value })} placeholder="Europe/Berlin" />
          </Field>
          <div className="space-y-1">
            <Toggle checked={values.update_check ?? true} onChange={(v) => set({ update_check: v })} label={t("settings.updateCheck")} />
            <p className="text-fg-3">{t("settings.updateCheckHint")}</p>
          </div>
        </div>
      ),
    },
    {
      id: "secrets",
      label: t("setup.secrets.label"),
      title: t("setup.secrets.title"),
      intro: t("setup.secrets.intro"),
      content: <SecretsEditor projectId={null} />,
    },
    {
      id: "people",
      label: t("setup.people.label"),
      title: t("setup.people.title"),
      intro: t("setup.people.intro"),
      content: <UsersEditor />,
    },
  ];

  return <Wizard heading={t("setup.heading")} steps={steps} doneLabel={t("setup.finish")} onDone={() => save([], { setup_done: true })} />;
}
