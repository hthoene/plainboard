import {
  closestCorners,
  DndContext,
  DragOverlay,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { arrayMove, SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Plus, Search } from "lucide-react";
import { useMemo, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useTasks } from "../../api/queries";
import type { Task, TaskStatus } from "../../api/types";
import { IconButton, Input, Spinner } from "../../components/ui";
import { useShell } from "../layout/shell-context";
import { TaskCard } from "./TaskCard";

export const STATUSES: TaskStatus[] = ["backlog", "todo", "in_progress", "review", "done"];

const taskKey = (id: number) => `t-${id}`;
const columnKey = (s: TaskStatus) => `c-${s}`;

export function Board() {
  const { t } = useTranslation();
  const { project } = useShell();
  const qc = useQueryClient();
  const tasks = useTasks(project.id);
  const [filter, setFilter] = useState("");
  const [activeId, setActiveId] = useState<number | null>(null);
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    // Long-press to drag on touch screens, so horizontal swiping still scrolls the board.
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const columns = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const visible = (tasks.data ?? []).filter(
      (task) =>
        !needle ||
        task.title.toLowerCase().includes(needle) ||
        `#${task.number}` === needle ||
        task.labels.some((l) => l.toLowerCase().includes(needle)),
    );
    const byStatus = Object.fromEntries(STATUSES.map((s) => [s, [] as Task[]])) as Record<TaskStatus, Task[]>;
    for (const task of visible) byStatus[task.status]?.push(task);
    for (const s of STATUSES) byStatus[s].sort((a, b) => a.position - b.position);
    return byStatus;
  }, [tasks.data, filter]);

  const findTask = (key: string | number) => tasks.data?.find((task) => taskKey(task.id) === key);

  const onDragStart = (e: DragStartEvent) => setActiveId(findTask(e.active.id)?.id ?? null);

  const onDragEnd = async (e: DragEndEvent) => {
    setActiveId(null);
    const task = findTask(e.active.id);
    if (!task || !e.over) return;
    const overKey = String(e.over.id);
    const overTask = findTask(overKey);
    const status = overTask ? overTask.status : (overKey.replace("c-", "") as TaskStatus);

    let list = columns[status].filter((x) => x.id !== task.id);
    let index = overTask ? list.findIndex((x) => x.id === overTask.id) : list.length;
    if (overTask && status === task.status) {
      const ordered = arrayMove(columns[status], columns[status].indexOf(task), columns[status].indexOf(overTask));
      list = ordered.filter((x) => x.id !== task.id);
      index = ordered.indexOf(task);
    }
    if (index < 0) index = list.length;
    const prev = list[index - 1]?.position;
    const next = list[index]?.position;
    const position = prev === undefined && next === undefined ? 1024 : prev === undefined ? next! - 1024 : next === undefined ? prev + 1024 : (prev + next) / 2;
    if (status === task.status && position === task.position) return;

    qc.setQueryData<Task[]>(qk.tasks(project.id), (old) => old?.map((x) => (x.id === task.id ? { ...x, status, position } : x)));
    try {
      await api.patch(`/tasks/${task.id}`, { status, position });
    } finally {
      qc.invalidateQueries({ queryKey: qk.tasks(project.id) });
    }
  };

  if (tasks.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }

  const active = activeId !== null ? tasks.data?.find((x) => x.id === activeId) : undefined;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 px-3 pt-3 pb-2 sm:px-4">
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute top-2 left-2.5 size-4 text-zinc-400" />
          <Input className="pl-8" placeholder={t("board.filter")} value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        <p className="hidden text-xs text-zinc-500 lg:block">{t("board.hint")}</p>
      </div>
      <DndContext sensors={sensors} collisionDetection={closestCorners} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={() => setActiveId(null)}>
        <div className="scroll-thin flex min-h-0 flex-1 snap-x snap-mandatory gap-3 overflow-x-auto px-3 pb-3 sm:snap-none sm:px-4 sm:pb-4">
          {STATUSES.map((status) => (
            <Column key={status} status={status} tasks={columns[status]} />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>{active ? <TaskCard task={active} overlay /> : null}</DragOverlay>
      </DndContext>
    </div>
  );
}

function Column({ status, tasks }: { status: TaskStatus; tasks: Task[] }) {
  const { t } = useTranslation();
  const { setNodeRef, isOver } = useDroppable({ id: columnKey(status) });
  const [adding, setAdding] = useState(false);

  return (
    <section className="flex w-[85vw] shrink-0 snap-center flex-col rounded-xl bg-zinc-100/70 sm:w-auto sm:max-w-80 sm:min-w-60 sm:flex-1 sm:shrink dark:bg-zinc-900/60">
      <header className="flex items-center gap-2 px-3 pt-2.5 pb-1.5">
        <span className={clsx("size-2 rounded-full", statusColor[status])} />
        <h2 className="text-[13px] font-semibold text-zinc-700 dark:text-zinc-200">{t(`taskStatus.${status}`)}</h2>
        <span className="text-xs text-zinc-400">{tasks.length}</span>
        <div className="flex-1" />
        <IconButton label={t("board.addTicket")} onClick={() => setAdding(true)}>
          <Plus className="size-4" />
        </IconButton>
      </header>
      <div ref={setNodeRef} className={clsx("scroll-thin flex min-h-16 flex-1 flex-col gap-2 overflow-y-auto px-2 pb-2", isOver && "rounded-lg bg-indigo-50/60 dark:bg-indigo-950/20")}>
        {adding && <NewTicket status={status} onDone={() => setAdding(false)} />}
        <SortableContext items={tasks.map((x) => taskKey(x.id))} strategy={verticalListSortingStrategy}>
          {tasks.map((task) => (
            <TaskCard key={task.id} task={task} />
          ))}
        </SortableContext>
      </div>
    </section>
  );
}

export const statusColor: Record<TaskStatus, string> = {
  backlog: "bg-zinc-400",
  todo: "bg-sky-500",
  in_progress: "bg-indigo-500",
  review: "bg-amber-500",
  done: "bg-emerald-500",
};

function NewTicket({ status, onDone }: { status: TaskStatus; onDone: () => void }) {
  const { t } = useTranslation();
  const { project, openTask } = useShell();
  const qc = useQueryClient();
  const [title, setTitle] = useState("");

  const create = async (open: boolean) => {
    if (!title.trim()) return onDone();
    const task = await api.post<Task>(`/projects/${project.id}/tasks`, { title: title.trim(), status });
    qc.invalidateQueries({ queryKey: qk.tasks(project.id) });
    setTitle("");
    if (open) {
      onDone();
      openTask(task.id);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void create(e.metaKey || e.ctrlKey);
    }
    if (e.key === "Escape") onDone();
  };

  return (
    <div className="rounded-lg bg-white p-2 shadow-sm ring-1 ring-indigo-300 dark:bg-zinc-800 dark:ring-indigo-700">
      <textarea
        autoFocus
        rows={2}
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={onKey}
        onBlur={() => !title.trim() && onDone()}
        placeholder={t("board.newTicketPlaceholder")}
        className="w-full resize-none bg-transparent text-sm outline-none placeholder:text-zinc-400"
      />
      <p className="text-[11px] text-zinc-400">{t("board.newTicketKeys")}</p>
    </div>
  );
}
