import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { CheckCircle2, Circle, FileText, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { qk, useTodos } from "../../api/queries";
import type { Agent, Todo, TodoState } from "../../api/types";
import { Markdown } from "../../components/Markdown";
import { Button, EmptyState, Input, Modal, Select, Spinner, Textarea } from "../../components/ui";
import { timeAgo } from "../../lib/format";
import { useShell } from "../layout/shell-context";

const STATES: TodoState[] = ["open", "done", "archived"];

function useRefresh() {
  const qc = useQueryClient();
  const { project } = useShell();
  return () => {
    qc.invalidateQueries({ queryKey: ["todos"] });
    qc.invalidateQueries({ queryKey: qk.agents(project.id) });
  };
}

export function TodoDialog({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const { t } = useTranslation();
  const { canWrite } = useShell();
  const [state, setState] = useState<TodoState>("open");

  return (
    <Modal open onClose={onClose} width="max-w-3xl" title={t("todos.title", { name: agent.name })}>
      <div className="mb-4 flex gap-1 rounded-sm bg-sunk p-1">
        {STATES.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setState(s)}
            className={clsx(
              "flex-1 rounded-sm px-3 py-1.5 font-medium transition-colors",
              state === s ? "bg-bg" : "text-fg-3 hover:text-fg",
            )}
          >
            {t(`todos.${s}`)}
            {s === "open" && agent.open_todos > 0 && <span className="ml-1.5 text-fg-3">{agent.open_todos}</span>}
          </button>
        ))}
      </div>
      {state === "open" ? canWrite && <NewTodo agentId={agent.id} /> : <p className="mb-3 text-fg-3">{t("todos.doneHint")}</p>}
      <TodoList key={state} agentId={agent.id} state={state} />
    </Modal>
  );
}

function NewTodo({ agentId }: { agentId: number }) {
  const { t } = useTranslation();
  const refresh = useRefresh();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const add = async () => {
    if (!title.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`/agents/${agentId}/todos`, { title: title.trim(), description });
      setTitle("");
      setDescription("");
      refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-4 space-y-2 rounded-sm p-3 ring-1 ring-line">
      <Input value={title} placeholder={t("todos.newTitle")} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
      {title && (
        <>
          <Textarea rows={4} value={description} placeholder={t("todos.newDescription")} onChange={(e) => setDescription(e.target.value)} />
          <div className="flex justify-end">
            <Button size="sm" variant="primary" icon={<Plus className="size-3.5" />} loading={busy} onClick={add}>
              {t("todos.add")}
            </Button>
          </div>
        </>
      )}
      {error && <p className="text-you-fg">{error}</p>}
    </div>
  );
}

// Paged: only the first page is loaded, more on demand, so long lists stay fast.
function TodoList({ agentId, state }: { agentId: number; state: TodoState }) {
  const { t } = useTranslation();
  const todos = useTodos(agentId, state);
  if (todos.isPending) {
    return (
      <div className="flex justify-center py-6">
        <Spinner />
      </div>
    );
  }
  const pages = todos.data?.pages ?? [];
  const items = pages.flatMap((p) => p.items);
  const left = (pages[0]?.total ?? 0) - items.length;
  if (!items.length) return <EmptyState title={t("todos.empty")} />;
  return (
    <div className="space-y-1">
      {items.map((todo) => (
        <TodoItem key={todo.id} todo={todo} />
      ))}
      {left > 0 && (
        <Button size="sm" variant="ghost" className="w-full" loading={todos.isFetchingNextPage} onClick={() => todos.fetchNextPage()}>
          {t("todos.loadMore", { count: left })}
        </Button>
      )}
    </div>
  );
}

function TodoItem({ todo }: { todo: Todo }) {
  const { t } = useTranslation();
  const { agents, agentById, canWrite } = useShell();
  const refresh = useRefresh();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(todo.title);
  const [description, setDescription] = useState(todo.description);
  const [error, setError] = useState<string | null>(null);
  const done = todo.done_at !== null;
  const creator = agentById(todo.created_by_agent_id);

  const patch = async (body: Partial<{ title: string; description: string; done: boolean; agent_id: number }>) => {
    setError(null);
    try {
      await api.patch(`/todos/${todo.id}`, body);
      refresh();
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    }
  };

  const remove = async () => {
    if (!confirm(t("todos.confirmDelete"))) return;
    try {
      await api.del(`/todos/${todo.id}`);
      refresh();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const save = async () => {
    if (title.trim() && (await patch({ title: title.trim(), description }))) setEditing(false);
  };

  const startEditing = () => {
    setTitle(todo.title);
    setDescription(todo.description);
    setEditing(true);
  };

  return (
    <div className="rounded-sm px-2 py-1.5 hover:bg-sunk">
      <div className="flex items-start gap-2">
        {canWrite && <button
          type="button"
          aria-label={done ? t("todos.reopen") : t("todos.markDone")}
          title={done ? t("todos.reopen") : t("todos.markDone")}
          onClick={() => patch({ done: !done })}
          className={clsx("mt-0.5 shrink-0", done ? "text-ok" : "text-fg-3 hover:text-fg")}
        >
          {done ? <CheckCircle2 className="size-4.5" /> : <Circle className="size-4.5" />}
        </button>}
        <button type="button" onClick={() => setOpen(!open)} className="min-w-0 flex-1 text-left">
          <span className={clsx(done && "text-fg-3 line-through")}>{todo.title}</span>
          {todo.description.trim() && !open && <FileText className="ml-1.5 inline size-3.5 text-fg-3" />}
          {todo.done_at && <span className="ml-2 text-fg-3">{t("todos.doneOn", { when: timeAgo(todo.done_at) })}</span>}
        </button>
      </div>
      {open && (
        <div className="mt-2 ml-6.5 space-y-3">
          {editing && canWrite ? (
            <div className="space-y-2">
              <Input value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
              <Textarea rows={8} value={description} placeholder={t("todos.newDescription")} onChange={(e) => setDescription(e.target.value)} />
              <div className="flex justify-end gap-2">
                <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                  {t("todos.cancel")}
                </Button>
                <Button size="sm" variant="primary" onClick={save}>
                  {t("todos.save")}
                </Button>
              </div>
            </div>
          ) : (
            <>
              {todo.description.trim() && <Markdown>{todo.description}</Markdown>}
              <div className="flex flex-wrap items-center gap-2 text-fg-3">
                <span className="flex-1">
                  {creator ? t("todos.byAgent", { name: creator.name }) : t("todos.byUser")} · {timeAgo(todo.created_at)}
                </span>
                {canWrite && (
                  <>
                    <Button size="sm" variant="ghost" icon={<Pencil className="size-3.5" />} onClick={startEditing}>
                      {t("todos.edit")}
                    </Button>
                    <Select aria-label={t("todos.moveTo")} value={todo.agent_id} onChange={(e) => patch({ agent_id: Number(e.target.value) })} className="w-40">
                      {agents.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.name}
                        </option>
                      ))}
                    </Select>
                    <Button size="sm" variant="danger" icon={<Trash2 className="size-3.5" />} onClick={remove}>
                      {t("todos.delete")}
                    </Button>
                  </>
                )}
              </div>
            </>
          )}
          {error && <p className="text-you-fg">{error}</p>}
        </div>
      )}
    </div>
  );
}
