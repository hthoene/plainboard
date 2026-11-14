import { useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { qk, useOwnNotes } from "../../api/queries";
import type { Agent, OwnNote } from "../../api/types";
import { Button, EmptyState, Modal, Spinner, Textarea } from "../../components/ui";
import { timeAgo } from "../../lib/format";
import { useShell } from "../layout/shell-context";

const MAX_NOTE = 500;
const BUDGET = 3000; // all notes of one member together (services/own_notes.py)

function useRefresh(agentId: number) {
  const qc = useQueryClient();
  const { project } = useShell();
  return () => {
    qc.invalidateQueries({ queryKey: qk.ownNotes(agentId) });
    qc.invalidateQueries({ queryKey: qk.agents(project.id) });
  };
}

export function OwnNotesDialog({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const { t } = useTranslation();
  const notes = useOwnNotes(agent.id);
  const refresh = useRefresh(agent.id);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const items = notes.data ?? [];
  const used = items.reduce((n, note) => n + note.text.length, 0);

  const add = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`/agents/${agent.id}/notes`, { text: text.trim() });
      setText("");
      refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={onClose} width="max-w-2xl" title={t("ownNotes.title", { name: agent.name })}>
      <p className="mb-3 text-xs text-zinc-500">{t("ownNotes.hint")}</p>
      <div className="mb-4 space-y-2 rounded-lg p-3 ring-1 ring-zinc-200 dark:ring-zinc-800">
        <Textarea rows={2} value={text} maxLength={MAX_NOTE} placeholder={t("ownNotes.new", { name: agent.name })} onChange={(e) => setText(e.target.value)} />
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-zinc-500">{t("ownNotes.used", { used, budget: BUDGET })}</span>
          <Button size="sm" variant="primary" icon={<Plus className="size-3.5" />} loading={busy} disabled={!text.trim()} onClick={add}>
            {t("ownNotes.add")}
          </Button>
        </div>
        {error && <p className="text-sm text-red-600">{error}</p>}
      </div>
      {notes.isPending ? (
        <div className="flex justify-center py-6">
          <Spinner />
        </div>
      ) : items.length ? (
        <div className="space-y-1">
          {items.map((note) => (
            <NoteItem key={note.id} note={note} />
          ))}
        </div>
      ) : (
        <EmptyState title={t("ownNotes.empty")} />
      )}
    </Modal>
  );
}

function NoteItem({ note }: { note: OwnNote }) {
  const { t } = useTranslation();
  const { agentById } = useShell();
  const refresh = useRefresh(note.agent_id);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(note.text);
  const [error, setError] = useState<string | null>(null);
  const creator = agentById(note.created_by_agent_id);

  const run = async (action: () => Promise<unknown>) => {
    setError(null);
    try {
      await action();
      refresh();
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    }
  };

  const save = async () => {
    if (text.trim() && (await run(() => api.patch(`/agent-notes/${note.id}`, { text: text.trim() })))) setEditing(false);
  };

  const remove = () => {
    if (confirm(t("ownNotes.confirmDelete"))) run(() => api.del(`/agent-notes/${note.id}`));
  };

  return (
    <div className="rounded-lg px-2 py-2 hover:bg-zinc-50 dark:hover:bg-zinc-800/40">
      {editing ? (
        <div className="space-y-2">
          <Textarea rows={3} value={text} maxLength={MAX_NOTE} onChange={(e) => setText(e.target.value)} />
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              {t("ownNotes.cancel")}
            </Button>
            <Button size="sm" variant="primary" onClick={save}>
              {t("ownNotes.save")}
            </Button>
          </div>
        </div>
      ) : (
        <>
          <p className="text-sm break-words whitespace-pre-wrap">{note.text}</p>
          <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-zinc-500">
            <span className="flex-1">
              {creator ? t("ownNotes.byAgent", { name: creator.name }) : t("ownNotes.byUser")} · {timeAgo(note.updated_at)}
            </span>
            <Button
              size="sm"
              variant="ghost"
              icon={<Pencil className="size-3.5" />}
              onClick={() => {
                setText(note.text);
                setEditing(true);
              }}
            >
              {t("ownNotes.edit")}
            </Button>
            <Button size="sm" variant="ghost" icon={<Trash2 className="size-3.5" />} onClick={remove}>
              {t("ownNotes.delete")}
            </Button>
          </div>
        </>
      )}
      {error && <p className="mt-1 text-sm text-red-600">{error}</p>}
    </div>
  );
}
