import { useQuery, useQueryClient } from "@tanstack/react-query";
import { GitBranch, Plus, RefreshCw, Sparkles, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { useOptimistic } from "../../lib/hooks";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { useAgents } from "../../api/queries";
import type { Agent, Skill } from "../../api/types";
import { Button, ErrorText, IconButton, Input, Toggle } from "../../components/ui";
import { formatBytes, timeAgo } from "../../lib/format";

const skillsKey = (pid: number) => ["skills", pid] as const;
/** Claude Code loads skills only with the full toolset (the project's settings). */
const usesSkills = (agent: Agent) => agent.provider === "claude_code" && agent.toolset === "full";

/** Claude skills of a project: uploaded (a zip of the folder or a SKILL.md) or from git, for all or chosen agents. Saved at once. */
export function SkillsEditor({ projectId }: { projectId: number }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const skills = useQuery({ queryKey: skillsKey(projectId), queryFn: () => api.get<Skill[]>(`/projects/${projectId}/skills`) });
  const agents = (useAgents(projectId).data ?? []).filter((a) => a.enabled);
  const [git, setGit] = useState<{ url: string; path: string; ref: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<unknown>(null);
  const file = useRef<HTMLInputElement>(null);
  const base = `/projects/${projectId}/skills`;

  const run = async (key: string, action: () => Promise<unknown>) => {
    setError(null);
    setBusy(key);
    try {
      await action();
      await qc.invalidateQueries({ queryKey: skillsKey(projectId) });
      return true;
    } catch (err) {
      setError(err);
      return false;
    } finally {
      setBusy("");
    }
  };

  const upload = (picked: File | undefined, path = base, method: "post" | "put" = "post") => {
    if (!picked) return;
    const body = new FormData();
    body.append("file", picked);
    return run(path, () => api[method](path, body));
  };

  const importGit = async () => {
    if (git && (await run("git", () => api.post(`${base}/import`, git)))) setGit(null);
  };

  const others = agents.filter((a) => !usesSkills(a));

  return (
    <div className="space-y-2">
      {(skills.data ?? []).map((skill) => (
        <SkillRow key={skill.id} skill={skill} agents={agents} base={base} busy={busy} run={run} upload={upload} />
      ))}
      {git === null ? (
        <div className="flex flex-wrap gap-2">
          <input
            ref={file}
            type="file"
            accept=".zip,.md"
            className="hidden"
            onChange={(e) => {
              upload(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
          <Button size="sm" icon={<Upload className="size-3.5" />} loading={busy === base} onClick={() => file.current?.click()}>
            {t("skills.upload")}
          </Button>
          <Button size="sm" icon={<GitBranch className="size-3.5" />} onClick={() => setGit({ url: "", path: "", ref: "" })}>
            {t("skills.fromGit")}
          </Button>
        </div>
      ) : (
        <div className="grid gap-2 rounded-sm p-2 ring-1 ring-line sm:grid-cols-[2fr_1fr_1fr]">
          <Input autoFocus placeholder="https://github.com/owner/repo" value={git.url} onChange={(e) => setGit({ ...git, url: e.target.value })} />
          <Input placeholder={t("skills.path")} value={git.path} onChange={(e) => setGit({ ...git, path: e.target.value })} />
          <Input placeholder={t("skills.ref")} value={git.ref} onChange={(e) => setGit({ ...git, ref: e.target.value })} />
          <div className="flex gap-2 sm:col-span-3">
            <Button size="sm" variant="primary" icon={<Plus className="size-3.5" />} loading={busy === "git"} disabled={!git.url.trim()} onClick={importGit}>
              {t("skills.import")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setGit(null)}>
              {t("common.cancel")}
            </Button>
          </div>
        </div>
      )}
      <p className="text-fg-3">{t("skills.hint")}</p>
      {others.length > 0 && <p className="text-fg-3">{t("skills.notClaude", { names: others.map((a) => a.name).join(", ") })}</p>}
      <ErrorText error={error} />
    </div>
  );
}

function SkillRow({
  skill,
  agents,
  base,
  busy,
  run,
  upload,
}: {
  skill: Skill;
  agents: Agent[];
  base: string;
  busy: string;
  run: (key: string, action: () => Promise<unknown>) => Promise<boolean>;
  upload: (picked: File | undefined, path?: string, method?: "post" | "put") => unknown;
}) {
  const { t } = useTranslation();
  const file = useRef<HTMLInputElement>(null);
  const path = `${base}/${skill.id}`;
  const patch = (body: Partial<Pick<Skill, "enabled" | "all_agents" | "agent_ids">>) => run(`${path}/patch`, () => api.patch(path, body));
  const [agentIds, changeAgents] = useOptimistic(skill.agent_ids);
  const toggleAgent = (id: number) =>
    void changeAgents(agentIds.includes(id) ? agentIds.filter((a) => a !== id) : [...agentIds, id], (agent_ids) => patch({ agent_ids }));
  const remove = () => confirm(t("skills.confirmDelete", { name: skill.name })) && run(path, () => api.del(path));
  const source = skill.source === "git" ? [skill.source_url, skill.source_path, skill.source_ref].filter(Boolean).join(" · ") : t("skills.uploaded");

  return (
    <div className="space-y-2 rounded-sm px-2 py-1.5 ring-1 ring-line">
      <div className="flex flex-wrap items-center gap-2">
        <Sparkles className="size-3.5 shrink-0 text-fg-3" />
        <div className="min-w-0 flex-1">
          <div className="truncate font-mono font-medium">{skill.name}</div>
          <div className="line-clamp-2 text-fg-2">{skill.description}</div>
          <div className="truncate text-fg-3">
            {t("skills.meta", { source, count: skill.file_count, size: formatBytes(skill.size), when: timeAgo(skill.updated_at) })}
          </div>
        </div>
        <input
          ref={file}
          type="file"
          accept=".zip,.md"
          className="hidden"
          onChange={(e) => {
            upload(e.target.files?.[0], `${path}/files`, "put");
            e.target.value = "";
          }}
        />
        {skill.source === "git" ? (
          <IconButton label={t("skills.reimport")} disabled={!!busy} onClick={() => run(`${path}/reimport`, () => api.post(`${path}/reimport`))}>
            <RefreshCw className={busy === `${path}/reimport` ? "size-3.5 animate-spin" : "size-3.5"} />
          </IconButton>
        ) : (
          <IconButton label={t("skills.replace")} disabled={!!busy} onClick={() => file.current?.click()}>
            <Upload className="size-3.5" />
          </IconButton>
        )}
        <IconButton label={t("skills.delete")} disabled={!!busy} onClick={remove}>
          <Trash2 className="size-3.5" />
        </IconButton>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Toggle checked={skill.enabled} onChange={(enabled) => patch({ enabled })} label={t("skills.enabled")} />
        <Toggle checked={skill.all_agents} onChange={(all_agents) => patch({ all_agents })} label={t("skills.allAgents")} />
      </div>
      {!skill.all_agents && (
        <div className="flex flex-wrap gap-1.5">
          {agents.map((agent) => {
            const on = agentIds.includes(agent.id);
            return (
              <button
                key={agent.id}
                type="button"
                aria-pressed={on}
                onClick={() => toggleAgent(agent.id)}
                className={`rounded-sm px-2 py-0.5 ring-1 ring-inset touch:py-2 ${on ? "bg-sunk font-medium text-fg ring-line-2" : "text-fg-3 ring-line hover:text-fg"}`}
              >
                {agent.name}
              </button>
            );
          })}
          {agentIds.length === 0 && <span className="text-fg-3">{t("skills.noAgents")}</span>}
        </div>
      )}
    </div>
  );
}
