import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { ChevronLeft, NotebookPen, Pin, Plus, Search, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useMeta, useNotes } from "../../api/queries";
import type { Note } from "../../api/types";
import { Markdown } from "../../components/Markdown";
import { Button, EmptyState, ErrorText, Input, Textarea, Toggle } from "../../components/ui";
import { timeAgo } from "../../lib/format";
import { useShell } from "../layout/shell-context";

export function NotesView() {
  const { t } = useTranslation();
  const { project, agentById } = useShell();
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
      <aside className={clsx("flex w-full shrink-0 flex-col border-zinc-200 md:w-72 md:border-r dark:border-zinc-800", detail && "max-md:hidden")}>
        <div className="flex items-center gap-2 p-3">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute top-2 left-2.5 size-4 text-zinc-400" />
            <Input className="pl-8" placeholder={t("notes.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <Button variant="primary" size="md" icon={<Plus className="size-4" />} onClick={() => open("new")} aria-label={t("notes.new")} />
        </div>
        <ul className="scroll-thin flex-1 overflow-y-auto px-2 pb-3">
          {list.map((n) => (
            <li key={n.id}>
              <button
                onClick={() => open(n.id)}
                className={clsx(
                  "w-full rounded-md px-2.5 py-2 text-left",
                  selected === n.id ? "bg-zinc-100 dark:bg-zinc-800" : "hover:bg-zinc-50 dark:hover:bg-zinc-900",
                )}
              >
                <div className="flex items-center gap-1.5 text-sm font-medium">
                  {n.pinned && <Pin className="size-3 shrink-0 text-indigo-500" />}
                  <span className="truncate">{n.title}</span>
                </div>
                <div className="truncate text-xs text-zinc-500">
                  {(agentById(n.created_by_agent_id)?.name ?? t("ticket.you")) + " · " + timeAgo(n.updated_at)}
                </div>
              </button>
            </li>
          ))}
          {notes.data && list.length === 0 && <li className="px-2 py-6 text-center text-xs text-zinc-400">{t("notes.empty")}</li>}
        </ul>
        <div className="border-t border-zinc-200 px-3 py-2 dark:border-zinc-800" title={t("notes.budgetHint")}>
          <div className="mb-1 flex justify-between text-[11px] text-zinc-500">
            <span>{t("notes.budget")}</span>
            <span className={clsx("tabular-nums", pinnedChars > budget && "text-amber-600")}>
              {pinnedChars.toLocaleString()} / {budget.toLocaleString()}
            </span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
            <div className={clsx("h-full rounded-full", pinnedChars > budget ? "bg-amber-500" : "bg-indigo-500")} style={{ width: `${Math.min(100, (pinnedChars / budget) * 100)}%` }} />
          </div>
        </div>
      </aside>
      <section className={clsx("min-w-0 flex-1 flex-col", detail ? "flex" : "max-md:hidden md:flex")}>
        <button onClick={() => setDetail(false)} className="flex items-center gap-1 px-4 pt-3 text-sm text-zinc-500 md:hidden">
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
  const { project } = useShell();
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
      <div className="mx-auto max-w-3xl space-y-4 px-4 py-4 sm:px-8 sm:py-6">
        <div className="flex items-center gap-3">
          {editing ? (
            <Input autoFocus value={title} placeholder={t("notes.titlePlaceholder")} onChange={(e) => setTitle(e.target.value)} className="h-10 text-lg font-semibold" />
          ) : (
            <h1 className="flex-1 text-xl font-semibold">{note?.title}</h1>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Toggle checked={pinned} onChange={togglePin} label={t("notes.pinned")} />
          <span className="hidden text-xs text-zinc-500 sm:inline">{t("notes.pinnedHint")}</span>
          <div className="flex-1" />
          {editing ? (
            <>
              {note && <Button onClick={() => setEditing(false)}>{t("common.cancel")}</Button>}
              <Button variant="primary" onClick={save} disabled={!title.trim()}>
                {t("common.save")}
              </Button>
            </>
          ) : (
            <>
              <Button onClick={() => setEditing(true)}>{t("common.edit")}</Button>
              <Button variant="danger" icon={<Trash2 className="size-3.5" />} onClick={remove} aria-label={t("common.delete")} />
            </>
          )}
        </div>
        <ErrorText error={error} />
        {editing ? (
          <Textarea rows={20} value={content} onChange={(e) => setContent(e.target.value)} placeholder={t("notes.contentPlaceholder")} className="font-mono text-[13px]" />
        ) : (
          <Markdown>{note?.content || ""}</Markdown>
        )}
      </div>
    </div>
  );
}
