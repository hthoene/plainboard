import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import clsx from "clsx";
import { Globe, Hand, Loader2, MessageSquare, Paperclip } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { Priority, Task } from "../../api/types";
import { Badge } from "../../components/ui";
import { useShell } from "../layout/shell-context";

export const priorityStyle: Record<Priority, string> = {
  low: "text-zinc-400",
  medium: "text-sky-600 dark:text-sky-400",
  high: "text-amber-600 dark:text-amber-400",
  urgent: "text-red-600 dark:text-red-400",
};

export function TaskCard({ task, overlay }: { task: Task; overlay?: boolean }) {
  const { t } = useTranslation();
  const { openTask, agentById } = useShell();
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: `t-${task.id}`, disabled: overlay });

  const working = task.active_agent_ids.map((id) => agentById(id)).filter(Boolean);

  return (
    <article
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      {...attributes}
      {...listeners}
      onClick={() => !overlay && openTask(task.id)}
      className={clsx(
        "group cursor-pointer rounded-lg bg-white p-2.5 shadow-sm ring-1 ring-zinc-200/80 transition-shadow select-none hover:ring-zinc-300 dark:bg-zinc-800 dark:ring-zinc-700/70 dark:hover:ring-zinc-600",
        isDragging && "opacity-40",
        overlay && "rotate-1 shadow-xl ring-indigo-300",
      )}
    >
      <div className="flex items-start gap-2">
        <span className="pt-px font-mono text-[11px] text-zinc-400">#{task.number}</span>
        <h3 className="min-w-0 flex-1 text-[13px] leading-snug font-medium text-zinc-800 dark:text-zinc-100">{task.title}</h3>
      </div>
      {task.open_decisions > 0 && (
        <div className="mt-1.5">
          <Badge tone="amber">
            <Hand className="size-3" />
            {t("decision.title")}
          </Badge>
        </div>
      )}
      {(task.labels.length > 0 || task.priority !== "medium") && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          {task.priority !== "medium" && <span className={clsx("text-[11px] font-medium", priorityStyle[task.priority])}>{t(`priority.${task.priority}`)}</span>}
          {task.labels.map((label) => (
            <span key={label} className="rounded bg-zinc-100 px-1.5 py-px text-[11px] text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300">
              {label}
            </span>
          ))}
        </div>
      )}
      {(task.comment_count > 0 || task.attachment_count > 0 || task.customer_title || working.length > 0 || task.manager_active) && (
        <div className="mt-2 flex items-center gap-2.5 text-[11px] text-zinc-400">
          {task.comment_count > 0 && (
            <span className="flex items-center gap-0.5">
              <MessageSquare className="size-3" />
              {task.comment_count}
            </span>
          )}
          {task.attachment_count > 0 && (
            <span className="flex items-center gap-0.5">
              <Paperclip className="size-3" />
              {task.attachment_count}
            </span>
          )}
          {task.customer_title && (
            <span title={task.customer_title}>
              <Globe className="size-3" />
            </span>
          )}
          <div className="flex-1" />
          {task.manager_active && (
            <span className="flex items-center gap-1 text-indigo-500" title={t("board.managerPlanning")}>
              <Loader2 className="size-3 animate-spin" />
            </span>
          )}
          {working.length > 0 && (
            <span className="flex -space-x-1" title={working.map((a) => a!.name).join(", ")}>
              {working.map((a) => (
                <span
                  key={a!.id}
                  className="flex size-4 items-center justify-center rounded-full text-[9px] font-semibold text-white ring-2 ring-white dark:ring-zinc-800"
                  style={{ background: a!.color }}
                >
                  {a!.name[0]?.toUpperCase()}
                </span>
              ))}
            </span>
          )}
        </div>
      )}
    </article>
  );
}
