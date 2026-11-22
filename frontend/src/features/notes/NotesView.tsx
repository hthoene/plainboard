import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { ChevronLeft, NotebookPen, Pin, Plus, Search, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useMeta, useNotes } from "../../api/queries";
import type { Note } from "../../api/types";
import { Markdown } from "../../components/Markdown";
import { Button, ChatPanelToggle, EmptyState, ErrorText, Input, Textarea, Toggle } from "../../components/ui";
import { timeAgo } from "../../lib/format";
import { useShell } from "../layout/shell-context";

export function NotesView() {
  const { t } = useTranslation();
  const { project, agentById, canWrite } = useShell();
  const qc = useQueryClient();
  const notes = useNotes(project.id);
  const [selected, setSelected] = useState<number | "new" | null>(null);
  const [query, setQuery] = useState("");
  // Phones show either the list or one note.
  const [detail, setDetail] = useState(false);
  const open = (id: number | "new") => {
    setSelected(id);
    setDetail(true);
  };

  const meta = useMeta();
  const budget = meta.data?.pinned_note_budget_chars ?? 4000;
  const pinnedChars = (notes.data ?? []).filter((n) => n.pinned).reduce((sum, n) => sum + n.content.trim().length, 0);
  const list = (notes.data ?? []).filter((n) => !query || `${n.title} ${n.content}`.toLowerCase().includes(query.toLowerCase()));
  const current = typeof selected === "number" ? notes.data?.find((n) => n.id === selected) : undefined;

  useEffect(() => {
    if (selected === null && notes.data?.length) setSelected(notes.data[0].id);
  }, [notes.data, selected]);

  return (
    <div className="flex h-full">
      <aside className={clsx("flex w-full shrink-0 flex-col border-line md:w-72 md:border-r", detail && "max-md:hidden")}>
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-2.5">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-3" />
            <Input className="pl-8" placeholder={t("notes.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          {canWrite && <Button variant="primary" size="md" icon={<Plus className="size-4" />} onClick={() => open("new")} aria-label={t("notes.new")} />}
        </div>
        <ul className="scroll-thin flex-1 overflow-y-auto">
          {list.map((n) => (
            <li key={n.id}>
              <button
                onClick={() => open(n.id)}
                className={clsx(
                  "w-full border-b border-line px-2.5 py-2 text-left",
                  selected === n.id ? "bg-sunk" : "hover:bg-sunk",
                )}
              >
                <div className="flex items-center gap-1.5 font-medium">
                  {n.pinned && <Pin className="size-3 shrink-0 text-fg" />}
                  <span className="truncate">{n.title}</span>
                </div>
                <div className="truncate text-fg-3">
                  {(agentById(n.created_by_agent_id)?.name ?? t("ticket.you")) + " · " + timeAgo(n.updated_at)}
                </div>
              </button>
            </li>
          ))}
          {notes.data && list.length === 0 && <li className="px-2 py-6 text-center text-fg-3">{t("notes.empty")}</li>}
        </ul>
        <div className="border-t border-line px-3 py-2" title={t("notes.budgetHint")}>
          <div className="mb-1 flex justify-between text-fg-3">
            <span>{t("notes.budget")}</span>
            <span className={clsx("tabular-nums", pinnedChars > budget && "text-you-fg")}>
              {pinnedChars.toLocaleString()} / {budget.toLocaleString()}
            </span>
          </div>
          <div className="h-1 bg-line">
            <div className={clsx("h-full", pinnedChars > budget ? "bg-you" : "bg-fg-2")} style={{ width: `${Math.min(100, (pinnedChars / budget) * 100)}%` }} />
          </div>
        </div>
      </aside>
      <section className={clsx("relative min-w-0 flex-1 flex-col", detail ? "flex" : "max-md:hidden md:flex")}>
        <div className="absolute top-2.5 right-4 z-10 flex items-center">
          <ChatPanelToggle where="view" />
        </div>
        <button onClick={() => setDetail(false)} className="flex items-center gap-1 px-4 pt-3 text-fg-3 md:hidden">
          <ChevronLeft className="size-4" /> {t("notes.back")}
        </button>
        <div className="min-h-0 flex-1">
        {selected === "new" ? (
          <NoteEditor
            key="new"
            onSaved={(n) => {
              qc.invalidateQueries({ queryKey: qk.notes(project.id) });
              setSelected(n.id);
            }}
          />
        ) : current ? (
          <NoteEditor key={current.id} note={current} onSaved={() => qc.invalidateQueries({ queryKey: qk.notes(project.id) })} onDeleted={() => {
              setSelected(null);
              setDetail(false);
            }} />
        ) : (
          <EmptyState icon={<NotebookPen className="size-10" />} title={t("notes.title")}>
            {t("notes.hint")}
          </EmptyState>
        )}
        </div>
      </section>
    </div>
  );
}

function NoteEditor({ note, onSaved, onDeleted }: { note?: Note; onSaved: (n: Note) => void; onDeleted?: () => void }) {
  const { t } = useTranslation();
  const { project, canWrite } = useShell();
  const [title, setTitle] = useState(note?.title ?? "");
  const [content, setContent] = useState(note?.content ?? "");
  const [pinned, setPinned] = useState(note?.pinned ?? false);
  const [editing, setEditing] = useState(!note);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!editing && note) {
      setTitle(note.title);
      setContent(note.content);
      setPinned(note.pinned);
    }
  }, [note, editing]);

  const save = async () => {
    setError(null);
    try {
      const body = { title, content, pinned };
      const saved = note ? await api.patch<Note>(`/projects/${project.id}/notes/${note.id}`, body) : await api.post<Note>(`/projects/${project.id}/notes`, body);
      setEditing(false);
      onSaved(saved);
    } catch (err) {
      setError(err);
    }
  };

  const remove = async () => {
    if (!note || !confirm(t("notes.confirmDelete"))) return;
    await api.del(`/projects/${project.id}/notes/${note.id}`);
    onSaved(note);
    onDeleted?.();
  };

  const togglePin = async (value: boolean) => {
    setPinned(value);
    if (note && !editing) onSaved(await api.patch<Note>(`/projects/${project.id}/notes/${note.id}`, { pinned: value }));
  };

  return (
    <div className="scroll-thin h-full overflow-y-auto">
      <div className="max-w-3xl space-y-4 px-4 pb-6">
        <div className="flex min-h-12 items-center gap-3 border-b border-line md:pr-10">
          {editing ? (
            <Input autoFocus value={title} placeholder={t("notes.titlePlaceholder")} onChange={(e) => setTitle(e.target.value)} className="h-10 font-semibold" />
          ) : (
            <h1 className="min-w-0 flex-1 font-extrabold break-words">{note?.title}</h1>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {canWrite ? (
            <>
              <Toggle checked={pinned} onChange={togglePin} label={t("notes.pinned")} />
              <span className="hidden text-fg-3 sm:inline">{t("notes.pinnedHint")}</span>
            </>
          ) : (
            pinned && <span className="text-fg-3">{t("notes.pinned")}</span>
          )}
          <div className="flex-1" />
          {editing ? (
            <>
              {note && <Button onClick={() => setEditing(false)}>{t("common.cancel")}</Button>}
              <Button variant="primary" onClick={save} disabled={!title.trim()}>
                {t("common.save")}
              </Button>
            </>
          ) : (
            canWrite && (
            <>
              <Button onClick={() => setEditing(true)}>{t("common.edit")}</Button>
              <Button variant="danger" icon={<Trash2 className="size-3.5" />} onClick={remove} aria-label={t("common.delete")} />
            </>
            )
          )}
        </div>
        <ErrorText error={error} />
        {editing ? (
          <Textarea rows={20} value={content} onChange={(e) => setContent(e.target.value)} placeholder={t("notes.contentPlaceholder")} className="font-mono" />
        ) : (
          <Markdown>{note?.content || ""}</Markdown>
        )}
      </div>
    </div>
  );
}
