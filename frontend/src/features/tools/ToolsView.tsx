import { useQueryClient } from "@tanstack/react-query";
import { Globe, Plug, Plus, Terminal } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useMcpServers } from "../../api/queries";
import type { McpServer } from "../../api/types";
import { Badge, Button, EmptyState, ErrorText, Field, Input, Modal, Select, Textarea, Toggle } from "../../components/ui";
import { useShell } from "../layout/shell-context";

const toLines = (map: Record<string, string>) =>
  Object.entries(map)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");

const fromLines = (text: string) =>
  Object.fromEntries(
    text
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.includes("="))
      .map((line) => [line.slice(0, line.indexOf("=")).trim(), line.slice(line.indexOf("=") + 1).trim()]),
  );

export function ToolsView() {
  const { t } = useTranslation();
  const { project, agentById } = useShell();
  const servers = useMcpServers(project.id);
  const [editing, setEditing] = useState<McpServer | "new" | null>(null);

  return (
    <div className="scroll-thin h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">{t("tools.title")}</h1>
            <p className="text-sm text-zinc-500">{t("tools.subtitle")}</p>
          </div>
          <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
            {t("tools.add")}
          </Button>
        </div>
        {servers.data?.length === 0 ? (
          <EmptyState icon={<Plug className="size-10" />} title={t("tools.empty")}>
            {t("tools.emptyHint")}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-zinc-100 rounded-xl bg-white ring-1 ring-zinc-200 dark:divide-zinc-800 dark:bg-zinc-900 dark:ring-zinc-800">
            {servers.data?.map((s) => (
              <li key={s.id}>
                <button onClick={() => setEditing(s)} className="flex w-full items-center gap-4 px-4 py-3 text-left hover:bg-zinc-50 dark:hover:bg-zinc-800/40">
                  <div className="text-zinc-400">{s.transport === "stdio" ? <Terminal className="size-4" /> : <Globe className="size-4" />}</div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-sm font-medium">
                      {s.name}
                      <Badge>{s.transport}</Badge>
                      {s.project_id === null && <Badge tone="sky">{t("tools.global")}</Badge>}
                      {!s.enabled && <Badge tone="red">{t("tools.disabled")}</Badge>}
                    </div>
                    <div className="truncate font-mono text-xs text-zinc-500">{s.transport === "stdio" ? [s.command, ...s.args].join(" ") : s.url}</div>
                  </div>
                  <div className="hidden text-xs text-zinc-500 sm:block">
                    {s.all_agents ? t("tools.allAgents") : s.agent_ids.map((id) => agentById(id)?.name ?? id).join(", ") || t("tools.noAgents")}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      {editing && <McpEditor server={editing === "new" ? undefined : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function McpEditor({ server, onClose }: { server?: McpServer; onClose: () => void }) {
  const { t } = useTranslation();
  const { project } = useShell();
  const qc = useQueryClient();
  const [form, setForm] = useState({
    name: server?.name ?? "",
    transport: server?.transport ?? ("stdio" as McpServer["transport"]),
    command: server?.command ?? "",
    args: (server?.args ?? []).join("\n"),
    url: server?.url ?? "",
    env: toLines(server?.env ?? {}),
    headers: toLines(server?.headers ?? {}),
    enabled: server?.enabled ?? true,
    all_agents: server?.all_agents ?? true,
    global_scope: false,
  });
  const [error, setError] = useState<unknown>(null);
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  const save = async () => {
    setError(null);
    const body: Record<string, unknown> = {
      name: form.name,
      transport: form.transport,
      command: form.command || null,
      args: form.args.split("\n").map((a) => a.trim()).filter(Boolean),
      url: form.url || null,
      env: fromLines(form.env),
      headers: fromLines(form.headers),
      enabled: form.enabled,
      all_agents: form.all_agents,
    };
    try {
      if (server) await api.patch(`/projects/${project.id}/mcp-servers/${server.id}`, body);
      else await api.post(`/projects/${project.id}/mcp-servers`, { ...body, global_scope: form.global_scope });
      qc.invalidateQueries({ queryKey: qk.mcp(project.id) });
      onClose();
    } catch (err) {
      setError(err);
    }
  };

  const remove = async () => {
    if (!server || !confirm(t("tools.confirmDelete", { name: server.name }))) return;
    await api.del(`/projects/${project.id}/mcp-servers/${server.id}`);
    qc.invalidateQueries({ queryKey: qk.mcp(project.id) });
    onClose();
  };

  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-lg"
      title={server ? server.name : t("tools.add")}
      footer={
        <>
          {server && (
            <Button variant="danger" className="mr-auto" onClick={remove}>
              {t("common.delete")}
            </Button>
          )}
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" onClick={save} disabled={!form.name.trim()}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-[1fr_140px] gap-3">
          <Field label={t("tools.name")} hint={t("tools.nameHint")}>
            <Input autoFocus value={form.name} onChange={(e) => set({ name: e.target.value })} placeholder="github" />
          </Field>
          <Field label={t("tools.transport")}>
            <Select value={form.transport} onChange={(e) => set({ transport: e.target.value as McpServer["transport"] })}>
              <option value="stdio">stdio</option>
              <option value="http">http</option>
              <option value="sse">sse</option>
            </Select>
          </Field>
        </div>
        {form.transport === "stdio" ? (
          <>
            <Field label={t("tools.command")} hint={t("tools.commandHint")}>
              <Input value={form.command} onChange={(e) => set({ command: e.target.value })} placeholder="npx" className="font-mono" />
            </Field>
            <Field label={t("tools.args")} hint={t("tools.argsHint")}>
              <Textarea rows={3} value={form.args} onChange={(e) => set({ args: e.target.value })} placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/workspaces"} className="font-mono text-[13px]" />
            </Field>
            <Field label={t("tools.env")} hint={t("tools.secretsHint")}>
              <Textarea rows={3} value={form.env} onChange={(e) => set({ env: e.target.value })} placeholder="API_KEY=..." className="font-mono text-[13px]" />
            </Field>
          </>
        ) : (
          <>
            <Field label="URL">
              <Input value={form.url} onChange={(e) => set({ url: e.target.value })} placeholder="https://example.com/mcp" className="font-mono" />
            </Field>
            <Field label={t("tools.headers")} hint={t("tools.secretsHint")}>
              <Textarea rows={3} value={form.headers} onChange={(e) => set({ headers: e.target.value })} placeholder="Authorization=Bearer ..." className="font-mono text-[13px]" />
            </Field>
          </>
        )}
        <Toggle checked={form.all_agents} onChange={(all_agents) => set({ all_agents })} label={t("tools.allAgentsToggle")} />
        {!form.all_agents && <p className="-mt-2 pl-11 text-xs text-zinc-500">{t("tools.selectAgentsHint")}</p>}
        <Toggle checked={form.enabled} onChange={(enabled) => set({ enabled })} label={t("tools.enabled")} />
        {!server && <Toggle checked={form.global_scope} onChange={(global_scope) => set({ global_scope })} label={t("tools.globalToggle")} />}
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}
