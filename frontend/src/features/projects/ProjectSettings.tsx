import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { checks, patchProject, qk, useAccounts, useAgents, useMeta, useRepositories, useSettings } from "../../api/queries";
import type { CheckResult, RetentionPreview } from "../../api/types";
import { ApiKeyEditor } from "../../components/ApiKeyEditor";
import { SaveRow, SavedAtOnce, SettingsLayout, type SettingsSection } from "../../components/SettingsLayout";
import { Button, ErrorText, Field, Input, Modal, PageHead, Select, Textarea, Toggle } from "../../components/ui";
import { useStored } from "../../lib/hooks";
import { useShell } from "../layout/shell-context";
import { GithubTokenField } from "../settings/GithubTokenField";
import { SecretsEditor } from "../settings/SecretsEditor";
import { CheckBadge } from "../settings/SettingsDialog";
import { MembersEditor } from "./MembersEditor";
import { ProjectAccounts, allowedAccounts, sameAccounts, useUsableAccounts } from "./ProjectAccounts";
import { ProjectPauseSwitch } from "./ProjectPause";
import { RetentionConfirm, retentionText } from "./RetentionConfirm";
import { ProjectRunnerEditor } from "./ProjectRunner";
import { ExportProject } from "./MoveProject";
import { GivenAccounts } from "./GivenAccounts";
import { CustomerLanguages } from "./CustomerLanguages";
import { RepositoriesEditor, toBody, toDraft } from "./RepositoriesEditor";
import { SkillsEditor } from "./SkillsEditor";
import { ToolsView } from "../tools/ToolsView";

const days = (value: string, max: number) => Math.max(0, Math.min(max, Math.round(Number(value) || 0)));

/** Everything about one project that is not daily work, as a view of its own: found in the
 * navigation, one section per subject, every setting with its name and what it does. */
export function ProjectSettings({ onOpenTeam, onDeleted }: { onOpenTeam: () => void; onDeleted: () => void }) {
  const { t } = useTranslation();
  const { project, isOwner, canWrite } = useShell();
  const qc = useQueryClient();
  const [current, setCurrent] = useStored<string>("plainboard.projectSection", "general");
  const settings = useSettings();
  const accounts = useAccounts();
  const usableIds = useUsableAccounts(project.id);
  const repos = useRepositories(project.id);
  const meta = useMeta();
  const agents = useAgents(project.id);
  const defaultParallel = Number(settings.data?.values.max_parallel_runs ?? 3);
  const [form, setForm] = useState({
    name: project.name,
    description: project.description,
    language: project.language ?? "",
    workspace_path: project.workspace_path ?? "",
    github_token: "",
    auto_wake_manager: project.auto_wake_manager,
    continue_with_backlog: project.continue_with_backlog,
    parallel_agents: project.parallel_agents,
    account_id: project.account_id,
    account_ids: project.account_ids ?? [],
    auto_archive_days: project.auto_archive_days,
    review_days: project.review_days,
    attachment_retention_days: project.attachment_retention_days ?? null,
    attachment_retention_min_mb: project.attachment_retention_min_mb ?? null,
    ask_provider: project.ask_provider ?? "",
    ask_model: project.ask_model ?? "",
    ask_effort: project.ask_effort ?? "",
    idea_provider: project.idea_provider ?? "",
    idea_model: project.idea_model ?? "",
    idea_effort: project.idea_effort ?? "",
    cleanup_provider: project.cleanup_provider ?? "",
    cleanup_model: project.cleanup_model ?? "",
  });
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));
  const stored: Record<string, unknown> = {
    ...project,
    attachment_retention_days: project.attachment_retention_days ?? null,
    attachment_retention_min_mb: project.attachment_retention_min_mb ?? null,
    workspace_path: project.workspace_path ?? "", github_token: "" };
  const dirty = (...keys: (keyof typeof form)[]) => keys.some((k) => form[k] !== stored[k]);
  const save = async (...keys: (keyof typeof form)[]) => {
    const body: Record<string, unknown> = Object.fromEntries(keys.map((k) => [k, form[k]]));
    if ("workspace_path" in body) body.workspace_path = form.workspace_path.trim() || null;
    if ("github_token" in body && !form.github_token) delete body.github_token;
    await patchProject(qc, project.id, body);
    set({ github_token: "" });
  };
  // Which subscriptions the project may use is an admin's call and has a route of its own, but it is
  // one form: stored with the same button as the fields around it, and first, because the
  // subscription chosen below it has to be one of them.
  const accountsDirty = !sameAccounts(form.account_ids, project.account_ids ?? []);
  const saveTeam = async () => {
    if (accountsDirty) {
      await api.put(`/projects/${project.id}/accounts`, { account_ids: form.account_ids });
      await qc.invalidateQueries({ queryKey: ["project-accounts", project.id] });
    }
    await save("parallel_agents", "account_id", "auto_wake_manager", "continue_with_backlog", "ask_provider", "ask_model", "ask_effort", "idea_provider", "idea_model", "idea_effort", "cleanup_provider", "cleanup_model");
  };
  // Quick questions: the provider's small model with effort high, unless chosen here.
  const providers = meta.data?.providers ?? [];
  const askProvider = providers.find((p) => p.name === (form.ask_provider || "claude_code"));
  const askModels = askProvider?.models ?? [];
  const askSmall = askModels.filter((m) => m.tier === "small");
  const askDefault = askSmall.find((m) => m.id.includes("haiku")) ?? askSmall[0] ?? askModels[askModels.length - 1];
  const askEfforts = askProvider?.efforts ?? [];
  // Deleting files is never a surprise: before a retention is switched on or changed, it says what would go, and is saved only after that.
  const [retention, setRetention] = useState<RetentionPreview | null>(null);
  const [retentionBusy, setRetentionBusy] = useState(false);
  const [retentionError, setRetentionError] = useState<unknown>(null);
  // null: the PlainBoard default, which the preview then shows for this project.
  const defaultDays = Number(settings.data?.values.attachment_retention_days ?? 0);
  const defaultMinMb = Number(settings.data?.values.attachment_retention_min_mb ?? 0);
  const useDefault = form.attachment_retention_days === null;
  const ownDays = form.attachment_retention_days ?? 0;
  const saveRetention = async () => {
    const [effDays, effMinMb] = useDefault ? [defaultDays, defaultMinMb] : [ownDays, form.attachment_retention_min_mb ?? 0];
    if (effDays > 0) {
      const q = `days=${effDays}&min_mb=${effMinMb}`;
      setRetention(await api.get<RetentionPreview>(`/projects/${project.id}/attachment-retention?${q}`));
      return; // saved when confirmed
    }
    await save("attachment_retention_days", "attachment_retention_min_mb");
  };
  const confirmRetention = async () => {
    setRetentionBusy(true);
    setRetentionError(null);
    try {
      await save("attachment_retention_days", "attachment_retention_min_mb");
      setRetention(null);
    } catch (err) {
      setRetentionError(err);
    } finally {
      setRetentionBusy(false);
    }
  };
  const ideaProvider = providers.find((p) => p.name === (form.idea_provider || "claude_code"));
  const ideaModels = ideaProvider?.models ?? [];
  const ideaSmall = ideaModels.filter((m) => m.tier === "small");
  const ideaDefault = ideaSmall.find((m) => m.id.includes("haiku")) ?? ideaSmall[0] ?? ideaModels[ideaModels.length - 1];
  const ideaEfforts = ideaProvider?.efforts ?? [];
  // Cleanup runs: empty is the small model of the manager's provider, said by name; a provider the project may use
  // no subscription of is shown as an error (the run never switches to another one).
  const managerProvider = agents.data?.find((a) => a.is_manager)?.provider || "claude_code";
  const cleanupProviderName = form.cleanup_provider || managerProvider;
  const cleanupProvider = providers.find((p) => p.name === cleanupProviderName);
  const cleanupModels = cleanupProvider?.models ?? [];
  const cleanupSmall = cleanupModels.filter((m) => m.tier === "small");
  const cleanupDefault = cleanupSmall.find((m) => m.id.includes("haiku")) ?? cleanupSmall[0] ?? cleanupModels[cleanupModels.length - 1];
  const ofCleanupProvider = (accounts.data ?? []).filter((a) => a.provider === cleanupProviderName);
  const cleanupBlocked = form.account_ids.length > 0 && ofCleanupProvider.length > 0 && !ofCleanupProvider.some((a) => form.account_ids.includes(a.id));
  const refreshRepos = () => qc.invalidateQueries({ queryKey: qk.repos(project.id) });
  // Deleting asks first, and asks what happens to the folders the agents worked in.
  const [deleting, setDeleting] = useState(false);
  const [withFiles, setWithFiles] = useState(true);
  const [removing, setRemoving] = useState(false);
  const remove = async () => {
    setError(null);
    setRemoving(true);
    try {
      await api.del(`/projects/${project.id}${withFiles && !project.workspace_path ? "?files=true" : ""}`);
      await qc.invalidateQueries({ queryKey: qk.projects });
      onDeleted();
    } catch (err) {
      setError(err);
    } finally {
      setRemoving(false);
    }
  };

  if (!isOwner) {
    return (
      <div className="flex h-full flex-col">
        <PageHead title={t("projectSettings.title")} />
        <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-4">
          <p className="text-fg-2">{t("projectSettings.ownersOnly")}</p>
          {canWrite && (
            <section className="max-w-2xl space-y-3">
              <div>
                <h2 className="font-semibold">{t("givenAccounts.label")}</h2>
                <p className="text-fg-3">{t("givenAccounts.description")}</p>
              </div>
              <GivenAccounts projectId={project.id} isOwner={false} />
            </section>
          )}
          <section className="max-w-2xl space-y-3">
            <div>
              <h2 className="font-semibold">{t("tools.title")}</h2>
              <p className="text-fg-3">{t("tools.subtitle")}</p>
            </div>
            <ToolsView embedded />
          </section>
        </div>
      </div>
    );
  }

  const sections: SettingsSection[] = [
    {
      id: "general",
      label: t("projectSettings.general.label"),
      description: t("projectSettings.general.description"),
      content: (
        <div className="space-y-4">
          <Field label={t("projects.name")}>
            <Input value={form.name} onChange={(e) => set({ name: e.target.value })} />
          </Field>
          <Field label={t("projects.description")} hint={t("projects.descriptionHint")}>
            <Textarea rows={7} value={form.description} onChange={(e) => set({ description: e.target.value })} />
          </Field>
          <Field label={t("projects.language")} hint={t("projects.languageHint")}>
            <Input className="sm:w-64" placeholder={t("projects.languagePlaceholder")} value={form.language} onChange={(e) => set({ language: e.target.value })} />
          </Field>
          <SaveRow dirty={dirty("name", "description", "language") && form.name.trim() !== ""} onSave={() => save("name", "description", "language")} />
        </div>
      ),
    },
    {
      id: "repositories",
      label: t("projectSettings.repositories.label"),
      description: t("projectSettings.repositories.description"),
      content: (
        <div className="space-y-4">
          <RepositoriesEditor
            repos={(repos.data ?? []).map(toDraft)}
            projectId={project.id}
            onAdd={async (d) => {
              await api.post(`/projects/${project.id}/repositories`, toBody(d));
              refreshRepos();
            }}
            onSave={async (d) => {
              await api.patch(`/repositories/${d.key}`, { ...toBody(d), new_ssh_key: d.new_ssh_key });
              refreshRepos();
            }}
            onRemove={async (d) => {
              await api.del(`/repositories/${d.key}`);
              refreshRepos();
            }}
          />
          {repos.data?.length === 0 && <p className="text-fg-3">{t("projectSettings.repositories.none")}</p>}
          {(repos.data?.length ?? 0) > 0 && (
            <div className="flex flex-wrap items-center gap-3">
              <Button
                size="sm"
                onClick={async () => {
                  setCheck({ ok: true, detail: t("settings.testing") });
                  try {
                    setCheck(await checks.projectGithub(project.id));
                  } catch (err) {
                    setCheck({ ok: false, detail: errorMessage(err) });
                  }
                }}
              >
                {t("projectSettings.repositories.test")}
              </Button>
              <CheckBadge result={check} />
            </div>
          )}
          <div className="space-y-1">
            <Toggle
              checked={project.fast_checks}
              onChange={(v) => patchProject(qc, project.id, { fast_checks: v })}
              label={t("projects.fastChecks")}
            />
            <p className="text-fg-3">{t("projects.fastChecksHint")}</p>
          </div>
          <SavedAtOnce />
        </div>
      ),
    },
    {
      id: "team",
      label: t("projectSettings.team.label"),
      description: t("projectSettings.team.description"),
      content: (
        <div className="space-y-4">
          <ProjectPauseSwitch project={project} />
          <div className="space-y-1">
            <Toggle
              checked={project.limit_pause}
              onChange={(v) => patchProject(qc, project.id, { limit_pause: v })}
              label={t("projects.limitPause")}
            />
            <p className="text-fg-3">{t("projects.limitPauseHint")}</p>
          </div>
          <Field label={t("projects.parallel")} hint={t("projects.parallelHint", { count: defaultParallel })}>
            <Input
              type="number"
              min={0}
              max={20}
              className="w-28"
              value={form.parallel_agents ?? ""}
              placeholder={String(defaultParallel)}
              onChange={(e) => set({ parallel_agents: e.target.value === "" ? null : days(e.target.value, 20) })}
            />
          </Field>
          <Field label={t("newProject.team.subscription")} hint={t("newProject.team.subscriptionHint")}>
            <Select value={form.account_id ?? ""} onChange={(e) => set({ account_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">{t("newProject.team.subscriptionAuto")}</option>
              {allowedAccounts(form, accounts.data ?? [], usableIds)
                .filter((a) => a.enabled)
                .map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.label}
                  </option>
                ))}
            </Select>
          </Field>
          <ProjectAccounts
            value={form.account_ids}
            onChange={(ids) => set({ account_ids: ids, account_id: ids.length && form.account_id !== null && !ids.includes(form.account_id) ? null : form.account_id })}
          />
          <div className="space-y-1">
            <Toggle checked={form.auto_wake_manager} onChange={(v) => set({ auto_wake_manager: v })} label={t("projects.autoWake")} />
            <p className="text-fg-3">{t("projects.autoWakeHint")}</p>
          </div>
          <div className="space-y-1">
            <Toggle checked={form.continue_with_backlog} onChange={(v) => set({ continue_with_backlog: v })} label={t("projects.backlogNext")} />
            <p className="text-fg-3">{t("projects.backlogNextHint")}</p>
          </div>
          <Field group label={t("projects.askModel")} hint={t("projects.askModelHint")}>
            <div data-part="ask-model" className="flex flex-wrap items-start gap-2">
              {providers.length > 1 && (
                <div className="w-full sm:w-44">
                  <Select
                    aria-label={t("projects.askProvider")}
                    value={form.ask_provider || "claude_code"}
                    onChange={(e) => set({ ask_provider: e.target.value === "claude_code" ? "" : e.target.value, ask_model: "", ask_effort: "" })}
                  >
                    {providers.map((p) => (
                      <option key={p.name} value={p.name}>
                        {p.label}
                      </option>
                    ))}
                  </Select>
                </div>
              )}
              <div className="min-w-0 flex-1 basis-48">
                <Select aria-label={t("projects.askModel")} value={form.ask_model} onChange={(e) => set({ ask_model: e.target.value })}>
                  <option value="">{t("projects.askModelSmall", { model: askDefault?.label ?? "Claude Haiku" })}</option>
                  {askModels.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                  {form.ask_model && !askModels.some((m) => m.id === form.ask_model) && <option value={form.ask_model}>{form.ask_model}</option>}
                </Select>
              </div>
              <div className="w-full sm:w-48">
                <Select aria-label={t("settings.defaultEffort")} value={form.ask_effort} onChange={(e) => set({ ask_effort: e.target.value })}>
                  <option value="">{askEfforts.includes("high") || !askProvider ? t("projects.askEffortDefault") : t("settings.effortOwn")}</option>
                  {askEfforts.map((e) => (
                    <option key={e} value={e}>
                      {t("settings.effortOption", { effort: e })}
                    </option>
                  ))}
                </Select>
              </div>
            </div>
          </Field>
          <Field group label={t("projects.ideaModel")} hint={t("projects.ideaModelHint")}>
            <div data-part="idea-model" className="flex flex-wrap items-start gap-2">
              {providers.length > 1 && (
                <div className="w-full sm:w-44">
                  <Select
                    aria-label={t("projects.ideaProvider")}
                    value={form.idea_provider || "claude_code"}
                    onChange={(e) => set({ idea_provider: e.target.value === "claude_code" ? "" : e.target.value, idea_model: "", idea_effort: "" })}
                  >
                    {providers.map((p) => (
                      <option key={p.name} value={p.name}>
                        {p.label}
                      </option>
                    ))}
                  </Select>
                </div>
              )}
              <div className="min-w-0 flex-1 basis-48">
                <Select aria-label={t("projects.ideaModel")} value={form.idea_model} onChange={(e) => set({ idea_model: e.target.value })}>
                  <option value="">{t("projects.askModelSmall", { model: ideaDefault?.label ?? "Claude Haiku" })}</option>
                  {ideaModels.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                  {form.idea_model && !ideaModels.some((m) => m.id === form.idea_model) && <option value={form.idea_model}>{form.idea_model}</option>}
                </Select>
              </div>
              <div className="w-full sm:w-48">
                <Select aria-label={t("settings.defaultEffort")} value={form.idea_effort} onChange={(e) => set({ idea_effort: e.target.value })}>
                  <option value="">{t("settings.effortOwn")}</option>
                  {ideaEfforts.map((e) => (
                    <option key={e} value={e}>
                      {t("settings.effortOption", { effort: e })}
                    </option>
                  ))}
                </Select>
              </div>
            </div>
          </Field>
          <Field group label={t("projects.cleanupModel")} hint={t("projects.cleanupModelHint")}>
            <div data-part="cleanup-model" className="flex flex-wrap items-start gap-2">
              {providers.length > 1 && (
                <div className="w-full sm:w-56">
                  <Select
                    aria-label={t("projects.cleanupProvider")}
                    value={form.cleanup_provider}
                    onChange={(e) => set({ cleanup_provider: e.target.value, cleanup_model: "" })}
                  >
                    <option value="">{t("projects.cleanupProviderManager", { provider: cleanupProvider?.label ?? managerProvider })}</option>
                    {providers.map((p) => (
                      <option key={p.name} value={p.name}>
                        {p.label}
                      </option>
                    ))}
                  </Select>
                </div>
              )}
              <div className="min-w-0 flex-1 basis-48">
                <Select aria-label={t("projects.cleanupModel")} value={form.cleanup_model} onChange={(e) => set({ cleanup_model: e.target.value })}>
                  <option value="">{t("projects.askModelSmall", { model: cleanupDefault?.label ?? cleanupDefault?.id ?? "Claude Haiku" })}</option>
                  {cleanupModels.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                  {form.cleanup_model && !cleanupModels.some((m) => m.id === form.cleanup_model) && <option value={form.cleanup_model}>{form.cleanup_model}</option>}
                </Select>
              </div>
            </div>
            {cleanupBlocked && <ErrorText error={t("projects.cleanupBlocked", { provider: cleanupProvider?.label ?? cleanupProviderName })} />}
          </Field>
          <SaveRow
            dirty={accountsDirty || dirty("parallel_agents", "account_id", "auto_wake_manager", "continue_with_backlog", "ask_provider", "ask_model", "ask_effort", "idea_provider", "idea_model", "idea_effort", "cleanup_provider", "cleanup_model")}
            onSave={saveTeam}
          >
            <Button variant="ghost" onClick={onOpenTeam}>
              {t("projectSettings.team.open")}
            </Button>
          </SaveRow>
          <div className="space-y-1">
            {meta.data?.semantic_search ? (
              <Toggle
                checked={project.semantic_search}
                onChange={(v) => patchProject(qc, project.id, { semantic_search: v })}
                label={t("projects.semanticSearch")}
              />
            ) : (
              <h3 className="font-semibold">{t("projects.semanticSearch")}</h3>
            )}
            <p className="text-fg-3">{t(meta.data?.semantic_search ? "projects.semanticSearchHint" : "search.unavailable")}</p>
          </div>
        </div>
      ),
    },
    {
      id: "board",
      label: t("projectSettings.board.label"),
      description: t("projectSettings.board.description"),
      content: (
        <div className="space-y-4">
          <Field label={t("projects.reviewDays")} hint={t("projects.reviewDaysHint")}>
            <Input type="number" min={0} max={365} className="w-28" value={form.review_days} onChange={(e) => set({ review_days: days(e.target.value, 365) })} />
          </Field>
          <Field label={t("projects.autoArchive")} hint={t("projects.autoArchiveHint")}>
            <Input type="number" min={0} max={3650} className="w-28" value={form.auto_archive_days} onChange={(e) => set({ auto_archive_days: days(e.target.value, 3650) })} />
          </Field>
          <SaveRow dirty={dirty("review_days", "auto_archive_days")} onSave={() => save("review_days", "auto_archive_days")} />
          <div className="space-y-3 pt-2">
            <Toggle
              checked={useDefault}
              onChange={(v) => set(v ? { attachment_retention_days: null, attachment_retention_min_mb: null } : { attachment_retention_days: defaultDays, attachment_retention_min_mb: defaultMinMb })}
              label={<span className="font-semibold">{t("projects.retentionUseDefault", { current: retentionText(t, defaultDays, defaultMinMb) })}</span>}
            />
            <p className="text-fg-3">{t("projects.retentionUseDefaultHint")}</p>
            {!useDefault && (
              <Toggle
                checked={ownDays > 0}
                onChange={(v) => set({ attachment_retention_days: v ? project.attachment_retention_days || defaultDays || 90 : 0, attachment_retention_min_mb: form.attachment_retention_min_mb ?? 0 })}
                label={<span className="font-semibold">{t("projects.retention")}</span>}
              />
            )}
            {!useDefault && <p className="text-fg-3">{t("projects.retentionHint")}</p>}
            {!useDefault && ownDays > 0 && (
              <div className="flex flex-wrap gap-4">
                <Field label={t("projects.retentionDays")}>
                  <Input
                    type="number"
                    min={1}
                    max={3650}
                    className="w-28"
                    value={ownDays}
                    onChange={(e) => set({ attachment_retention_days: Math.max(1, days(e.target.value, 3650)) })}
                  />
                </Field>
                <Field label={t("projects.retentionMinMb")}>
                  <Input
                    type="number"
                    min={0}
                    max={100000}
                    className="w-28"
                    value={form.attachment_retention_min_mb ?? 0}
                    onChange={(e) => set({ attachment_retention_min_mb: days(e.target.value, 100000) })}
                  />
                </Field>
              </div>
            )}
            <SaveRow dirty={dirty("attachment_retention_days", "attachment_retention_min_mb")} onSave={saveRetention} />
          </div>
          <RetentionConfirm preview={retention} busy={retentionBusy} error={retentionError} onCancel={() => setRetention(null)} onConfirm={confirmRetention} />
        </div>
      ),
    },
    {
      id: "secrets",
      label: t("projectSettings.secrets.label"),
      description: t("projectSettings.secrets.description"),
      content: (
        <div className="space-y-4">
          <SecretsEditor projectId={project.id} />
          <SavedAtOnce />
        </div>
      ),
    },
    {
      id: "skills",
      label: t("projectSettings.skills.label"),
      description: t("projectSettings.skills.description"),
      content: (
        <div className="space-y-4">
          <SkillsEditor projectId={project.id} />
          <SavedAtOnce />
        </div>
      ),
    },
    {
      id: "givenAccounts",
      label: t("givenAccounts.label"),
      description: t("givenAccounts.description"),
      content: (
        <div className="space-y-4">
          <GivenAccounts projectId={project.id} isOwner />
          <SavedAtOnce />
        </div>
      ),
    },
    {
      id: "members",
      label: t("projectSettings.members.label"),
      description: t("projectSettings.members.description"),
      content: <MembersEditor projectId={project.id} />,
    },
    {
      id: "customerLanguages",
      label: t("customerLanguages.label"),
      description: t("customerLanguages.description"),
      content: <CustomerLanguages project={project} />,
    },
    {
      id: "publicApi",
      label: t("projectSettings.publicApi.label"),
      description: t("projects.publicApiHint"),
      content: (
        <div className="space-y-4">
          <ApiKeyEditor path={`/projects/${project.id}/public-api-keys`} revokePath="/public-api-keys" placeholder={t("projects.publicApiLabel")} />
          <SavedAtOnce />
        </div>
      ),
    },
    { id: "tools", label: t("tools.title"), description: t("tools.subtitle"), content: <ToolsView embedded /> },
    { id: "runner", label: t("projectRunner.title"), description: t("projectRunner.description"), content: <ProjectRunnerEditor projectId={project.id} isOwner={isOwner} /> },
    { id: "move", label: t("move.label"), description: t("move.description"), content: <ExportProject project={project} /> },
    {
      id: "advanced",
      label: t("projectSettings.advanced.label"),
      description: t("projectSettings.advanced.description"),
      content: (
        <div className="space-y-4">
          <Field group label={t("projects.githubToken")} hint={project.has_github_token ? t("projects.githubTokenSet") : t("projects.githubTokenHint")}>
            <GithubTokenField label={t("projects.githubToken")} projectId={project.id} value={form.github_token} onChange={(v) => set({ github_token: v })} />
          </Field>
          <Field label={t("projects.localFolder")} hint={t("projects.localFolderHint")}>
            <Input placeholder="my-app" value={form.workspace_path} onChange={(e) => set({ workspace_path: e.target.value })} />
          </Field>
          <SaveRow dirty={dirty("workspace_path") || form.github_token !== ""} onSave={() => save("workspace_path", "github_token")} />
          <div className="space-y-2 pt-4">
            <h3 className="font-semibold">{t("projectSettings.advanced.delete")}</h3>
            <p className="text-fg-3">{t("projectSettings.advanced.deleteHint")}</p>
            <Button variant="danger" onClick={() => setDeleting(true)}>
              {t("projectSettings.advanced.deleteAction", { name: project.name })}
            </Button>
          </div>
          <Modal
            open={deleting}
            onClose={() => !removing && setDeleting(false)} // on its way: the window stays until it is over
            width="max-w-lg"
            title={t("projectSettings.advanced.deleteTitle", { name: project.name })}
            footer={
              <>
                <Button variant="ghost" disabled={removing} onClick={() => setDeleting(false)}>
                  {t("common.cancel")}
                </Button>
                <Button variant="danger" loading={removing} onClick={remove}>
                  {t("projectSettings.advanced.deleteAction", { name: project.name })}
                </Button>
              </>
            }
          >
            <div className="space-y-4">
              <p>{t("projectSettings.advanced.deleteHint")}</p>
              {project.workspace_path ? (
                <p className="text-fg-3">{t("projectSettings.advanced.folderStays", { folder: project.workspace_path })}</p>
              ) : (
                <div className="space-y-1">
                  <Toggle checked={withFiles} onChange={(v) => !removing && setWithFiles(v)} label={t("projectSettings.advanced.deleteFiles")} />
                  <p className="text-fg-3">{t("projectSettings.advanced.deleteFilesHint")}</p>
                </div>
              )}
              {removing && <p className="text-fg-2">{t("projectSettings.advanced.deletingNote")}</p>}
              <ErrorText error={error} />
            </div>
          </Modal>
        </div>
      ),
    },
  ];

  return (
    <div className="flex h-full flex-col">
      <PageHead title={t("projectSettings.title")} />
      <SettingsLayout label={t("projectSettings.title")} sections={sections} current={current} onChange={setCurrent} />
    </div>
  );
}
