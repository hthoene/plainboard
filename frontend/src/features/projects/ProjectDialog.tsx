import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { checks, qk, useRepositories } from "../../api/queries";
import type { CheckResult, Project, Repository } from "../../api/types";
import { Button, ErrorText, Field, Input, Modal, Textarea, Toggle } from "../../components/ui";
import { CheckBadge } from "../settings/SettingsDialog";
import { RepositoriesEditor, type RepoDraft } from "./RepositoriesEditor";

const toDraft = (r: Repository): RepoDraft => ({
  key: r.id,
  name: r.name,
  url: r.url,
  default_branch: r.default_branch ?? "",
  description: r.description,
});

const toBody = (d: RepoDraft) => ({ url: d.url.trim(), name: d.name.trim() || null, default_branch: d.default_branch.trim() || null, description: d.description });

export function ProjectDialog({
  project,
  onClose,
  onSaved,
  onDeleted,
}: {
  project?: Project;
  onClose: () => void;
  onSaved: (p: Project) => void;
  onDeleted: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [form, setForm] = useState({
    name: project?.name ?? "",
    description: project?.description ?? "",
    workspace_path: project?.workspace_path ?? "",
    github_token: "",
    auto_wake_manager: project?.auto_wake_manager ?? true,
  });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [check, setCheck] = useState<CheckResult | null>(null);
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));
  // Existing project: repositories are saved immediately. New project: collected and sent on create.
  const savedRepos = useRepositories(project?.id ?? 0);
  const [draftRepos, setDraftRepos] = useState<RepoDraft[]>([]);
  const repoList = project ? (savedRepos.data ?? []).map(toDraft) : draftRepos;
  const refreshRepos = () => project && qc.invalidateQueries({ queryKey: qk.repos(project.id) });

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = { ...form, workspace_path: form.workspace_path || null };
      if (!form.github_token) delete body.github_token;
      if (!project) body.repositories = draftRepos.map(toBody);
      const saved = project ? await api.patch<Project>(`/projects/${project.id}`, body) : await api.post<Project>("/projects", body);
      await qc.invalidateQueries({ queryKey: qk.projects });
      onSaved(saved);
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!project || !confirm(t("projects.confirmDelete", { name: project.name }))) return;
    try {
      await api.del(`/projects/${project.id}`);
      await qc.invalidateQueries({ queryKey: qk.projects });
      onDeleted();
      onClose();
    } catch (err) {
      setError(err);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={project ? t("projects.settings") : t("projects.new")}
      width="max-w-xl"
      footer={
        <>
          {project && (
            <Button variant="danger" className="mr-auto" onClick={remove}>
              {t("common.delete")}
            </Button>
          )}
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" onClick={save} loading={busy} disabled={!form.name.trim()}>
            {project ? t("common.save") : t("projects.create")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label={t("projects.name")}>
          <Input autoFocus value={form.name} onChange={(e) => set({ name: e.target.value })} />
        </Field>
        <Field label={t("projects.description")} hint={t("projects.descriptionHint")}>
          <Textarea rows={4} value={form.description} onChange={(e) => set({ description: e.target.value })} />
        </Field>
        <Field label={t("projects.localFolder")} hint={t("projects.localFolderHint")}>
          <Input placeholder="my-app" value={form.workspace_path} onChange={(e) => set({ workspace_path: e.target.value })} />
        </Field>
        <Field group label={t("projects.repositories")} hint={t("projects.repositoriesHint")}>
          <RepositoriesEditor
            repos={repoList}
            projectId={project?.id}
            onAdd={async (d) => {
              if (project) {
                await api.post(`/projects/${project.id}/repositories`, toBody(d));
                refreshRepos();
              } else setDraftRepos((list) => [...list, d]);
            }}
            onSave={async (d) => {
              if (project) {
                await api.patch(`/repositories/${d.key}`, toBody(d));
                refreshRepos();
              } else setDraftRepos((list) => list.map((x) => (x.key === d.key ? d : x)));
            }}
            onRemove={async (d) => {
              if (project) {
                await api.del(`/repositories/${d.key}`);
                refreshRepos();
              } else setDraftRepos((list) => list.filter((x) => x.key !== d.key));
            }}
          />
        </Field>
        <Field label={t("projects.githubToken")} hint={project?.has_github_token ? t("projects.githubTokenSet") : t("projects.githubTokenHint")}>
          <Input type="password" value={form.github_token} onChange={(e) => set({ github_token: e.target.value })} autoComplete="off" />
        </Field>
        {project && repoList.length > 0 && (
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              onClick={async () => {
                setCheck(null);
                try {
                  setCheck(await checks.projectGithub(project.id));
                } catch (err) {
                  setCheck({ ok: false, detail: errorMessage(err) });
                }
              }}
            >
              {t("settings.test")}
            </Button>
            <CheckBadge result={check} />
          </div>
        )}
        <Toggle checked={form.auto_wake_manager} onChange={(v) => set({ auto_wake_manager: v })} label={t("projects.autoWake")} />
        <p className="-mt-2 pl-11 text-xs text-zinc-500">{t("projects.autoWakeHint")}</p>
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}
