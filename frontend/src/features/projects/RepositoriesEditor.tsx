import { Check, Copy, GitBranch, KeyRound, ListChecks, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { errorMessage } from "../../api/client";
import type { RepoAuth, Repository } from "../../api/types";
import { Button, Field, IconButton, Input, Segmented, Select, Textarea, Toggle } from "../../components/ui";
import { GitHubRepoPicker } from "./GitHubRepoPicker";

export interface RepoDraft {
  key: string | number;
  name: string;
  url: string;
  default_branch: string;
  description: string;
  check_command: string;
  quick_check_command: string;
  land_first: boolean;
  tidy_branches: boolean;
  auth: RepoAuth;
  token: string; // write-only: empty keeps the stored one
  token_user: string;
  has_token: boolean;
  ssh_private_key: string; // write-only: one's own key instead of a created one
  ssh_public_key: string;
  new_ssh_key: boolean;
  red_base?: string | null; // read-only: the base branch, while its checks fail
}

// Not crypto.randomUUID: browsers only have it over HTTPS or on localhost.
let drafts = 0;

export const emptyRepo = (): RepoDraft => ({
  key: `new-${++drafts}`,
  name: "",
  url: "",
  default_branch: "",
  description: "",
  check_command: "",
  quick_check_command: "",
  land_first: false,
  tidy_branches: false,
  auth: "default",
  token: "",
  token_user: "",
  has_token: false,
  ssh_private_key: "",
  ssh_public_key: "",
  new_ssh_key: false,
});

export const toDraft = (r: Repository): RepoDraft => ({
  key: r.id,
  name: r.name,
  url: r.url,
  default_branch: r.default_branch ?? "",
  description: r.description,
  check_command: r.check_command ?? "",
  quick_check_command: r.quick_check_command ?? "",
  land_first: r.land_first ?? false,
  tidy_branches: r.tidy_branches ?? false,
  red_base: r.red_base ?? null,
  auth: r.auth,
  token: "",
  token_user: r.token_user,
  has_token: r.has_token,
  ssh_private_key: "",
  ssh_public_key: r.ssh_public_key,
  new_ssh_key: false,
});

// Secrets are write-only: an empty field keeps what is stored.
export const toBody = (d: RepoDraft) => ({
  url: d.url.trim(),
  name: d.name.trim() || null,
  default_branch: d.default_branch.trim() || null,
  description: d.description,
  check_command: d.check_command.trim(),
  quick_check_command: d.quick_check_command.trim(),
  land_first: d.land_first,
  tidy_branches: d.tidy_branches,
  auth: d.auth,
  ...(d.auth === "token" ? { token_user: d.token_user.trim(), ...(d.token ? { token: d.token } : {}) } : {}),
  ...(d.auth === "ssh" && d.ssh_private_key.trim() ? { ssh_private_key: d.ssh_private_key } : {}),
});

const AUTHS: { value: RepoAuth; label: string; hint: string }[] = [
  { value: "default", label: "projects.repoAccessDefault", hint: "projects.repoAccessDefaultHint" },
  { value: "token", label: "projects.repoAccessToken", hint: "projects.repoAccessTokenHint" },
  { value: "ssh", label: "projects.repoAccessSsh", hint: "projects.repoAccessSshHint" },
];

/** How git reaches one repository: the general token, its own token, or its own SSH key. */
function AccessFields({ draft, onChange }: { draft: RepoDraft; onChange: (patch: Partial<RepoDraft>) => void }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(draft.ssh_public_key);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="space-y-2">
      <Field label={t("projects.repoAccess")} hint={t(AUTHS.find((a) => a.value === draft.auth)!.hint)}>
        <Select value={draft.auth} onChange={(e) => onChange({ auth: e.target.value as RepoAuth })}>
          {AUTHS.map((a) => (
            <option key={a.value} value={a.value}>
              {t(a.label)}
            </option>
          ))}
        </Select>
      </Field>
      {draft.auth === "token" && (
        <div className="grid gap-2 sm:grid-cols-2">
          <Input
            type="password"
            autoComplete="off"
            aria-label={t("projects.repoToken")}
            placeholder={draft.has_token ? t("projects.repoTokenStored") : t("projects.repoToken")}
            value={draft.token}
            onChange={(e) => onChange({ token: e.target.value })}
          />
          <Input autoComplete="off" placeholder={t("projects.repoTokenUser")} value={draft.token_user} onChange={(e) => onChange({ token_user: e.target.value })} />
        </div>
      )}
      {draft.auth === "ssh" && (
        <>
          {draft.ssh_public_key ? (
            <Field group label={t("projects.repoSshPublicKey")} hint={t("projects.repoSshPublicKeyHint")}>
              <div className="flex items-start gap-2">
                <code className="min-w-0 flex-1 rounded-sm bg-bg px-2 py-1.5 font-mono break-all ring-1 ring-line">
                  {draft.ssh_public_key}
                </code>
                <Button size="sm" icon={copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />} onClick={copy}>
                  {copied ? t("projects.repoSshCopied") : t("projects.repoSshCopy")}
                </Button>
              </div>
            </Field>
          ) : (
            <p className="text-fg-3">{t("projects.repoSshOnSave")}</p>
          )}
          {draft.ssh_public_key && (
            <label className="flex items-center gap-2 text-fg-2">
              <input type="checkbox" checked={draft.new_ssh_key} onChange={(e) => onChange({ new_ssh_key: e.target.checked })} />
              {t("projects.repoSshNew")}
            </label>
          )}
          <details className="text-fg-3">
            <summary className="cursor-pointer">{t("projects.repoSshOwn")}</summary>
            <Textarea
              rows={3}
              className="mt-2 font-mono"
              autoComplete="off"
              spellCheck={false}
              aria-label={t("projects.repoSshOwn")}
              placeholder={t("projects.repoSshOwnPlaceholder")}
              value={draft.ssh_private_key}
              onChange={(e) => onChange({ ssh_private_key: e.target.value })}
            />
          </details>
        </>
      )}
    </div>
  );
}

/** Editable list of repositories. Persistence is up to the caller (API or local draft list). */
export function RepositoriesEditor({
  repos,
  projectId,
  onAdd,
  onSave,
  onRemove,
  beforeGithub,
}: {
  repos: RepoDraft[];
  projectId?: number;
  /** Runs before the list from GitHub opens: e.g. stores a token typed beside it, so the list is that token's. */
  beforeGithub?: () => Promise<void>;
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
        <ul className="divide-y divide-line rounded-sm ring-1 ring-line">
          {repos.map((repo) => (
            <li key={repo.key} className="flex items-center gap-2 px-3 py-2 hover:bg-sunk">
              <GitBranch className="size-3.5 shrink-0 text-fg-3" />
              <button
                type="button"
                className="min-w-0 flex-1 text-left"
                onClick={() => {
                  setIsNew(false);
                  setEditing({ ...repo });
                }}
              >
                <div className="truncate font-medium">
                  {repo.name || t("projects.repoAutoName")}
                  {repo.default_branch && <span className="ml-1.5 font-normal text-fg-3">@{repo.default_branch}</span>}
                  {repo.auth !== "default" && (
                    <span className="ml-1.5 font-normal text-fg-3">· {t(repo.auth === "ssh" ? "projects.repoAccessBadgeSsh" : "projects.repoAccessBadgeToken")}</span>
                  )}
                  {repo.land_first && <span className="ml-1.5 font-normal text-fg-3">· {t("projects.repoLandFirstBadge")}</span>}
                  {repo.red_base && <span className="ml-1.5 text-you-fg">· {t("projects.repoBaseRed", { branch: repo.red_base })}</span>}
                </div>
                <div className="truncate font-mono text-fg-3">{repo.url}</div>
              </button>
              {repo.ssh_public_key && (
                <IconButton label={t("projects.repoSshCopyKey")} onClick={() => navigator.clipboard.writeText(repo.ssh_public_key)}>
                  <KeyRound className="size-3.5" />
                </IconButton>
              )}
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
        <div data-part="repo-form" className="space-y-5 rounded-sm bg-sunk p-3 sm:p-4">
          <div className="space-y-3">
            <h3 className="font-semibold">{isNew ? t("projects.repoNew") : editing.name || t("projects.repoEdit")}</h3>
            <Field label={t("projects.repoUrl")} hint={t("projects.repoUrlHint")}>
              <Input autoFocus placeholder="https://github.com/owner/repo.git" value={editing.url} onChange={(e) => setEditing({ ...editing, url: e.target.value })} className="font-mono" />
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label={t("projects.repoBranch")} hint={t("projects.repoBranchHint")}>
                <Input placeholder="main" value={editing.default_branch} onChange={(e) => setEditing({ ...editing, default_branch: e.target.value })} />
              </Field>
              <Field label={t("projects.repoName")} hint={t("projects.repoNameHint")}>
                <Input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
              </Field>
            </div>
            <Field label={t("projects.repoDescription")} hint={t("projects.repoDescriptionHint")}>
              <Input value={editing.description} onChange={(e) => setEditing({ ...editing, description: e.target.value })} />
            </Field>
          </div>

          <div className="space-y-3 border-t border-line pt-4">
            <div>
              <h3 className="font-semibold">{t("projects.repoLanding")}</h3>
              <p className="text-fg-3">{t("projects.repoLandingHint")}</p>
            </div>
            <Field group label={t("projects.repoLandWhen")} hint={t(editing.land_first ? "projects.repoLandFirstHint" : "projects.repoLandCheckedHint")}>
              <Segmented
                aria-label={t("projects.repoLandWhen")}
                className="max-w-full flex-wrap"
                value={editing.land_first ? "first" : "checked"}
                onChange={(v) => setEditing({ ...editing, land_first: v === "first" })}
                options={[
                  { value: "checked", label: t("projects.repoLandChecked") },
                  { value: "first", label: t("projects.repoLandFirst") },
                ]}
              />
            </Field>
            <Field label={t("projects.repoQuickCheck")} hint={t("projects.repoQuickCheckHint")}>
              <Input placeholder="cargo fmt --check && cargo clippy" value={editing.quick_check_command} onChange={(e) => setEditing({ ...editing, quick_check_command: e.target.value })} className="font-mono" />
            </Field>
            <Field label={t("projects.repoCheck")} hint={t("projects.repoCheckHint")}>
              <Input placeholder="./scripts/ci.sh" value={editing.check_command} onChange={(e) => setEditing({ ...editing, check_command: e.target.value })} className="font-mono" />
            </Field>
            <div className="space-y-1">
              <Toggle checked={editing.tidy_branches} onChange={(v) => setEditing({ ...editing, tidy_branches: v })} label={t("projects.repoTidy")} />
              <p className="text-fg-3">{t("projects.repoTidyHint")}</p>
            </div>
          </div>

          <div className="space-y-3 border-t border-line pt-4">
            <AccessFields draft={editing} onChange={(patch) => setEditing({ ...editing, ...patch })} />
          </div>

          <div className="flex gap-2 border-t border-line pt-4">
            <Button variant="primary" onClick={submit} disabled={!editing.url.trim()}>
              {isNew ? t("projects.addRepo") : t("projects.repoSave")}
            </Button>
            <Button onClick={() => setEditing(null)}>{t("common.cancel")}</Button>
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
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            icon={<Plus className="size-3.5" />}
            onClick={() => {
              setIsNew(true);
              setEditing(emptyRepo());
            }}
          >
            {t("projects.addRepoByUrl")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            icon={<ListChecks className="size-3.5" />}
            onClick={async () => {
              setError(null);
              try {
                await beforeGithub?.();
                setPicking(true);
              } catch (err) {
                setError(errorMessage(err));
              }
            }}
          >
            {t("projects.pickFromGithub")}
          </Button>
        </div>
      )}
      {error && <p className="text-you-fg">{error}</p>}
    </div>
  );
}
