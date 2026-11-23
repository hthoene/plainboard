import { History, RotateCcw, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { useIdeaVersions } from "../../api/queries";
import type { Idea, IdeaTopic } from "../../api/types";
import { Button, ErrorText, Field, Input, Modal, Select, Spinner, Textarea } from "../../components/ui";
import { timeAgo } from "../../lib/format";
import { IdeaQuestion, ideaTitle } from "./IdeaCard";

/** An idea opened: its title and text to edit in place, its topic, and its earlier versions to restore. */
export function IdeaDialog({ idea, topics, canWrite, onClose }: { idea: Idea; topics: IdeaTopic[]; canWrite: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const [title, setTitle] = useState(idea.title);
  const [text, setText] = useState(idea.text);
  const [topic, setTopic] = useState(idea.topic_id === null ? "" : String(idea.topic_id));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [history, setHistory] = useState(false);
  const versions = useIdeaVersions(idea.project_id, idea.id, history);
  const base = `/projects/${idea.project_id}/ideas/${idea.id}`;
  const topicId = topic === "" ? null : Number(topic);
  const changed = title.trim() !== idea.title || text.trim() !== idea.text || topicId !== idea.topic_id;

  const run = async (action: () => Promise<unknown>, close = true) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      if (close) onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const save = () =>
    run(() => {
      const body: Record<string, unknown> = {};
      if (title.trim() !== idea.title) body.title = title.trim();
      if (text.trim() !== idea.text) body.text = text.trim();
      if (topicId !== idea.topic_id) body.topic_id = topicId;
      return api.patch(base, body);
    });
  const restore = (versionId: number) =>
    run(async () => {
      const restored = await api.post<Idea>(`${base}/versions/${versionId}/restore`);
      setTitle(restored.title);
      setText(restored.text);
    }, false);
  const remove = () => window.confirm(t("ideas.edit.deleteConfirm")) && run(() => api.del(base));

  return (
    <Modal
      open
      onClose={onClose}
      title={canWrite ? t("ideas.edit.title") : ideaTitle(idea)}
      width="sm:max-w-xl"
      footer={
        canWrite && (
          <>
            <Button variant="danger" icon={<Trash2 className="size-4" />} onClick={remove} disabled={busy} className="mr-auto">
              {t("ideas.edit.delete")}
            </Button>
            <Button variant="ghost" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button variant="primary" onClick={save} loading={busy} disabled={!changed || !text.trim()}>
              {t("common.save")}
            </Button>
          </>
        )
      }
    >
      <div data-part="idea-edit" className="flex flex-col gap-4">
        {canWrite ? (
          <>
            <Field label={t("ideas.edit.name")}>
              <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} placeholder={t("ideas.edit.namePlaceholder")} />
            </Field>
            <Field label={t("ideas.edit.text")}>
              <Textarea value={text} onChange={(e) => setText(e.target.value)} rows={6} />
            </Field>
            <Field group label={t("ideas.edit.topic")} hint={idea.topic_manual ? t("ideas.edit.pinned") : t("ideas.edit.topicHint")}>
              <Select value={topic} onChange={(e) => setTopic(e.target.value)} aria-label={t("ideas.edit.topic")}>
                <option value="">{t("ideas.unsorted")}</option>
                {topics.map((tp) => (
                  <option key={tp.id} value={String(tp.id)}>
                    {tp.name}
                  </option>
                ))}
              </Select>
            </Field>
          </>
        ) : (
          <p className="whitespace-pre-line">{idea.text}</p>
        )}
        {idea.question_status === "open" && <IdeaQuestion idea={idea} canWrite={canWrite} />}
        {idea.question_status === "answered" && (
          <p className="text-fg-2">
            {idea.question} → <span className="text-fg">{idea.question_answer}</span>
          </p>
        )}
        <p className="text-fg-3">{t("ideas.edit.by", { author: idea.author || t("ideas.edit.someone"), when: timeAgo(idea.created_at) })}</p>
        <ErrorText error={error} />
        <section className="flex flex-col gap-2">
          <button type="button" aria-expanded={history} onClick={() => setHistory(!history)} className="inline-flex items-center gap-1.5 self-start font-medium text-fg-2 hover:text-fg">
            <History className="size-4" />
            {t("ideas.edit.history")}
          </button>
          {history &&
            (versions.isLoading ? (
              <Spinner />
            ) : !versions.data?.length ? (
              <p className="text-fg-3">{t("ideas.edit.noHistory")}</p>
            ) : (
              <ul data-part="idea-history" className="flex flex-col">
                {versions.data.map((v) => (
                  <li key={v.id} className="flex items-start gap-3 border-b border-line py-2 last:border-b-0">
                    <div className="min-w-0 flex-1">
                      <div className="font-medium">{v.title || t("ideas.edit.untitled")}</div>
                      <p className="line-clamp-3 whitespace-pre-line text-fg-2">{v.text}</p>
                      <div className="text-fg-3">
                        {t(`ideas.edit.source.${v.source}`)} · {timeAgo(v.created_at)}
                      </div>
                    </div>
                    {canWrite && (
                      <Button size="sm" variant="ghost" icon={<RotateCcw className="size-3.5" />} onClick={() => restore(v.id)} disabled={busy}>
                        {t("ideas.edit.restore")}
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            ))}
        </section>
      </div>
    </Modal>
  );
}
