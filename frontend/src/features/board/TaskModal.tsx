import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Bot, ChevronDown, ChevronRight, FileText, Play, Send, Trash2, Upload, X } from "lucide-react";
import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { qk, useTask } from "../../api/queries";
import type { Assignment, Attachment, Priority, TaskDetail, TaskStatus } from "../../api/types";
import { Lightbox } from "../../components/Lightbox";
import { Markdown } from "../../components/Markdown";
import { AgentDot, Badge, Button, ErrorText, IconButton, Input, Modal, Select, Spinner, Textarea } from "../../components/ui";
import { formatBytes, timeAgo } from "../../lib/format";
import { useShell } from "../layout/shell-context";
import { statusColor, STATUSES } from "./Board";
import { Decisions, isImage, reviewAttachmentIds } from "./Decisions";

const PRIORITIES: Priority[] = ["low", "medium", "high", "urgent"];

export function TaskModal({ taskId, onClose }: { taskId: number; onClose: () => void }) {
  const { t } = useTranslation();
  const { project } = useShell();
  const qc = useQueryClient();
  const task = useTask(taskId);
  const [error, setError] = useState<unknown>(null);
  const [viewing, setViewing] = useState<number | null>(null);

  const patch = async (body: Partial<TaskDetail>) => {
    setError(null);
    try {
      await api.patch(`/tasks/${taskId}`, body);
      qc.invalidateQueries({ queryKey: qk.task(taskId) });
      qc.invalidateQueries({ queryKey: qk.tasks(project.id) });
    } catch (err) {
      setError(err);
    }
  };

  const upload = async (files: FileList | File[]) => {
    if (!files.length) return;
    const form = new FormData();
    for (const file of Array.from(files)) form.append("files", file, file.name || "pasted.png");
    try {
      await api.post(`/tasks/${taskId}/attachments`, form);
      qc.invalidateQueries({ queryKey: qk.task(taskId) });
    } catch (err) {
      setError(err);
    }
  };

  const onPaste = (e: ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files);
    if (files.length) {
      e.preventDefault();
      void upload(files);
    }
  };

  if (!task.data) {
    return (
      <Modal open onClose={onClose} title="">
        <div className="flex justify-center py-10">{task.error ? <ErrorText error={task.error} /> : <Spinner />}</div>
      </Modal>
    );
  }
  const data = task.data;
  const images = data.attachments.filter(isImage);

  const handToManager = async () => {
    setError(null);
    try {
      await api.post(`/tasks/${taskId}/start`);
      qc.invalidateQueries({ queryKey: qk.task(taskId) });
    } catch (err) {
      setError(err);
    }
  };

  const remove = async () => {
    if (!confirm(t("ticket.confirmDelete", { number: data.number }))) return;
    await api.del(`/tasks/${taskId}`);
    qc.invalidateQueries({ queryKey: qk.tasks(project.id) });
    onClose();
  };

  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-4xl"
      title={
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm text-zinc-400">#{data.number}</span>
          <TitleEditor value={data.title} onSave={(title) => patch({ title })} />
        </div>
      }
    >
      <div className="mb-6 empty:hidden">
        <Decisions decisions={data.decisions} attachments={data.attachments} onView={setViewing} />
      </div>
      <div className="grid gap-6 md:grid-cols-[1fr_220px]" onPaste={onPaste}>
        <div className="min-w-0 space-y-6">
          <DescriptionEditor value={data.description} onSave={(description) => patch({ description })} />
          <CustomerTexts title={data.customer_title} description={data.customer_description} onSave={patch} />
          <Attachments attachments={data.attachments} reviewIds={reviewAttachmentIds(data.decisions)} onUpload={upload} onView={setViewing} />
          <Assignments assignments={data.assignments} />
          <Comments taskId={taskId} comments={data.comments} />
        </div>
        <aside className="order-first space-y-4 text-sm md:order-none">
          <Button variant="primary" className="w-full" icon={<Play className="size-3.5" />} onClick={handToManager} disabled={data.manager_active}>
            {data.manager_active ? t("ticket.withManager") : t("ticket.handToManager")}
          </Button>
          <p className="-mt-2 text-xs text-zinc-500">{t("ticket.handToManagerHint")}</p>
          <SideField label={t("ticket.status")}>
            <div className="flex items-center gap-2">
              <span className={clsx("size-2 shrink-0 rounded-full", statusColor[data.status])} />
              <Select value={data.status} onChange={(e) => patch({ status: e.target.value as TaskStatus })}>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {t(`taskStatus.${s}`)}
                  </option>
                ))}
              </Select>
            </div>
          </SideField>
          <SideField label={t("ticket.priority")}>
            <Select value={data.priority} onChange={(e) => patch({ priority: e.target.value as Priority })}>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {t(`priority.${p}`)}
                </option>
              ))}
            </Select>
          </SideField>
          <SideField label={t("ticket.labels")}>
            <LabelsEditor value={data.labels} onSave={(labels) => patch({ labels })} />
          </SideField>
          <div className="space-y-1 border-t border-zinc-100 pt-3 text-xs text-zinc-500 dark:border-zinc-800">
            <div>{t("ticket.created", { when: timeAgo(data.created_at) })}</div>
            <div>{t("ticket.updated", { when: timeAgo(data.updated_at) })}</div>
          </div>
          <Button variant="danger" size="sm" icon={<Trash2 className="size-3.5" />} onClick={remove}>
            {t("common.delete")}
          </Button>
          <ErrorText error={error} />
        </aside>
      </div>
      {viewing !== null && (
        <Lightbox
          images={images.map((a) => ({ src: `/api/attachments/${a.id}`, alt: a.filename }))}
          start={Math.max(
            images.findIndex((a) => a.id === viewing),
            0,
          )}
          onClose={() => setViewing(null)}
        />
      )}
    </Modal>
  );
}

function SideField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium text-zinc-500">{label}</div>
      {children}
    </div>
  );
}

function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <h3 className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">{children}</h3>
      {right}
    </div>
  );
}

function TitleEditor({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <input
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft.trim() && draft !== value && onSave(draft.trim())}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      className="min-w-0 flex-1 rounded bg-transparent px-1 py-0.5 outline-none hover:bg-zinc-50 focus:bg-zinc-50 dark:hover:bg-zinc-800 dark:focus:bg-zinc-800"
    />
  );
}

function CustomerTexts({ title, description, onSave }: { title: string; description: string; onSave: (body: { customer_title?: string; customer_description?: string }) => void }) {
  const { t } = useTranslation();
  const [draftTitle, setDraftTitle] = useState(title);
  const [draftDescription, setDraftDescription] = useState(description);
  useEffect(() => setDraftTitle(title), [title]);
  useEffect(() => setDraftDescription(description), [description]);
  return (
    <section className="space-y-2">
      <SectionTitle>{t("ticket.customerSection")}</SectionTitle>
      <Input
        aria-label={t("ticket.customerTitle")}
        placeholder={t("ticket.customerTitle")}
        maxLength={150}
        value={draftTitle}
        onChange={(e) => setDraftTitle(e.target.value)}
        onBlur={() => draftTitle !== title && onSave({ customer_title: draftTitle })}
      />
      <Textarea
        aria-label={t("ticket.customerDescription")}
        placeholder={t("ticket.customerDescription")}
        rows={2}
        maxLength={400}
        value={draftDescription}
        onChange={(e) => setDraftDescription(e.target.value)}
        onBlur={() => draftDescription !== description && onSave({ customer_description: draftDescription })}
      />
      <p className="text-xs text-zinc-500">{t("ticket.customerHint")}</p>
    </section>
  );
}

function DescriptionEditor({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(!value);
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  return (
    <section>
      <SectionTitle
        right={
          !editing && (
            <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
              {t("common.edit")}
            </Button>
          )
        }
      >
        {t("ticket.description")}
      </SectionTitle>
      {editing ? (
        <div className="space-y-2">
          <Textarea autoFocus={!!value} rows={8} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={t("ticket.descriptionPlaceholder")} className="font-mono text-[13px]" />
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="primary"
              onClick={() => {
                onSave(draft);
                setEditing(false);
              }}
            >
              {t("common.save")}
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setDraft(value);
                setEditing(!value);
              }}
            >
              {t("common.cancel")}
            </Button>
          </div>
        </div>
      ) : (
        <div onDoubleClick={() => setEditing(true)} className="rounded-md">
          <Markdown>{value}</Markdown>
        </div>
      )}
    </section>
  );
}

function LabelsEditor({ value, onSave }: { value: string[]; onSave: (v: string[]) => void }) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState("");
  const add = () => {
    const label = draft.trim();
    if (label && !value.includes(label)) onSave([...value, label]);
    setDraft("");
  };
  return (
    <div className="space-y-1.5">
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {value.map((label) => (
            <Badge key={label}>
              {label}
              <button onClick={() => onSave(value.filter((l) => l !== label))} aria-label={t("common.remove")}>
                <X className="size-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
      <Input value={draft} placeholder={t("ticket.addLabel")} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} onBlur={add} />
    </div>
  );
}

function Attachments({
  attachments,
  reviewIds,
  onUpload,
  onView,
}: {
  attachments: Attachment[];
  reviewIds: Set<number>;
  onUpload: (files: FileList) => void;
  onView: (id: number) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    onUpload(e.dataTransfer.files);
  };
  const remove = async (a: Attachment) => {
    await api.del(`/attachments/${a.id}`);
    qc.invalidateQueries({ queryKey: ["task"] });
  };

  return (
    <section>
      <SectionTitle
        right={
          <Button size="sm" variant="ghost" icon={<Upload className="size-3.5" />} onClick={() => input.current?.click()}>
            {t("ticket.upload")}
          </Button>
        }
      >
        {t("ticket.attachments")}
      </SectionTitle>
      <input ref={input} type="file" multiple hidden onChange={(e) => e.target.files && onUpload(e.target.files)} />
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        className={clsx("rounded-lg border border-dashed p-2 transition-colors", over ? "border-indigo-400 bg-indigo-50/50 dark:bg-indigo-950/20" : "border-zinc-200 dark:border-zinc-800")}
      >
        {attachments.length === 0 ? (
          <p className="py-3 text-center text-xs text-zinc-400">{t("ticket.dropFiles")}</p>
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {attachments.map((a) => (
              <div key={a.id} className="group relative overflow-hidden rounded-md ring-1 ring-zinc-200 dark:ring-zinc-800">
                {isImage(a) ? (
                  <button type="button" onClick={() => onView(a.id)} className="block w-full cursor-zoom-in">
                    <img src={`/api/attachments/${a.id}`} alt={a.filename} className="h-24 w-full object-cover" />
                  </button>
                ) : (
                  <a href={`/api/attachments/${a.id}`} target="_blank" rel="noreferrer" className="flex h-24 items-center justify-center bg-zinc-50 dark:bg-zinc-800/60">
                    <FileText className="size-8 text-zinc-300" />
                  </a>
                )}
                <a href={`/api/attachments/${a.id}`} target="_blank" rel="noreferrer" className="block truncate px-2 py-1 text-xs">
                  {a.filename} <span className="text-zinc-400">· {formatBytes(a.size)}</span>
                </a>
                {reviewIds.has(a.id) && (
                  <Badge tone="amber" className="absolute top-1 left-1">
                    {t("ticket.forReview")}
                  </Badge>
                )}
                <IconButton label={t("common.delete")} onClick={() => remove(a)} className="absolute top-1 right-1 bg-white/90 opacity-0 group-hover:opacity-100 dark:bg-zinc-900/90">
                  <Trash2 className="size-3.5" />
                </IconButton>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

const assignmentTone = { queued: "amber", running: "indigo", done: "green", failed: "red", cancelled: "zinc" } as const;

function Assignments({ assignments }: { assignments: Assignment[] }) {
  const { t } = useTranslation();
  const { agentById, openChat } = useShell();
  const [open, setOpen] = useState<number | null>(null);
  if (!assignments.length) return null;
  return (
    <section>
      <SectionTitle>{t("ticket.assignments")}</SectionTitle>
      <ul className="divide-y divide-zinc-100 rounded-lg ring-1 ring-zinc-200 dark:divide-zinc-800 dark:ring-zinc-800">
        {assignments.map((a) => {
          const agent = agentById(a.agent_id);
          const expanded = open === a.id;
          return (
            <li key={a.id} className="text-sm">
              <button onClick={() => setOpen(expanded ? null : a.id)} className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-zinc-50 dark:hover:bg-zinc-800/50">
                {expanded ? <ChevronDown className="size-3.5 text-zinc-400" /> : <ChevronRight className="size-3.5 text-zinc-400" />}
                {agent && <AgentDot color={agent.color} status={a.status === "running" ? "running" : undefined} />}
                <span className="font-medium">{agent?.name ?? "?"}</span>
                <span className="min-w-0 flex-1 truncate text-zinc-600 dark:text-zinc-300">{a.title}</span>
                {a.effort && (
                  <Badge tone="sky" className="hidden sm:inline-flex">
                    {t("ticket.effort", { effort: a.effort })}
                  </Badge>
                )}
                <Badge tone={assignmentTone[a.status]}>{t(`assignment.${a.status}`)}</Badge>
              </button>
              {expanded && (
                <div className="space-y-3 border-t border-zinc-100 bg-zinc-50/60 px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900/40">
                  <div>
                    <div className="mb-1 text-xs font-medium text-zinc-500">{t("ticket.instructions")}</div>
                    <Markdown className="text-[13px]">{a.instructions}</Markdown>
                  </div>
                  {a.result && (
                    <div>
                      <div className="mb-1 text-xs font-medium text-zinc-500">{t("ticket.result")}</div>
                      <Markdown className="text-[13px]">{a.result}</Markdown>
                    </div>
                  )}
                  <div className="flex gap-2">
                    {agent && (
                      <Button size="sm" icon={<Bot className="size-3.5" />} onClick={() => openChat(agent.id)}>
                        {t("ticket.openAgentChat")}
                      </Button>
                    )}
                    {(a.status === "queued" || a.status === "running") && (
                      <Button size="sm" variant="danger" onClick={() => api.post(`/assignments/${a.id}/cancel`)}>
                        {t("common.cancel")}
                      </Button>
                    )}
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Comments({ taskId, comments }: { taskId: number; comments: TaskDetail["comments"] }) {
  const { t } = useTranslation();
  const { agentById } = useShell();
  const qc = useQueryClient();
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);

  const send = async () => {
    if (!body.trim()) return;
    try {
      await api.post(`/tasks/${taskId}/comments`, { body });
      setBody("");
      qc.invalidateQueries({ queryKey: qk.task(taskId) });
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <section>
      <SectionTitle>{t("ticket.comments")}</SectionTitle>
      <div className="space-y-3">
        {comments.map((c) => {
          const agent = agentById(c.author_agent_id);
          const author = c.author_kind === "agent" ? (agent?.name ?? t("ticket.agent")) : c.author_kind === "system" ? t("ticket.system") : t("ticket.you");
          return (
            <div key={c.id} className="flex gap-2.5">
              <div
                className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold text-white"
                style={{ background: agent?.color ?? (c.author_kind === "system" ? "#a1a1aa" : "#52525b") }}
              >
                {author[0]?.toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-xs text-zinc-500">
                  <span className="font-medium text-zinc-700 dark:text-zinc-300">{author}</span> · {timeAgo(c.created_at)}
                </div>
                <Markdown className="text-[13px]">{c.body}</Markdown>
              </div>
            </div>
          );
        })}
        <div className="flex items-end gap-2">
          <Textarea
            rows={2}
            value={body}
            placeholder={t("ticket.commentPlaceholder")}
            onChange={(e) => setBody(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send();
            }}
          />
          <IconButton label={t("ticket.sendComment")} onClick={send} className="mb-1">
            <Send className="size-4" />
          </IconButton>
        </div>
        {error && <p className="text-sm text-red-600">{error}</p>}
      </div>
    </section>
  );
}
