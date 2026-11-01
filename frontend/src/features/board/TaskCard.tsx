import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import clsx from "clsx";
import { Globe, Hand, Loader2, MessageSquare, Paperclip, UserRound } from "lucide-react";
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

const labelTones: Record<string, string> = {
  gray: "bg-zinc-100 text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300",
  red: "bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200",
  orange: "bg-orange-100 text-orange-800 dark:bg-orange-900/60 dark:text-orange-200",
  amber: "bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200",
  green: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200",
  teal: "bg-teal-100 text-teal-800 dark:bg-teal-900/60 dark:text-teal-200",
  blue: "bg-sky-100 text-sky-800 dark:bg-sky-900/60 dark:text-sky-200",
  indigo: "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/60 dark:text-indigo-200",
  purple: "bg-purple-100 text-purple-800 dark:bg-purple-900/60 dark:text-purple-200",
  pink: "bg-pink-100 text-pink-800 dark:bg-pink-900/60 dark:text-pink-200",
};

export const labelTone = (colors: Record<string, string>, label: string) => labelTones[colors[label]] ?? labelTones.gray;

export function TaskCard({ task, overlay }: { task: Task; overlay?: boolean }) {
  const { t } = useTranslation();
  const { project, openTask, agentById } = useShell();
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
        "group cursor-pointer rounded-lg p-2.5 shadow-sm transition-shadow select-none",
        task.user_action
          ? "bg-amber-50 ring-2 ring-amber-400 dark:bg-amber-950/50 dark:ring-amber-500"
          : "bg-white ring-1 ring-zinc-200/80 hover:ring-zinc-300 dark:bg-zinc-800 dark:ring-zinc-700/70 dark:hover:ring-zinc-600",
        isDragging && "opacity-40",
        overlay && "rotate-1 shadow-xl ring-indigo-300",
      )}
    >
      <div className="flex items-start gap-2">
        <span className="pt-px font-mono text-[11px] text-zinc-400">#{task.number}</span>
        <h3 className="min-w-0 flex-1 text-[13px] leading-snug font-medium text-zinc-800 dark:text-zinc-100">{task.title}</h3>
      </div>
      {task.user_action && (
        <div className="mt-1.5 rounded bg-amber-400/25 px-1.5 py-1 text-[11px] text-amber-950 dark:text-amber-100">
          <div className="flex items-center gap-1 font-semibold">
            <UserRound className="size-3" />
            {t("board.userAction")}
          </div>
          <p className="line-clamp-3">{task.user_action}</p>
        </div>
      )}
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
            <span key={label} className={clsx("rounded px-1.5 py-px text-[11px]", labelTone(project.label_colors, label))}>
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
