import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { CheckCircle2, X, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useAccounts, useMe, useMeta, useSettings } from "../../api/queries";
import type { CheckResult, RetentionPreview } from "../../api/types";
import { ApiKeyEditor } from "../../components/ApiKeyEditor";
import { SaveRow, SavedAtOnce, SettingsLayout, type SettingsSection } from "../../components/SettingsLayout";
import { Button, ErrorText, Field, FieldRow, IconButton, Input, PageHead, Select, Textarea, Toggle } from "../../components/ui";
import { useStored } from "../../lib/hooks";
import { equivalentModel } from "../../lib/models";
import { updateHow } from "../../lib/version";
import { DatabaseEditor } from "./DatabaseEditor";
import { DefaultModels } from "./DefaultModels";
import { GithubTokenField } from "./GithubTokenField";
import { AccountEditor } from "./AccountEditor";
import { AccountsEditor } from "./AccountsEditor";
import { EndpointAccounts } from "./EndpointAccounts";
import { McpAccessEditor } from "./McpAccessEditor";
import { PauseEditor } from "./PauseEditor";
import { RetentionConfirm } from "../projects/RetentionConfirm";
import { RunnersEditor } from "./RunnersEditor";
import { SearchEditor } from "./SearchEditor";
import { SecretsEditor } from "./SecretsEditor";
import { UsageEditor } from "./UsageEditor";
import { UsersEditor } from "./UsersEditor";
import luchsApproved from "../../assets/luchs-approved.webp";

export function CheckBadge({ result }: { result: CheckResult | null }) {
  if (!result) return null;
  return (
    <span className={clsx("flex min-w-0 items-center gap-1", result.ok ? "text-ok" : "text-you-fg")}>
      {result.ok ? <CheckCircle2 className="size-3.5 shrink-0" /> : <XCircle className="size-3.5 shrink-0" />}
      <span className="truncate" title={result.detail}>
        {result.detail}
      </span>
    </span>
  );
}

type Values = Record<string, any>;

/** The installation's settings on a page of its own: one section per subject. Everyone has the
 * section about their own account; the rest is the admins'. */
/** `section`: the section to show when it opens, instead of the one that was open last. */
export function SettingsDialog({ open, onClose, section }: { open: boolean; onClose: () => void; section?: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const settings = useSettings();
  const meta = useMeta();
  const accounts = useAccounts();
  const me = useMe();
  const [current, setCurrent] = useStored<string>("plainboard.settingsSection", "account");
  useEffect(() => {
    if (open && section) setCurrent(section);
  }, [open, section, setCurrent]);
  const [values, setValues] = useState<Values>({});
  const [githubToken, setGithubToken] = useState<string | null>(null); // null: untouched, "": clear it
  const [retention, setRetention] = useState<RetentionPreview | null>(null);
  const [retentionBusy, setRetentionBusy] = useState(false);
  const [retentionError, setRetentionError] = useState<unknown>(null);

  useEffect(() => {
    if (open && settings.data) {
      setValues(settings.data.values);
      setGithubToken(null);
    }
  }, [open, settings.data]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || !me) return null;
  const others = (accounts.data ?? []).some((a) => a.owner_id != null);
  const ownEndpoints = (accounts.data ?? []).some((a) => a.owner_id != null && meta.data?.endpoints?.some((p) => p.name === a.provider));
  const stored = settings.data?.values ?? {};
  const set = (patch: Values) => setValues((v) => ({ ...v, ...patch }));
  const dirty = (...keys: string[]) => keys.some((k) => values[k] !== stored[k]);
  const save = async (keys: string[], more: Values = {}) => {
    await api.put("/settings", { values: { ...Object.fromEntries(keys.map((k) => [k, values[k]])), ...more } });
    await qc.invalidateQueries({ queryKey: qk.settings });
  };
  const providers = meta.data?.providers ?? [];
  const provider = providers.find((p) => p.name === (values.default_provider ?? "claude_code"));
  const setDefaultProvider = (name: string) =>
    set({
      default_provider: name,
      default_manager_model: equivalentModel(providers, values.default_manager_model ?? "", name),
      default_worker_model: equivalentModel(providers, values.default_worker_model ?? "", name),
      default_summary_model: equivalentModel(providers, values.default_summary_model ?? "", name),
    });
  const saveCode = async () => {
    await save(["git_author_name", "git_author_email"], githubToken === null ? {} : { github_token: githubToken });
    setGithubToken(null);
  };
  const MODELS = ["default_provider", "default_manager_model", "default_worker_model", "default_summary_model", "default_manager_effort", "default_worker_effort", "default_summary_effort", "default_context_limit", "default_keep_recent"];
  const PACE = ["max_parallel_runs", "manager_parallel_limit"];
  const GENERAL = ["timezone", "member_chat", "update_check"];
  const RETENTION = ["attachment_retention_days", "attachment_retention_min_mb"];
  // The default's preview covers every project that follows it; saved only after that.
  const saveRetention = async () => {
    const days = Number(values.attachment_retention_days ?? 0);
    if (days > 0) {
      setRetention(await api.get<RetentionPreview>(`/settings/attachment-retention?days=${days}&min_mb=${Number(values.attachment_retention_min_mb ?? 0)}`));
      return;
    }
    await save(RETENTION);
  };
  const confirmRetention = async () => {
    setRetentionBusy(true);
    setRetentionError(null);
    try {
      await save(RETENTION);
      setRetention(null);
    } catch (err) {
      setRetentionError(err);
    } finally {
      setRetentionBusy(false);
    }
  };
  const CLEANUP = ["storage_notify_percent", "storage_emergency_percent", "cleanup", "cleanup_memory_free_percent", "tool_details_days"];

  const account: SettingsSection = { id: "account", label: t("account.title"), description: t("account.hint"), content: <AccountEditor /> };
  const mcp: SettingsSection = {
    id: "mcp",
    label: t("mcp.title"),
    description: t("mcp.description"),
    content: (
      <McpAccessEditor
        enabled={stored.mcp_enabled ?? true}
        admin={me.is_admin}
        onToggle={async (on) => {
          await api.put("/settings", { values: { mcp_enabled: on } });
          await qc.invalidateQueries({ queryKey: qk.settings });
        }}
      />
    ),
  };
  const ownAccounts = (
    <>
      <div className="space-y-2">
        <h3 className="font-semibold">{t("settings.claude")}</h3>
        <AccountsEditor provider="claude_code" own />
      </div>
      {providers.some((p) => p.name === "codex") && (
        <div className="space-y-2">
          <h3 className="font-semibold">{t("settings.codex")}</h3>
          <AccountsEditor provider="codex" own />
        </div>
      )}
      {!!meta.data?.endpoints?.length && (!me.is_admin || ownEndpoints) && (
        <div className="space-y-2">
          <h3 className="font-semibold">{t("endpoints.title")}</h3>
          {!me.is_admin && <p className="text-fg-3">{t("endpoints.hint")}</p>}
          <EndpointAccounts own />
        </div>
      )}
    </>
  );
  const own: SettingsSection[] = me.is_admin
    ? []
    : [
        {
          id: "subscriptions",
          label: t("settingsPage.own.label"),
          description: t("settingsPage.own.description"),
          content: (
            <div className="space-y-6">
              <p className="text-fg-3">{t("settingsPage.own.hint")}</p>
              {ownAccounts}
              <SavedAtOnce />
            </div>
          ),
        },
      ];
  const admin: SettingsSection[] = !me.is_admin || !settings.data
    ? []
    : [
        { id: "people", label: t("users.title"), description: t("users.hint"), content: <UsersEditor /> },
        {
          id: "subscriptions",
          label: t("settingsPage.subscriptions.label"),
          description: t("settingsPage.subscriptions.description"),
          content: (
            <div className="space-y-6">
              <div className="space-y-2">
                <h3 className="font-semibold">{t("settings.claude")}</h3>
                <p className="text-fg-3">{t("settings.claudeHint")}</p>
                <AccountsEditor provider="claude_code" />
              </div>
              {providers.some((p) => p.name === "codex") && (
                <div className="space-y-2">
                  <h3 className="font-semibold">{t("settings.codex")}</h3>
                  <p className="text-fg-3">{t("settings.codexHint")}</p>
                  <AccountsEditor provider="codex" />
                </div>
              )}
              {!!meta.data?.endpoints?.length && (
                <div className="space-y-2">
                  <h3 className="font-semibold">{t("endpoints.title")}</h3>
                  <p className="text-fg-3">{t("endpoints.hint")}</p>
                  <EndpointAccounts />
                </div>
              )}
              {others && (
                <div className="space-y-4 border-t border-line pt-4">
                  <div className="space-y-1">
                    <h3 className="font-semibold">{t("settingsPage.own.peoples")}</h3>
                    <p className="text-fg-3">{t("settingsPage.own.peoplesHint")}</p>
                  </div>
                  {ownAccounts}
                </div>
              )}
              <SavedAtOnce />
            </div>
          ),
        },
        {
          id: "models",
          label: t("settingsPage.models.label"),
          description: t("settingsPage.models.description"),
          content: (
            <div className="space-y-4">
              {providers.length > 1 && (
                <Field label={t("settings.defaultProvider")} hint={t("settingsPage.models.providerHint")}>
                  <Select value={values.default_provider ?? "claude_code"} onChange={(e) => setDefaultProvider(e.target.value)}>
                    {providers.map((p) => (
                      <option key={p.name} value={p.name}>
                        {p.label}
                      </option>
                    ))}
                  </Select>
                </Field>
              )}
              <DefaultModels provider={provider} values={values} set={set} />
              <FieldRow>
                <Field label={t("settings.contextLimit")} hint={t("team.contextLimitHint")}>
                  <Input type="number" min={5000} step={5000} value={values.default_context_limit ?? 350000} onChange={(e) => set({ default_context_limit: Number(e.target.value) })} />
                </Field>
                <Field label={t("settings.keepRecent")} hint={t("settingsPage.models.keepRecentHint")}>
                  <Input type="number" min={0} max={50} value={values.default_keep_recent ?? 6} onChange={(e) => set({ default_keep_recent: Number(e.target.value) })} />
                </Field>
              </FieldRow>
              <SaveRow dirty={dirty(...MODELS)} onSave={() => save(MODELS)} />
            </div>
          ),
        },
        {
          id: "code",
          label: t("settingsPage.code.label"),
          description: t("settingsPage.code.description"),
          content: (
            <div className="space-y-4">
              <Field
                group
                label={t("settings.githubToken")}
                hint={
                  <>
                    {stored.github_token ? t("settings.secretSet") : settings.data.env_fallbacks.github_token ? t("settings.secretFromEnv") : t("settings.secretUnset")} · {t("settingsPage.code.tokenHint")}
                  </>
                }
              >
                <GithubTokenField label={t("settings.githubToken")} value={githubToken ?? ""} onChange={setGithubToken}>
                  {stored.github_token && (
                    <Button variant="ghost" onClick={() => setGithubToken("")}>
                      {t("settings.clear")}
                    </Button>
                  )}
                </GithubTokenField>
              </Field>
              <FieldRow>
                <Field label={t("settings.gitName")} hint={t("setup.code.authorHint")}>
                  <Input value={values.git_author_name ?? ""} onChange={(e) => set({ git_author_name: e.target.value })} />
                </Field>
                <Field label={t("settings.gitEmail")}>
                  <Input value={values.git_author_email ?? ""} onChange={(e) => set({ git_author_email: e.target.value })} />
                </Field>
              </FieldRow>
              <SaveRow dirty={dirty("git_author_name", "git_author_email") || githubToken !== null} onSave={saveCode} />
              <div className="pt-2">
                <SshKeyEditor publicKey={stored.git_ssh_public_key ?? ""} />
              </div>
            </div>
          ),
        },
        {
          id: "pace",
          label: t("settingsPage.pace.label"),
          description: t("settingsPage.pace.description"),
          content: (
            <div className="space-y-4">
              <Field label={t("settings.maxParallel")} hint={t("settings.maxParallelHint")}>
                <Input type="number" min={0} max={20} className="w-28" value={values.max_parallel_runs ?? 3} onChange={(e) => set({ max_parallel_runs: Number(e.target.value) })} />
              </Field>
              <Field label={t("settings.managerParallel")} hint={t("settings.managerParallelHint")}>
                <Input type="number" min={0} max={20} className="w-28" value={values.manager_parallel_limit ?? 6} onChange={(e) => set({ manager_parallel_limit: Number(e.target.value) })} />
              </Field>
              <SaveRow dirty={dirty(...PACE)} onSave={() => save(PACE)} />
            </div>
          ),
        },
        {
          id: "secrets",
          label: t("secrets.title"),
          description: t("secrets.globalHint"),
          content: (
            <div className="space-y-4">
              <SecretsEditor projectId={null} />
              <SavedAtOnce />
            </div>
          ),
        },
        {
          id: "general",
          label: t("settings.general"),
          description: t("settingsPage.general.description"),
          content: (
            <div className="space-y-4">
              <Field label={t("settings.timezone")} hint={t("settingsPage.general.timezoneHint")}>
                <Input value={values.timezone ?? ""} onChange={(e) => set({ timezone: e.target.value })} placeholder="Europe/Berlin" />
              </Field>
              <div className="space-y-1">
                <Toggle checked={values.member_chat ?? false} onChange={(v) => set({ member_chat: v })} label={t("settings.memberChat")} />
                <p className="text-fg-3">{t("settings.memberChatHint")}</p>
              </div>
              <div className="space-y-1">
                <Toggle checked={values.update_check ?? true} onChange={(v) => set({ update_check: v })} label={t("settings.updateCheck")} />
                <p className="text-fg-3">
                  {t("settings.updateCheckHint")}
                  {meta.data?.version && ` ${t("settings.versionRunning", { commit: meta.data.version.name })}`}
                  {meta.data?.version?.behind ? ` ${updateHow(t, meta.data.version)}` : meta.data?.version?.behind === 0 ? ` ${t("settings.versionCurrent")}` : ""}
                </p>
              </div>
              <SaveRow dirty={dirty(...GENERAL)} onSave={() => save(GENERAL)} />
              <DatabaseEditor />
              <div className="flex justify-end">
                <div className="rounded-lg border border-line bg-sunk p-4">
                  <img src={luchsApproved} alt={t("settings.luchsApproved")} width={150} draggable={false} className="pointer-events-none w-[150px] -rotate-3 select-none" />
                </div>
              </div>
            </div>
          ),
        },
        { id: "pause", label: t("pause.title"), description: t("pause.description"), content: <PauseEditor /> },
        { id: "search", label: t("search.title"), description: t("search.description"), content: <SearchEditor enabled={stored.semantic_search ?? true} /> },
        { id: "runners", label: t("runners.title"), description: t("runners.description"), content: <RunnersEditor /> },
        {
          id: "usage",
          label: t("usage.title"),
          description: t("usage.description"),
          content: (
            <div className="space-y-6">
              <UsageEditor />
              <section className="space-y-4">
                <h3 className="caps rule-b pb-1.5">{t("cleanup.title")}</h3>
                <FieldRow>
                  <Field label={t("cleanup.notify")} hint={t("cleanup.notifyHint")}>
                    <Input type="number" min={1} max={100} value={values.storage_notify_percent ?? 80} onChange={(e) => set({ storage_notify_percent: Number(e.target.value) })} />
                  </Field>
                  <Field label={t("cleanup.emergency")} hint={t("cleanup.emergencyHint")}>
                    <Input type="number" min={1} max={100} value={values.storage_emergency_percent ?? 97} onChange={(e) => set({ storage_emergency_percent: Number(e.target.value) })} />
                  </Field>
                </FieldRow>
                <div className="space-y-1">
                  <Toggle checked={values.cleanup ?? true} onChange={(v) => set({ cleanup: v })} label={t("cleanup.auto")} />
                  <p className="text-fg-3">{t("cleanup.hint")}</p>
                </div>
                <FieldRow>
                  <Field label={t("cleanup.memory")} hint={t("cleanup.memoryHint")}>
                    <Input type="number" min={0} max={50} value={values.cleanup_memory_free_percent ?? 10} onChange={(e) => set({ cleanup_memory_free_percent: Number(e.target.value) })} />
                  </Field>
                  <Field label={t("cleanup.toolDetails")} hint={t("cleanup.toolDetailsHint")}>
                    <Input type="number" min={0} value={values.tool_details_days ?? 30} onChange={(e) => set({ tool_details_days: Number(e.target.value) })} />
                  </Field>
                </FieldRow>
                <SaveRow dirty={dirty(...CLEANUP)} onSave={() => save(CLEANUP)} />
              </section>
              <section className="space-y-3">
                <Toggle
                  checked={Number(values.attachment_retention_days ?? 0) > 0}
                  onChange={(v) => set({ attachment_retention_days: v ? Number(stored.attachment_retention_days) || 90 : 0 })}
                  label={<span className="font-semibold">{t("projects.retentionDefault")}</span>}
                />
                <p className="text-fg-3">{t("projects.retentionDefaultHint")}</p>
                {Number(values.attachment_retention_days ?? 0) > 0 && (
                  <FieldRow>
                    <Field label={t("projects.retentionDays")}>
                      <Input
                        type="number"
                        min={1}
                        max={3650}
                        value={values.attachment_retention_days}
                        onChange={(e) => set({ attachment_retention_days: Math.max(1, Math.min(3650, Math.round(Number(e.target.value) || 0))) })}
                      />
                    </Field>
                    <Field label={t("projects.retentionMinMb")}>
                      <Input
                        type="number"
                        min={0}
                        max={100000}
                        value={values.attachment_retention_min_mb ?? 0}
                        onChange={(e) => set({ attachment_retention_min_mb: Math.max(0, Math.min(100000, Math.round(Number(e.target.value) || 0))) })}
                      />
                    </Field>
                  </FieldRow>
                )}
                <SaveRow dirty={dirty(...RETENTION)} onSave={saveRetention} />
                <RetentionConfirm preview={retention} busy={retentionBusy} error={retentionError} onCancel={() => setRetention(null)} onConfirm={confirmRetention} />
              </section>
            </div>
          ),
        },
        {
          id: "readAccess",
          label: t("settings.readAccess"),
          description: t("settings.readAccessHint"),
          content: (
            <div className="space-y-4">
              <ApiKeyEditor path="/access-keys" revokePath="/access-keys" placeholder={t("settings.readAccessLabel")} />
              <SavedAtOnce />
            </div>
          ),
        },
      ];

  return (
    // A window over the page where there is room for one (it keeps its size from section to section); the whole screen on a phone.
    <div className="fixed inset-0 z-50 flex sm:items-center sm:justify-center sm:bg-black/55 sm:p-6" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("settings.title")}
        data-part="modal"
        onMouseDown={(e) => e.stopPropagation()}
        className="flex min-h-0 w-full flex-col overflow-hidden bg-bg pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] sm:h-[min(42rem,100%)] sm:max-w-[60rem] sm:rounded-sm sm:pt-0 sm:pb-0 sm:shadow-lg sm:ring-1 sm:ring-line-2"
      >
        <PageHead title={t("settings.title")}>
          <IconButton label={t("common.close")} onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        </PageHead>
        <SettingsLayout label={t("settings.title")} sections={[account, mcp, ...own, ...admin]} current={current} onChange={setCurrent} />
      </div>
    </div>
  );
}

/** One SSH key for all repositories with an SSH address. Saved right away, apart from the form. */
export function SshKeyEditor({ publicKey }: { publicKey: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [own, setOwn] = useState("");
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const change = async (action: () => Promise<unknown>) => {
    setError(null);
    try {
      await action();
      setOwn("");
      setCopied(false);
      await qc.invalidateQueries({ queryKey: qk.settings });
    } catch (err) {
      setError(err);
    }
  };
  const create = (privateKey?: string) => {
    if (publicKey && !confirm(t("settings.sshKeyConfirmNew"))) return;
    change(() => api.post("/settings/git-ssh-key", privateKey ? { private_key: privateKey } : {}));
  };
  const remove = () => {
    if (confirm(t("settings.sshKeyConfirmRemove"))) change(() => api.del("/settings/git-ssh-key"));
  };
  const copy = async () => {
    await navigator.clipboard.writeText(publicKey);
    setCopied(true);
  };

  return (
    <Field group label={t("settings.sshKey")} hint={t("settings.sshKeyHint")}>
      <div className="space-y-2">
        {publicKey ? (
          <code className="block rounded-sm bg-sunk px-2 py-1.5 font-mono break-all ring-1 ring-line">{publicKey}</code>
        ) : (
          <p className="text-fg-3">{t("settings.sshKeyNone")}</p>
        )}
        <div className="flex flex-wrap gap-2">
          {publicKey && (
            <Button size="sm" variant="primary" onClick={copy}>
              {copied ? t("projects.repoSshCopied") : t("projects.repoSshCopy")}
            </Button>
          )}
          <Button size="sm" onClick={() => create()}>
            {publicKey ? t("settings.sshKeyNew") : t("settings.sshKeyCreate")}
          </Button>
          {publicKey && (
            <Button size="sm" variant="ghost" onClick={remove}>
              {t("settings.sshKeyRemove")}
            </Button>
          )}
        </div>
        <details className="text-fg-3">
          <summary className="cursor-pointer">{t("settings.sshKeyOwn")}</summary>
          <div className="mt-2 space-y-2">
            <Textarea
              rows={3}
              className="font-mono"
              autoComplete="off"
              spellCheck={false}
              aria-label={t("settings.sshKeyOwn")}
              placeholder={t("projects.repoSshOwnPlaceholder")}
              value={own}
              onChange={(e) => setOwn(e.target.value)}
            />
            <Button size="sm" disabled={!own.trim()} onClick={() => create(own)}>
              {t("settings.sshKeyOwnSave")}
            </Button>
          </div>
        </details>
        <ErrorText error={error} />
      </div>
    </Field>
  );
}
