import { useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Plus, Repeat, Timer } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useSchedules, useSettings } from "../../api/queries";
import type { Schedule } from "../../api/types";
import { AgentDot, Badge, Button, EmptyState, ErrorText, Field, Input, Modal, Select, Textarea, Toggle } from "../../components/ui";
import { formatDateTime, timeAgo, toLocalInput } from "../../lib/format";
import { useShell } from "../layout/shell-context";

const PRESETS = [
  { cron: "0 * * * *", key: "hourly" },
  { cron: "0 9 * * *", key: "daily" },
  { cron: "0 9 * * 1-5", key: "weekdays" },
  { cron: "0 9 * * 1", key: "weekly" },
];

export function SchedulesView() {
  const { t } = useTranslation();
  const { project, agentById } = useShell();
  const qc = useQueryClient();
  const schedules = useSchedules(project.id);
  const [editing, setEditing] = useState<Schedule | "new" | null>(null);

  const toggle = async (s: Schedule, enabled: boolean) => {
    await api.patch(`/projects/${project.id}/schedules/${s.id}`, { enabled });
    qc.invalidateQueries({ queryKey: qk.schedules(project.id) });
  };

  return (
    <div className="scroll-thin h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">{t("schedules.title")}</h1>
            <p className="text-sm text-zinc-500">{t("schedules.subtitle")}</p>
          </div>
          <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
            {t("schedules.new")}
          </Button>
        </div>
        {schedules.data?.length === 0 ? (
          <EmptyState icon={<CalendarClock className="size-10" />} title={t("schedules.empty")}>
            {t("schedules.emptyHint")}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-zinc-100 rounded-xl bg-white ring-1 ring-zinc-200 dark:divide-zinc-800 dark:bg-zinc-900 dark:ring-zinc-800">
            {schedules.data?.map((s) => {
              const agent = agentById(s.agent_id);
              return (
                <li key={s.id} className="flex items-center gap-4 px-4 py-3">
                  <div className="text-zinc-400">{s.kind === "cron" ? <Repeat className="size-4" /> : <Timer className="size-4" />}</div>
                  <button className="min-w-0 flex-1 text-left" onClick={() => setEditing(s)}>
                    <div className="flex items-center gap-2 text-sm font-medium">
                      <span className="truncate">{s.name}</span>
                      {s.kind === "cron" ? <Badge className="font-mono">{s.cron}</Badge> : <Badge>{formatDateTime(s.run_at)}</Badge>}
                    </div>
                    <div className="mt-0.5 flex items-center gap-1.5 truncate text-xs text-zinc-500">
                      {agent && <AgentDot color={agent.color} />}
                      {agent?.name} · {s.next_run_at ? t("schedules.next", { when: timeAgo(s.next_run_at) }) : t("schedules.noNext")}
                      {s.last_run_at && ` · ${t("schedules.last", { when: timeAgo(s.last_run_at) })}`}
                    </div>
                  </button>
                  <Toggle checked={s.enabled} onChange={(v) => toggle(s, v)} label="" />
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {editing && <ScheduleEditor schedule={editing === "new" ? undefined : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function ScheduleEditor({ schedule, onClose }: { schedule?: Schedule; onClose: () => void }) {
  const { t } = useTranslation();
  const { project, agents } = useShell();
  const qc = useQueryClient();
  const settings = useSettings();
  const manager = agents.find((a) => a.is_manager);
  const [form, setForm] = useState({
    name: schedule?.name ?? "",
    agent_id: schedule?.agent_id ?? manager?.id ?? 0,
    prompt: schedule?.prompt ?? "",
    kind: schedule?.kind ?? ("cron" as "once" | "cron"),
    cron: schedule?.cron ?? "0 9 * * 1-5",
    run_at: toLocalInput(schedule?.run_at),
    timezone: schedule?.timezone ?? settings.data?.values.timezone ?? "UTC",
  });
  const [error, setError] = useState<unknown>(null);
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  const save = async () => {
    setError(null);
    const body = {
      name: form.name,
      agent_id: Number(form.agent_id),
      prompt: form.prompt,
      kind: form.kind,
      cron: form.kind === "cron" ? form.cron : null,
      // The browser's local time is sent with its offset, so the server stores the exact instant.
      run_at: form.kind === "once" && form.run_at ? new Date(form.run_at).toISOString() : null,
      timezone: form.timezone,
    };
    try {
      if (schedule) await api.patch(`/projects/${project.id}/schedules/${schedule.id}`, body);
      else await api.post(`/projects/${project.id}/schedules`, body);
      qc.invalidateQueries({ queryKey: qk.schedules(project.id) });
      onClose();
    } catch (err) {
      setError(err);
    }
  };

  const remove = async () => {
    if (!schedule || !confirm(t("schedules.confirmDelete"))) return;
    await api.del(`/projects/${project.id}/schedules/${schedule.id}`);
    qc.invalidateQueries({ queryKey: qk.schedules(project.id) });
    onClose();
  };

  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-lg"
      title={schedule ? schedule.name : t("schedules.new")}
      footer={
        <>
          {schedule && (
            <Button variant="danger" className="mr-auto" onClick={remove}>
              {t("common.delete")}
            </Button>
          )}
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" onClick={save} disabled={!form.name.trim() || !form.prompt.trim()}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label={t("schedules.name")}>
          <Input autoFocus value={form.name} onChange={(e) => set({ name: e.target.value })} placeholder={t("schedules.namePlaceholder")} />
        </Field>
        <Field label={t("schedules.agent")}>
          <Select value={form.agent_id} onChange={(e) => set({ agent_id: Number(e.target.value) })}>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} — {a.role}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("schedules.prompt")} hint={t("schedules.promptHint")}>
          <Textarea rows={4} value={form.prompt} onChange={(e) => set({ prompt: e.target.value })} />
        </Field>
        <div className="flex gap-2">
          {(["cron", "once"] as const).map((kind) => (
            <Button key={kind} size="sm" variant={form.kind === kind ? "primary" : "secondary"} onClick={() => set({ kind })}>
              {t(`schedules.kind.${kind}`)}
            </Button>
          ))}
        </div>
        {form.kind === "cron" ? (
          <Field group label={t("schedules.cron")} hint={t("schedules.cronHint")}>
            <Input value={form.cron} onChange={(e) => set({ cron: e.target.value })} className="font-mono" />
            <div className="flex flex-wrap gap-1.5 pt-1">
              {PRESETS.map((p) => (
                <button key={p.cron} type="button" onClick={() => set({ cron: p.cron })} className="rounded bg-zinc-100 px-2 py-0.5 text-xs hover:bg-zinc-200 dark:bg-zinc-800 dark:hover:bg-zinc-700">
                  {t(`schedules.preset.${p.key}`)}
                </button>
              ))}
            </div>
          </Field>
        ) : (
          <Field label={t("schedules.runAt")}>
            <Input type="datetime-local" value={form.run_at} onChange={(e) => set({ run_at: e.target.value })} />
          </Field>
        )}
        <Field label={t("schedules.timezone")}>
          <Input value={form.timezone} onChange={(e) => set({ timezone: e.target.value })} />
        </Field>
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}
