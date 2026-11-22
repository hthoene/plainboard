import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Crown, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useDirectory, useMe, useMembers } from "../../api/queries";
import type { Member, OwnerChange, ProjectRole } from "../../api/types";
import { Badge, Button, ErrorText, IconButton, Modal, Select } from "../../components/ui";

export const ROLES: ProjectRole[] = ["viewer", "member", "owner"];

/** Who has a part in a project, and which. Saved right away. Owners only. */
export function MembersEditor({ projectId }: { projectId: number }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const members = useMembers(projectId);
  const directory = useDirectory();
  const me = useMe();
  const [error, setError] = useState<unknown>(null);
  const [handing, setHanding] = useState<Member | null>(null);
  const owner = (members.data ?? []).find((m) => m.project_owner);
  // Admins always; the project owner hands it to another member (checked again by the server).
  const mayHandOver = !!me && (me.is_admin || owner?.user_id === me.id);
  const inProject = new Set((members.data ?? []).map((m) => m.user_id));
  const others = (directory.data ?? []).filter((p) => !inProject.has(p.id));

  const run = async (action: () => Promise<Member[]>) => {
    setError(null);
    try {
      qc.setQueryData(qk.members(projectId), await action());
      qc.invalidateQueries({ queryKey: qk.people(projectId) });
      await qc.invalidateQueries({ queryKey: qk.projects });
    } catch (err) {
      setError(err);
    }
  };
  const setRole = (userId: number, role: ProjectRole) => run(() => api.put<Member[]>(`/projects/${projectId}/members/${userId}`, { role }));

  return (
    <div className="space-y-2">
      <ul className="divide-y divide-line rounded-sm ring-1 ring-line">
        {(members.data ?? []).map((m) => (
          <li key={m.user_id} className="flex items-center gap-2 px-3 py-1.5">
            <div className="min-w-0 flex-1 truncate">
              <span className="font-medium">{m.name}</span> <span className="text-fg-3">{m.username}</span>
              {m.project_owner && (
                <span title={t("members.projectOwnerHint")}>
                  {" "}
                  <Badge tone="indigo">
                    <Crown className="size-3" />
                    {t("members.projectOwner")}
                  </Badge>
                </span>
              )}
            </div>
            {mayHandOver && !m.project_owner && (
              <IconButton label={t("members.makeOwner")} onClick={() => setHanding(m)}>
                <Crown className="size-4" />
              </IconButton>
            )}
            <div className="w-32 shrink-0">
              <Select aria-label={t("members.role")} value={m.role} onChange={(e) => setRole(m.user_id, e.target.value as ProjectRole)}>
                {ROLES.map((role) => (
                  <option key={role} value={role}>
                    {t(`members.roles.${role}`)}
                  </option>
                ))}
              </Select>
            </div>
            <IconButton label={t("members.remove")} onClick={() => run(() => api.del<Member[]>(`/projects/${projectId}/members/${m.user_id}`))}>
              <Trash2 className="size-4" />
            </IconButton>
          </li>
        ))}
        {members.data?.length === 0 && <li className="px-3 py-2 text-fg-3">{t("members.none")}</li>}
      </ul>
      {others.length > 0 ? (
        <Select aria-label={t("members.add")} value="" onChange={(e) => e.target.value && setRole(Number(e.target.value), "member")}>
          <option value="">{t("members.add")}</option>
          {others.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.username})
            </option>
          ))}
        </Select>
      ) : (
        <p className="text-fg-3">{t("members.nobodyElse")}</p>
      )}
      <p className="text-fg-3">{t("members.rolesHint")}</p>
      <ErrorText error={error} />
      {handing && (
        <HandOver
          projectId={projectId}
          member={handing}
          onClose={() => setHanding(null)}
          onDone={(next) => {
            setHanding(null);
            qc.setQueryData(qk.members(projectId), next);
            qc.invalidateQueries({ queryKey: qk.projects });
          }}
        />
      )}
    </div>
  );
}

/** What changes under the new project owner, then the change itself. */
function HandOver({ projectId, member, onClose, onDone }: { projectId: number; member: Member; onClose: () => void; onDone: (members: Member[]) => void }) {
  const { t } = useTranslation();
  const preview = useQuery({
    queryKey: ["ownerChange", projectId, member.user_id],
    queryFn: () => api.get<OwnerChange>(`/projects/${projectId}/owner/${member.user_id}`),
    gcTime: 0,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const c = preview.data;
  const limit = (n: number | null) => (n === null ? t("members.change.noLimit") : String(n));
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await api.put<Member[]>(`/projects/${projectId}/owner/${member.user_id}`, {}));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const changes = c
    ? [
        c.lost_accounts.length > 0 && t("members.change.lostAccounts", { list: c.lost_accounts.join(", ") }),
        c.unpinned.length > 0 && t("members.change.unpinned", { list: c.unpinned.map((n) => (n === "the project" ? t("members.change.theProject") : n)).join(", ") }),
        c.loses_shared && t("members.change.losesShared"),
        c.max_parallel_before !== c.max_parallel_after && t("members.change.parallel", { before: limit(c.max_parallel_before), after: limit(c.max_parallel_after) }),
      ].filter((x): x is string => !!x)
    : [];
  return (
    <Modal
      open
      onClose={onClose}
      title={t("members.makeOwnerTitle", { name: member.name })}
      width="max-w-lg"
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" icon={<Crown className="size-3.5" />} loading={busy} disabled={!c?.runner_allowed} onClick={confirm}>
            {t("members.makeOwnerConfirm")}
          </Button>
        </>
      }
    >
      <div className="space-y-2">
        <p>{t("members.makeOwnerIntro", { name: member.name })}</p>
        {preview.isLoading && <p className="text-fg-3">{t("members.change.loading")}</p>}
        {c && !c.runner_allowed && <p className="text-you-fg">{t("members.change.runnerBlocked", { name: c.name, runner: c.runner || t("members.change.builtinRunner") })}</p>}
        {c && changes.length > 0 && (
          <ul className="list-disc space-y-1 pl-5">
            {changes.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
        {c && c.runner_allowed && changes.length === 0 && <p className="text-fg-3">{t("members.change.nothing")}</p>}
        <ErrorText error={preview.error ?? error} />
      </div>
    </Modal>
  );
}
