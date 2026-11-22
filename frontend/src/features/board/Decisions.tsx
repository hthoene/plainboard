import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { CircleCheck, CircleSlash, FileText, Hand, MessageCircle } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk } from "../../api/queries";
import type { Attachment, Decision } from "../../api/types";
import { AttachmentLink } from "../../components/AttachmentLink";
import { Markdown, TicketText } from "../../components/Markdown";
import { Button, ErrorText, Textarea } from "../../components/ui";
import { useShell } from "../layout/shell-context";

export const isImage = (attachment: Attachment) => attachment.content_type.startsWith("image/");

/** Attachments of open decisions: those still wait for the user's review. */
export function reviewAttachmentIds(decisions: Decision[]): Set<number> {
  return new Set(decisions.filter((d) => d.status === "open").flatMap((d) => [...d.attachment_ids, ...d.options.flatMap((o) => o.attachment_ids)]));
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
          <button key={a.id} type="button" onClick={() => onView(a.id)} title={a.filename} className={clsx("cursor-zoom-in overflow-hidden rounded-sm ring-1 ring-line", box)}>
            <img src={`/api/attachments/${a.id}`} alt={a.filename} className="size-full object-cover" />
          </button>
        ) : (
          <AttachmentLink
            key={a.id}
            file={a}
            title={a.filename}
            className={clsx("flex flex-col items-center justify-center gap-1 overflow-hidden rounded-sm bg-sunk px-1 text-fg-3 ring-1 ring-line", box)}
          >
            <FileText className="size-4 shrink-0" />
            {!small && <span className="w-full truncate text-center">{a.filename}</span>}
          </AttachmentLink>
        ),
      )}
    </div>
  );
}

function OpenDecision({ decision, attachments, onView }: { decision: Decision; attachments: Attachment[]; onView: (id: number) => void }) {
  const { t } = useTranslation();
  const { project, agents, agentById, openChat, canWrite } = useShell();
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
    <div className="space-y-3 rounded-sm bg-you-bg p-3 ring-1 ring-you">
      <div>
        <div className="flex flex-wrap items-center gap-x-2 font-semibold text-you-fg">
          <span className="flex items-center gap-1">
            <Hand className="size-3.5" />
            {t("decision.title")}
          </span>
          {asker && <span className="font-normal text-fg-3">{t("decision.requestedBy", { name: asker.name })}</span>}
        </div>
        <p className="mt-1 font-medium text-fg"><TicketText text={decision.question} /></p>
        {decision.details && <Markdown className="mt-1">{decision.details}</Markdown>}
      </div>
      <Thumbs ids={decision.attachment_ids} attachments={attachments} onView={onView} />
      {decision.options.length > 0 && (
        <div className={clsx("grid gap-2", visual && "sm:grid-cols-2")}>
          {decision.options.map((option, index) => (
            <div key={index} className="flex flex-col gap-2 rounded-sm bg-bg p-2 ring-1 ring-line">
              <Thumbs ids={option.attachment_ids} attachments={attachments} onView={onView} />
              <div className={clsx("flex flex-1 gap-2", visual ? "flex-col" : "items-center")}>
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{option.label}</div>
                  {option.description && <Markdown className="text-fg-2">{option.description}</Markdown>}
                </div>
                {canWrite && (
                  <Button size="sm" className="shrink-0" disabled={busy} onClick={() => send(index)}>
                    {t("decision.choose")}
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {!canWrite && <p className="text-fg-3">{t("members.readOnlyShort")}</p>}
      {canWrite && <Textarea rows={2} value={note} disabled={busy} placeholder={t(decision.options.length ? "decision.notePlaceholder" : "decision.answerPlaceholder")} onChange={(e) => setNote(e.target.value)} />}
      {canWrite && <div className="flex flex-wrap items-center justify-end gap-2">
        {asker && (
          <Button size="sm" variant="ghost" icon={<MessageCircle className="size-3.5" />} onClick={() => openChat(asker.id)}>
            {t("decision.askInChat", { name: asker.name })}
          </Button>
        )}
        <Button size="sm" variant="primary" disabled={busy || !note.trim()} onClick={() => send(null)}>
          {t("decision.sendAnswer")}
        </Button>
      </div>}
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
    <div className="flex items-start gap-2 rounded-sm bg-sunk px-3 py-2 text-fg-3">
      {decision.status === "answered" ? <CircleCheck className="mt-px size-3.5 shrink-0 text-ok" /> : <CircleSlash className="mt-px size-3.5 shrink-0" />}
      <div className="min-w-0 flex-1 space-y-1">
        <div>
          <TicketText text={decision.question} />
        </div>
        {decision.status === "answered" ? (
          <div className="font-medium text-fg">
            {answer}
            {recorder && <span className="ml-1 font-normal text-fg-3">{t("decision.recordedBy", { name: recorder.name })}</span>}
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
