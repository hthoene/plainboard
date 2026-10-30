import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { CircleCheck, CircleSlash, FileText, Hand, MessageCircle } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk } from "../../api/queries";
import type { Attachment, Decision } from "../../api/types";
import { Markdown } from "../../components/Markdown";
import { Button, ErrorText, Textarea } from "../../components/ui";
import { useShell } from "../layout/shell-context";

export const isImage = (attachment: Attachment) => attachment.content_type.startsWith("image/");

/** Attachments that a decision shows to the user. */
export function reviewAttachmentIds(decisions: Decision[]): Set<number> {
  return new Set(decisions.flatMap((d) => [...d.attachment_ids, ...d.options.flatMap((o) => o.attachment_ids)]));
}

function Thumbs({ ids, attachments, onView, small }: { ids: number[]; attachments: Attachment[]; onView: (id: number) => void; small?: boolean }) {
  // Attachments can be deleted after the question was asked.
  const files = ids.map((id) => attachments.find((a) => a.id === id)).filter((a) => a !== undefined);
  if (!files.length) return null;
  const box = small ? "size-10" : "h-28 w-full";
  return (
    <div className={clsx("gap-1.5", small ? "flex flex-wrap" : "grid grid-cols-[repeat(auto-fit,minmax(6rem,1fr))]")}>
      {files.map((a) =>
        isImage(a) ? (
          <button key={a.id} type="button" onClick={() => onView(a.id)} title={a.filename} className={clsx("cursor-zoom-in overflow-hidden rounded-md ring-1 ring-zinc-200 dark:ring-zinc-700", box)}>
            <img src={`/api/attachments/${a.id}`} alt={a.filename} className="size-full object-cover" />
          </button>
        ) : (
          <a
            key={a.id}
            href={`/api/attachments/${a.id}`}
            target="_blank"
            rel="noreferrer"
            title={a.filename}
            className={clsx("flex flex-col items-center justify-center gap-1 overflow-hidden rounded-md bg-zinc-50 px-1 text-[11px] text-zinc-500 ring-1 ring-zinc-200 dark:bg-zinc-800/60 dark:ring-zinc-700", box)}
          >
            <FileText className="size-4 shrink-0" />
            {!small && <span className="w-full truncate text-center">{a.filename}</span>}
          </a>
        ),
      )}
    </div>
  );
}

function OpenDecision({ decision, attachments, onView }: { decision: Decision; attachments: Attachment[]; onView: (id: number) => void }) {
  const { t } = useTranslation();
  const { project, agents, agentById, openChat } = useShell();
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const asker = agentById(decision.requested_by_agent_id) ?? agents.find((a) => a.is_manager);
  const visual = decision.options.some((o) => o.attachment_ids.length > 0);

  const send = async (optionIndex: number | null) => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/decisions/${decision.id}/answer`, { option_index: optionIndex, text: note });
      qc.invalidateQueries({ queryKey: qk.task(decision.task_id) });
      qc.invalidateQueries({ queryKey: qk.tasks(project.id) });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3 rounded-lg bg-amber-50/70 p-3 ring-1 ring-amber-300 dark:bg-amber-950/20 dark:ring-amber-700/60">
      <div>
        <div className="flex flex-wrap items-center gap-x-2 text-xs font-semibold text-amber-700 dark:text-amber-300">
          <span className="flex items-center gap-1">
            <Hand className="size-3.5" />
            {t("decision.title")}
          </span>
          {asker && <span className="font-normal text-zinc-500">{t("decision.requestedBy", { name: asker.name })}</span>}
        </div>
        <p className="mt-1 text-sm font-medium text-zinc-900 dark:text-zinc-100">{decision.question}</p>
        {decision.details && <Markdown className="mt-1 text-[13px]">{decision.details}</Markdown>}
      </div>
      <Thumbs ids={decision.attachment_ids} attachments={attachments} onView={onView} />
      {decision.options.length > 0 && (
        <div className={clsx("grid gap-2", visual && "sm:grid-cols-2")}>
          {decision.options.map((option, index) => (
            <div key={index} className="flex flex-col gap-2 rounded-md bg-white p-2 ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-800">
              <Thumbs ids={option.attachment_ids} attachments={attachments} onView={onView} />
              <div className={clsx("flex flex-1 gap-2", visual ? "flex-col" : "items-center")}>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">{option.label}</div>
                  {option.description && <Markdown className="text-xs text-zinc-600 dark:text-zinc-400">{option.description}</Markdown>}
                </div>
                <Button size="sm" className="shrink-0" disabled={busy} onClick={() => send(index)}>
                  {t("decision.choose")}
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
      <Textarea rows={2} value={note} disabled={busy} placeholder={t(decision.options.length ? "decision.notePlaceholder" : "decision.answerPlaceholder")} onChange={(e) => setNote(e.target.value)} />
      <div className="flex flex-wrap items-center justify-end gap-2">
        {asker && (
          <Button size="sm" variant="ghost" icon={<MessageCircle className="size-3.5" />} onClick={() => openChat(asker.id)}>
            {t("decision.askInChat", { name: asker.name })}
          </Button>
        )}
        <Button size="sm" variant="primary" disabled={busy || !note.trim()} onClick={() => send(null)}>
          {t("decision.sendAnswer")}
        </Button>
      </div>
      <ErrorText error={error} />
    </div>
  );
}

function ClosedDecision({ decision, attachments, onView }: { decision: Decision; attachments: Attachment[]; onView: (id: number) => void }) {
  const { t } = useTranslation();
  const { agentById } = useShell();
  const chosen = decision.answer_option === null ? undefined : decision.options[decision.answer_option];
  const answer = [chosen?.label, decision.answer_text].filter(Boolean).join(" — ");
  const recorder = decision.answered_by === "agent" ? agentById(decision.requested_by_agent_id) : undefined;
  return (
    <div className="flex items-start gap-2 rounded-md bg-zinc-50 px-3 py-2 text-xs text-zinc-500 dark:bg-zinc-800/40">
      {decision.status === "answered" ? <CircleCheck className="mt-px size-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" /> : <CircleSlash className="mt-px size-3.5 shrink-0" />}
      <div className="min-w-0 flex-1 space-y-1">
        <div>{decision.question}</div>
        {decision.status === "answered" ? (
          <div className="font-medium text-zinc-700 dark:text-zinc-300">
            {answer}
            {recorder && <span className="ml-1 font-normal text-zinc-500">{t("decision.recordedBy", { name: recorder.name })}</span>}
          </div>
        ) : (
          <div>{t("decision.withdrawn")}</div>
        )}
      </div>
      {chosen && <Thumbs small ids={chosen.attachment_ids} attachments={attachments} onView={onView} />}
    </div>
  );
}

export function Decisions({ decisions, attachments, onView }: { decisions: Decision[]; attachments: Attachment[]; onView: (id: number) => void }) {
  if (!decisions.length) return null;
  const open = decisions.filter((d) => d.status === "open");
  const closed = decisions.filter((d) => d.status !== "open");
  return (
    <section className="space-y-2">
      {open.map((d) => (
        <OpenDecision key={d.id} decision={d} attachments={attachments} onView={onView} />
      ))}
      {closed.map((d) => (
        <ClosedDecision key={d.id} decision={d} attachments={attachments} onView={onView} />
      ))}
    </section>
  );
}
