import { GitBranch, ListChecks, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { errorMessage } from "../../api/client";
import { Button, IconButton, Input } from "../../components/ui";
import { GitHubRepoPicker } from "./GitHubRepoPicker";

export interface RepoDraft {
  key: string | number;
  name: string;
  url: string;
  default_branch: string;
  description: string;
}

export const emptyRepo = (): RepoDraft => ({ key: crypto.randomUUID(), name: "", url: "", default_branch: "", description: "" });

/** Editable list of repositories. Persistence is up to the caller (API or local draft list). */
export function RepositoriesEditor({
  repos,
  projectId,
  onAdd,
  onSave,
  onRemove,
}: {
  repos: RepoDraft[];
  projectId?: number;
  onAdd: (draft: RepoDraft) => Promise<void>;
  onSave: (draft: RepoDraft) => Promise<void>;
  onRemove: (draft: RepoDraft) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<RepoDraft | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  const submit = async () => {
    if (!editing?.url.trim()) return;
    setError(null);
    try {
      await (isNew ? onAdd(editing) : onSave(editing));
      setEditing(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <div className="space-y-2">
      {repos.length > 0 && (
        <ul className="divide-y divide-zinc-100 rounded-lg ring-1 ring-zinc-200 dark:divide-zinc-800 dark:ring-zinc-800">
          {repos.map((repo) => (
            <li key={repo.key} className="flex items-center gap-2 px-3 py-2">
              <GitBranch className="size-3.5 shrink-0 text-zinc-400" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">
                  {repo.name || t("projects.repoAutoName")}
                  {repo.default_branch && <span className="ml-1.5 text-xs font-normal text-zinc-400">@{repo.default_branch}</span>}
                </div>
                <div className="truncate font-mono text-xs text-zinc-500">{repo.url}</div>
              </div>
              <IconButton
                label={t("common.edit")}
                onClick={() => {
                  setIsNew(false);
                  setEditing({ ...repo });
                }}
              >
                <Pencil className="size-3.5" />
              </IconButton>
              <IconButton
                label={t("common.delete")}
                onClick={async () => {
                  if (!confirm(t("projects.confirmRemoveRepo", { name: repo.name || repo.url }))) return;
                  try {
                    await onRemove(repo);
                  } catch (err) {
                    setError(errorMessage(err));
                  }
                }}
              >
                <Trash2 className="size-3.5" />
              </IconButton>
            </li>
          ))}
        </ul>
      )}
      {editing ? (
        <div className="space-y-2 rounded-lg bg-zinc-50 p-3 dark:bg-zinc-800/40">
          <Input autoFocus placeholder="https://github.com/owner/repo.git" value={editing.url} onChange={(e) => setEditing({ ...editing, url: e.target.value })} className="font-mono" />
          <div className="grid gap-2 sm:grid-cols-2">
            <Input placeholder={t("projects.repoNamePlaceholder")} value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            <Input placeholder={t("projects.repoBranchPlaceholder")} value={editing.default_branch} onChange={(e) => setEditing({ ...editing, default_branch: e.target.value })} />
          </div>
          <Input placeholder={t("projects.repoDescriptionPlaceholder")} value={editing.description} onChange={(e) => setEditing({ ...editing, description: e.target.value })} />
          <div className="flex gap-2">
            <Button size="sm" variant="primary" onClick={submit} disabled={!editing.url.trim()}>
              {isNew ? t("projects.addRepo") : t("common.save")}
            </Button>
            <Button size="sm" onClick={() => setEditing(null)}>
              {t("common.cancel")}
            </Button>
          </div>
        </div>
      ) : picking ? (
        <GitHubRepoPicker
          projectId={projectId}
          existing={repos}
          onClose={() => setPicking(false)}
          onPick={async (drafts) => {
            setError(null);
            try {
              for (const draft of drafts) await onAdd(draft);
            } catch (err) {
              setError(errorMessage(err));
            }
          }}
        />
      ) : (
        <div className="flex flex-wrap gap-1">
          <Button size="sm" variant="ghost" icon={<ListChecks className="size-3.5" />} onClick={() => setPicking(true)}>
            {t("projects.pickFromGithub")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            icon={<Plus className="size-3.5" />}
            onClick={() => {
              setIsNew(true);
              setEditing(emptyRepo());
            }}
          >
            {t("projects.addRepoByUrl")}
          </Button>
        </div>
      )}
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}
