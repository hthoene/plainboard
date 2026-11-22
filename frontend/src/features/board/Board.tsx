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
import { Archive, ChevronDown, Plus, Search } from "lucide-react";
import { useMemo, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useTasks } from "../../api/queries";
import type { Task, TaskStatus } from "../../api/types";
import { Button, ChatPanelToggle, IconButton, Input, Spinner } from "../../components/ui";
import { useMediaQuery, useStored } from "../../lib/hooks";
import { useShell } from "../layout/shell-context";
import { ArchiveDialog } from "./ArchiveDialog";
import { TaskCard } from "./TaskCard";

export const STATUSES: TaskStatus[] = ["backlog", "todo", "in_progress", "review", "done"];

const taskKey = (id: number) => `t-${id}`;
const columnKey = (s: TaskStatus) => `c-${s}`;

export function Board() {
  const { t } = useTranslation();
  const { project, canWrite } = useShell();
  const qc = useQueryClient();
  const tasks = useTasks(project.id);
  // Side by side from here; below, the statuses stack and can be folded.
  const columnsLayout = useMediaQuery("(min-width: 768px)");
  const [shut, setShut] = useStored<TaskStatus[]>("plainboard.boardShut", ["backlog", "done"]);
  const [filter, setFilter] = useState("");
  const [activeId, setActiveId] = useState<number | null>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [adding, setAdding] = useState<TaskStatus | null>(null);
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    // Long-press to drag on touch screens, so swiping still scrolls the board.
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
    if (!canWrite) return setActiveId(null);  // a viewer's card goes back where it was
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
  const empty = (tasks.data ?? []).length === 0;
  // While filtering, or while dragging, every status stays open: nothing matching may hide.
  const folded = (status: TaskStatus) => !columnsLayout && !filter.trim() && activeId === null && adding !== status && shut.includes(status);
  const toggle = (status: TaskStatus) => setShut(shut.includes(status) ? shut.filter((s) => s !== status) : [...shut, status]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-12 shrink-0 items-center gap-2 px-4">
        <h1 className="mr-2 font-extrabold max-sm:hidden">{t("nav.board")}</h1>
        <div className="relative min-w-0 flex-1 sm:max-w-56">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-3" />
          <Input type="search" className="pl-8" placeholder={t("board.filter")} aria-label={t("board.filter")} value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        <div className="hidden flex-1 sm:block" />
        <Button variant="ghost" className="max-sm:w-11 max-sm:px-0" icon={<Archive className="size-4" />} aria-label={t("archive.title")} onClick={() => setArchiveOpen(true)}>
          <span className="label-line max-sm:hidden">{t("archive.title")}</span>
        </Button>
        {canWrite && (
          <Button variant="primary" className="max-sm:w-11 max-sm:px-0" icon={<Plus className="size-4" />} aria-label={t("board.addTicket")} onClick={() => setAdding("backlog")}>
            <span className="label-line max-sm:hidden">{t("board.addTicket")}</span>
          </Button>
        )}
        <ChatPanelToggle where="view" divider />
      </div>
      {archiveOpen && <ArchiveDialog onClose={() => setArchiveOpen(false)} />}
      {empty && adding === null && <p className="mx-4 mb-3 max-w-prose text-fg-2">{t("board.hint")}</p>}
      <DndContext sensors={sensors} collisionDetection={closestCorners} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={() => setActiveId(null)}>
        <div data-part="board" className="scroll-thin min-h-0 flex-1 overflow-y-auto border-t border-line md:grid md:auto-cols-[minmax(172px,1fr)] md:grid-flow-col md:grid-rows-[minmax(0,1fr)] md:overflow-x-auto md:overflow-y-hidden md:border-t md:border-line">
          {STATUSES.map((status) => (
            <Column
              key={status}
              status={status}
              tasks={columns[status]}
              folded={folded(status)}
              foldable={!columnsLayout}
              onToggle={() => toggle(status)}
              adding={adding === status}
              setAdding={(on) => setAdding(on ? status : null)}
            />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>{active ? <TaskCard task={active} overlay /> : null}</DragOverlay>
      </DndContext>
    </div>
  );
}

function Column({
  status,
  tasks,
  folded,
  foldable,
  onToggle,
  adding,
  setAdding,
}: {
  status: TaskStatus;
  tasks: Task[];
  folded: boolean;
  foldable: boolean;
  onToggle: () => void;
  adding: boolean;
  setAdding: (on: boolean) => void;
}) {
  const { t } = useTranslation();
  const { setNodeRef, isOver } = useDroppable({ id: columnKey(status) });
  const { project, canWrite } = useShell();
  const qc = useQueryClient();

  const archiveDone = async () => {
    await api.post(`/projects/${project.id}/tasks/archive`);
    qc.invalidateQueries({ queryKey: qk.tasks(project.id) });
  };

  return (
    <section data-part="column" data-status={status} className="flex min-h-0 min-w-0 flex-col md:border-r md:border-line md:last:border-r-0">
      {/* Title (folds on phones), then fixed cells from the right: add, count; archive sits before them. */}
      <header data-part="column-head" className="sticky top-0 z-10 flex h-11 shrink-0 items-center rule-b bg-bg pr-1 pl-4 md:static md:mx-2.5 md:h-[38px] md:px-0 md:touch:h-11">
        {foldable ? (
          <button type="button" onClick={onToggle} aria-expanded={!folded} className="flex h-full min-w-0 flex-1 items-center gap-1.5 text-left">
            <h2 className="caps truncate">{t(`taskStatus.${status}`)}</h2>
            <ChevronDown className={clsx("size-4 shrink-0 text-fg-3 transition-transform", folded && "-rotate-90")} />
          </button>
        ) : (
          <h2 className="caps min-w-0 flex-1 truncate">{t(`taskStatus.${status}`)}</h2>
        )}
        {canWrite && status === "done" && tasks.length > 0 && (
          <IconButton label={t("archive.archiveDone")} onClick={archiveDone}>
            <Archive className="size-4" />
          </IconButton>
        )}
        <span className="w-7 shrink-0 text-right text-fg-3">{tasks.length}</span>
        {canWrite && (
          <IconButton label={t("board.addTicket")} onClick={() => setAdding(true)} className="ml-1 md:-mr-1.5">
            <Plus className="size-4" />
          </IconButton>
        )}
      </header>
      {!folded && (
        <div ref={setNodeRef} data-part="column-body" data-over={isOver || undefined} className={clsx("min-h-6 md:scroll-thin md:min-h-0 md:flex-1 md:overflow-y-auto", isOver && "bg-sunk")}>
          {adding && <NewTicket status={status} onDone={() => setAdding(false)} />}
          <SortableContext items={tasks.map((x) => taskKey(x.id))} strategy={verticalListSortingStrategy}>
            {tasks.map((task) => (
              <TaskCard key={task.id} task={task} done={status === "done"} />
            ))}
          </SortableContext>
        </div>
      )}
    </section>
  );
}

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
    <div data-part="new-ticket" className="border-b border-line bg-sunk px-4 py-2.5 ring-2 ring-fg ring-inset md:px-2.5">
      <textarea
        autoFocus
        rows={2}
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={onKey}
        onBlur={() => !title.trim() && onDone()}
        placeholder={t("board.newTicketPlaceholder")}
        aria-label={t("board.newTicketPlaceholder")}
        className="block w-full resize-none bg-transparent outline-none"
      />
      <p className="text-fg-3 touch:hidden">{t("board.newTicketKeys")}</p>
    </div>
  );
}
