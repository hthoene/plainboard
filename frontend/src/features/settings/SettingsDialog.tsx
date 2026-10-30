import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { CheckCircle2, XCircle } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { checks, qk, useMeta, useSettings } from "../../api/queries";
import type { CheckResult } from "../../api/types";
import { Button, ErrorText, Field, Input, Modal, Select } from "../../components/ui";
import { ModelSelect } from "../team/AgentEditor";

export function CheckBadge({ result }: { result: CheckResult | null }) {
  if (!result) return null;
  return (
    <span className={clsx("flex min-w-0 items-center gap-1 text-xs", result.ok ? "text-emerald-600" : "text-red-600")}>
      {result.ok ? <CheckCircle2 className="size-3.5 shrink-0" /> : <XCircle className="size-3.5 shrink-0" />}
      <span className="truncate" title={result.detail}>
        {result.detail}
      </span>
    </span>
  );
}

type Values = Record<string, any>;

export function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const settings = useSettings();
  const meta = useMeta();
  const [values, setValues] = useState<Values>({});
  const [secrets, setSecrets] = useState<Values>({});
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [claudeCheck, setClaudeCheck] = useState<CheckResult | null>(null);
  const [githubCheck, setGithubCheck] = useState<CheckResult | null>(null);

  useEffect(() => {
    if (open && settings.data) {
      setValues(settings.data.values);
      setSecrets({});
      setClaudeCheck(null);
      setGithubCheck(null);
    }
  }, [open, settings.data]);

  if (!settings.data) return null;
  const set = (patch: Values) => setValues((v) => ({ ...v, ...patch }));
  const setSecret = (patch: Values) => setSecrets((s) => ({ ...s, ...patch }));
  const provider = meta.data?.providers.find((p) => p.name === (values.default_provider ?? "claude_code"));
  const authMode = values.claude_auth_mode ?? values.claude_auth_mode_effective;

  const persist = async () => {
    const editable = Object.fromEntries(Object.entries(values).filter(([k]) => !settings.data!.secrets.includes(k) && k !== "claude_auth_mode_effective"));
    const changedSecrets = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v !== undefined));
    await api.put("/settings", { values: { ...editable, claude_auth_mode: authMode, ...changedSecrets } });
    setSecrets({});
    await qc.invalidateQueries({ queryKey: qk.settings });
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await persist();
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const runCheck = async (kind: "claude" | "github") => {
    const setter = kind === "claude" ? setClaudeCheck : setGithubCheck;
    setter({ ok: true, detail: t("settings.testing") });
    try {
      await persist();
      setter(kind === "claude" ? await checks.provider(values.default_provider) : await checks.github());
    } catch (err) {
      setter({ ok: false, detail: errorMessage(err) });
    }
  };

  const secretField = (key: string, label: string, hint?: ReactNode) => (
    <Field
      group
      label={label}
      hint={
        <>
          {settings.data!.values[key] ? t("settings.secretSet") : settings.data!.env_fallbacks[key] ? t("settings.secretFromEnv") : t("settings.secretUnset")}
          {hint && <> · {hint}</>}
        </>
      }
    >
      <div className="flex gap-2">
        <Input type="password" autoComplete="off" placeholder="••••••••" value={secrets[key] ?? ""} onChange={(e) => setSecret({ [key]: e.target.value })} />
        {settings.data!.values[key] && (
          <Button size="md" variant="ghost" onClick={() => setSecret({ [key]: "" })}>
            {t("settings.clear")}
          </Button>
        )}
      </div>
    </Field>
  );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("settings.title")}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" onClick={save} loading={busy}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-7">
        <Section title={t("settings.claude")} description={t("settings.claudeHint")}>
          <div className="grid grid-cols-3 gap-2">
            {(["api_key", "oauth_token", "runner_login"] as const).map((mode) => (
              <button
                key={mode}
                onClick={() => set({ claude_auth_mode: mode })}
                className={clsx(
                  "rounded-lg px-3 py-2 text-left text-xs ring-1 transition-colors",
                  authMode === mode ? "bg-indigo-50 ring-indigo-400 dark:bg-indigo-950/40" : "ring-zinc-200 hover:bg-zinc-50 dark:ring-zinc-800 dark:hover:bg-zinc-800",
                )}
              >
                <div className="font-medium">{t(`settings.auth.${mode}`)}</div>
                <div className="text-zinc-500">{t(`settings.auth.${mode}Hint`)}</div>
              </button>
            ))}
          </div>
          {authMode === "oauth_token" && secretField("claude_oauth_token", t("settings.oauthToken"), <code>claude setup-token</code>)}
          {authMode === "api_key" && secretField("anthropic_api_key", t("settings.apiKey"))}
          {authMode !== "api_key" && <p className="text-xs text-amber-700 dark:text-amber-400">{t("settings.ownLoginNotice")}</p>}
          {authMode === "runner_login" && (
            <p className="rounded-md bg-zinc-50 p-3 text-xs text-zinc-600 dark:bg-zinc-800/50 dark:text-zinc-400">
              {t("settings.runnerLoginHint")} <code className="font-mono">docker compose exec -it runner claude</code>
            </p>
          )}
          <div className="flex items-center gap-3">
            <Button size="sm" onClick={() => runCheck("claude")}>
              {t("settings.test")}
            </Button>
            <CheckBadge result={claudeCheck} />
          </div>
        </Section>

        <Section title={t("settings.github")} description={t("settings.githubHint")}>
          {secretField("github_token", t("settings.githubToken"))}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.gitName")}>
              <Input value={values.git_author_name ?? ""} onChange={(e) => set({ git_author_name: e.target.value })} />
            </Field>
            <Field label={t("settings.gitEmail")}>
              <Input value={values.git_author_email ?? ""} onChange={(e) => set({ git_author_email: e.target.value })} />
            </Field>
          </div>
          <div className="flex items-center gap-3">
            <Button size="sm" onClick={() => runCheck("github")}>
              {t("settings.test")}
            </Button>
            <CheckBadge result={githubCheck} />
          </div>
        </Section>

        <Section title={t("settings.defaults")} description={t("settings.defaultsHint")}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.defaultProvider")}>
              <Select value={values.default_provider ?? "claude_code"} onChange={(e) => set({ default_provider: e.target.value })}>
                {(meta.data?.providers ?? []).map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.label}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t("settings.defaultEffort")}>
              <Select value={values.default_effort ?? ""} onChange={(e) => set({ default_effort: e.target.value || null })}>
                <option value="">{t("team.effortDefault")}</option>
                {(provider?.efforts ?? []).map((e) => (
                  <option key={e} value={e}>
                    {e}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t("settings.managerModel")}>
              <ModelSelect provider={provider} value={values.default_manager_model ?? ""} onChange={(v) => set({ default_manager_model: v })} />
            </Field>
            <Field label={t("settings.workerModel")}>
              <ModelSelect provider={provider} value={values.default_worker_model ?? ""} onChange={(v) => set({ default_worker_model: v })} />
            </Field>
            <Field label={t("settings.summaryModel")} hint={t("team.summaryModelHint")}>
              <ModelSelect provider={provider} value={values.default_summary_model ?? ""} onChange={(v) => set({ default_summary_model: v })} />
            </Field>
            <Field label={t("settings.contextLimit")} hint={t("team.contextLimitHint")}>
              <Input type="number" min={5000} step={5000} value={values.default_context_limit ?? 400000} onChange={(e) => set({ default_context_limit: Number(e.target.value) })} />
            </Field>
            <Field label={t("settings.keepRecent")}>
              <Input type="number" min={0} max={50} value={values.default_keep_recent ?? 6} onChange={(e) => set({ default_keep_recent: Number(e.target.value) })} />
            </Field>
            <Field label={t("settings.maxParallel")} hint={t("settings.maxParallelHint")}>
              <Input type="number" min={0} max={20} value={values.max_parallel_runs ?? 3} onChange={(e) => set({ max_parallel_runs: Number(e.target.value) })} />
            </Field>
            <Field label={t("settings.timezone")}>
              <Input value={values.timezone ?? ""} onChange={(e) => set({ timezone: e.target.value })} placeholder="Europe/Berlin" />
            </Field>
          </div>
        </Section>
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}

function Section({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">{title}</h3>
        <p className="text-xs text-zinc-500">{description}</p>
      </div>
      {children}
    </section>
  );
}
