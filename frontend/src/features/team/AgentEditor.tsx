import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useMcpServers, useMeta, useRepositories, useSettings } from "../../api/queries";
import type { Agent, ProviderInfo } from "../../api/types";
import { Button, ErrorText, Field, Input, Modal, Select, Textarea, Toggle } from "../../components/ui";
import { formatTokens } from "../../lib/format";
import { useShell } from "../layout/shell-context";

const CUSTOM = "__custom__";

export function ModelSelect({ provider, value, onChange }: { provider?: ProviderInfo; value: string; onChange: (v: string) => void }) {
  const { t } = useTranslation();
  const known = provider?.models.some((m) => m.id === value) ?? false;
  const [custom, setCustom] = useState(!known && !!value);
  return (
    <div className="space-y-1.5">
      <Select
        value={custom ? CUSTOM : value}
        onChange={(e) => {
          if (e.target.value === CUSTOM) setCustom(true);
          else {
            setCustom(false);
            onChange(e.target.value);
          }
        }}
      >
        {!value && <option value="">{t("team.defaultModel")}</option>}
        {provider?.models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.label}
          </option>
        ))}
        <option value={CUSTOM}>{t("team.customModel")}</option>
      </Select>
      {custom && <Input value={value} placeholder="model-id" onChange={(e) => onChange(e.target.value)} />}
    </div>
  );
}

export function AgentEditor({ agent, onClose }: { agent?: Agent; onClose: () => void }) {
  const { t } = useTranslation();
  const { project } = useShell();
  const qc = useQueryClient();
  const meta = useMeta();
  const settings = useSettings();
  const mcp = useMcpServers(project.id);
  const repos = useRepositories(project.id);
  const defaults = settings.data?.values ?? {};

  const [form, setForm] = useState(() => ({
    name: agent?.name ?? "",
    role: agent?.role ?? "",
    description: agent?.description ?? "",
    instructions: agent?.instructions ?? "",
    provider: agent?.provider ?? defaults.default_provider ?? "claude_code",
    model: agent?.model ?? "",
    effort: agent?.effort ?? defaults.default_effort ?? "",
    toolset: agent?.toolset ?? "full",
    context_limit_tokens: agent?.context_limit_tokens ?? defaults.default_context_limit ?? 60000,
    keep_recent_messages: agent?.keep_recent_messages ?? defaults.default_keep_recent ?? 6,
    summary_model: agent?.summary_model ?? "",
    max_turns: agent?.max_turns ?? "",
    enabled: agent?.enabled ?? true,
    color: agent?.color ?? "",
    mcp_server_ids: agent?.mcp_server_ids ?? [],
    all_repos: agent?.all_repos ?? true,
    repository_ids: agent?.repository_ids ?? [],
  }));
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));
  const provider = useMemo(() => meta.data?.providers.find((p) => p.name === form.provider), [meta.data, form.provider]);
  const selectableServers = (mcp.data ?? []).filter((s) => !s.all_agents);

  const save = async () => {
    setBusy(true);
    setError(null);
    const body = {
      ...form,
      model: form.model || null,
      summary_model: form.summary_model || null,
      effort: form.effort || (agent ? "" : null),
      max_turns: form.max_turns === "" ? null : Number(form.max_turns),
      context_limit_tokens: Number(form.context_limit_tokens),
      keep_recent_messages: Number(form.keep_recent_messages),
      color: form.color || null,
    };
    try {
      if (agent) await api.patch(`/agents/${agent.id}`, body);
      else await api.post(`/projects/${project.id}/agents`, body);
      await qc.invalidateQueries({ queryKey: qk.agents(project.id) });
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!agent || !confirm(t("team.confirmDelete", { name: agent.name }))) return;
    try {
      await api.del(`/agents/${agent.id}`);
      await qc.invalidateQueries({ queryKey: qk.agents(project.id) });
      onClose();
    } catch (err) {
      setError(err);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={agent ? agent.name : t("team.add")}
      footer={
        <>
          {agent && !agent.is_manager && (
            <Button variant="danger" className="mr-auto" onClick={remove}>
              {t("common.delete")}
            </Button>
          )}
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" loading={busy} onClick={save} disabled={!form.name.trim()}>
            {agent ? t("common.save") : t("team.create")}
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-[1fr_1fr_auto]">
          <Field label={t("team.name")}>
            <Input autoFocus={!agent} value={form.name} onChange={(e) => set({ name: e.target.value })} />
          </Field>
          <Field label={t("team.role")}>
            <Input placeholder={t("team.rolePlaceholder")} value={form.role} onChange={(e) => set({ role: e.target.value })} />
          </Field>
          <Field label={t("team.color")}>
            <input type="color" value={form.color || agent?.color || "#6366f1"} onChange={(e) => set({ color: e.target.value })} className="h-8 w-12 cursor-pointer rounded-md bg-transparent" />
          </Field>
        </div>
        <Field label={t("team.area")} hint={t("team.areaHint")}>
          <Textarea rows={3} value={form.description} onChange={(e) => set({ description: e.target.value })} />
        </Field>
        <Field label={t("team.instructions")} hint={t("team.instructionsHint")}>
          <Textarea rows={4} value={form.instructions} onChange={(e) => set({ instructions: e.target.value })} className="font-mono text-[13px]" />
        </Field>

        <fieldset className="grid gap-4 rounded-lg bg-zinc-50 p-4 sm:grid-cols-2 dark:bg-zinc-800/40">
          <legend className="sr-only">{t("team.modelSection")}</legend>
          <Field label={t("team.provider")}>
            <Select value={form.provider} onChange={(e) => set({ provider: e.target.value, model: "", summary_model: "" })}>
              {(meta.data?.providers ?? [{ name: form.provider, label: form.provider }]).map((p) => (
                <option key={p.name} value={p.name}>
                  {p.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("team.model")}>
            <ModelSelect provider={provider} value={form.model} onChange={(model) => set({ model })} />
          </Field>
          <Field label={t("team.effort")} hint={t("team.effortHint")}>
            <Select value={form.effort ?? ""} onChange={(e) => set({ effort: e.target.value })}>
              <option value="">{t("team.effortDefault")}</option>
              {(provider?.efforts ?? []).map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("team.toolset")} hint={t(`toolset.${form.toolset}Hint`)}>
            <Select value={form.toolset} onChange={(e) => set({ toolset: e.target.value as Agent["toolset"] })}>
              <option value="full">{t("toolset.full")}</option>
              <option value="board">{t("toolset.board")}</option>
            </Select>
          </Field>
        </fieldset>

        <fieldset className="grid gap-4 rounded-lg bg-zinc-50 p-4 sm:grid-cols-2 dark:bg-zinc-800/40">
          <legend className="sr-only">{t("team.contextSection")}</legend>
          <Field
            label={t("team.contextLimit")}
            hint={
              <>
                {t("team.contextLimitHint")}
                {agent?.base_context_tokens ? (
                  <span className={clsx("block", form.context_limit_tokens < agent.base_context_tokens + 4000 && "text-amber-600")}>
                    {t("team.baseContext", { tokens: formatTokens(agent.base_context_tokens) })}
                  </span>
                ) : null}
              </>
            }
          >
            <Input type="number" min={5000} step={5000} value={form.context_limit_tokens} onChange={(e) => set({ context_limit_tokens: Number(e.target.value) })} />
          </Field>
          <Field label={t("team.summaryModel")} hint={t("team.summaryModelHint")}>
            <ModelSelect provider={provider} value={form.summary_model} onChange={(summary_model) => set({ summary_model })} />
          </Field>
          <Field label={t("team.keepRecent")} hint={t("team.keepRecentHint")}>
            <Input type="number" min={0} max={50} value={form.keep_recent_messages} onChange={(e) => set({ keep_recent_messages: Number(e.target.value) })} />
          </Field>
          <Field label={t("team.maxTurns")} hint={t("team.maxTurnsHint")}>
            <Input type="number" min={1} value={form.max_turns} onChange={(e) => set({ max_turns: e.target.value === "" ? "" : Number(e.target.value) })} />
          </Field>
        </fieldset>

        {(repos.data?.length ?? 0) > 0 && !agent?.is_manager && (
          <Field group label={t("team.repositories")} hint={t("team.repositoriesHint")}>
            <div className="space-y-2 pt-1">
              <Toggle checked={form.all_repos} onChange={(all_repos) => set({ all_repos })} label={t("team.allRepos")} />
              {!form.all_repos && (
                <div className="flex flex-wrap gap-3 pl-11">
                  {repos.data!.map((r) => (
                    <label key={r.id} className="flex items-center gap-1.5 text-sm">
                      <input
                        type="checkbox"
                        checked={form.repository_ids.includes(r.id)}
                        onChange={(e) =>
                          set({ repository_ids: e.target.checked ? [...form.repository_ids, r.id] : form.repository_ids.filter((id) => id !== r.id) })
                        }
                      />
                      {r.name}
                    </label>
                  ))}
                </div>
              )}
            </div>
          </Field>
        )}
        {selectableServers.length > 0 && (
          <Field group label={t("team.mcpServers")} hint={t("team.mcpServersHint")}>
            <div className="flex flex-wrap gap-3 pt-1">
              {selectableServers.map((s) => (
                <label key={s.id} className="flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={form.mcp_server_ids.includes(s.id)}
                    onChange={(e) =>
                      set({ mcp_server_ids: e.target.checked ? [...form.mcp_server_ids, s.id] : form.mcp_server_ids.filter((id) => id !== s.id) })
                    }
                  />
                  {s.name}
                </label>
              ))}
            </div>
          </Field>
        )}
        {!agent?.is_manager && <Toggle checked={form.enabled} onChange={(enabled) => set({ enabled })} label={t("team.enabled")} />}
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}
