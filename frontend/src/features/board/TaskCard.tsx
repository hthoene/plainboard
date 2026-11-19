import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import clsx from "clsx";
import { Globe, MessageSquare, Paperclip } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Priority, Task } from "../../api/types";
import { AgentSquare } from "../../components/ui";
import { useSince } from "../../lib/time";
import { useRunningRuns } from "../layout/RunningNow";
import { useShell } from "../layout/shell-context";

/** A tag on a ticket row: one outlined mark of fixed height, so tags read as a set. */
const TAG = "inline-flex h-5 max-w-full items-center gap-1.5 rounded-sm px-1.5 ring-1 ring-inset";

/** Priority is rank, so it is told by weight, case and reversal, not by colour. It is always the
 * first tag of a row; medium is not shown. */
export const priorityStyle: Record<Priority, string> = {
  low: "text-fg-3 ring-line",
  medium: "text-fg-2 ring-line",
  high: "font-semibold text-fg ring-fg",
  urgent: "plate caps font-extrabold ring-fg",
};

/** OKLCH hue per label colour; lightness and chroma come from the theme (`--sq-l`, `--sq-c`). */
const labelHues: Record<string, number> = { red: 25, orange: 50, amber: 80, green: 150, teal: 190, blue: 250, indigo: 275, purple: 305, pink: 350 };

/** A label: its name, led by a small square in its colour if it has one. */
export function Label({ colors, label, className, tag }: { colors: Record<string, string>; label: string; className?: string; tag?: boolean }) {
  const hue = labelHues[colors[label]];
  return (
    <span data-part={tag ? "tag" : undefined} data-hue={hue} style={hue === undefined ? undefined : ({ "--h": hue } as CSSProperties)} className={clsx("inline-flex min-w-0 items-center gap-1.5", className)}>
      {hue !== undefined && <span className="sq-hue size-[7px] shrink-0 rounded-[1px]" />}
      <span className="truncate">{label}</span>
    </span>
  );
}

/** One ticket as a ruled row in three zones that never mix: the title (with its number); what
 * waits for the user; then the tags at the left (priority first, each label an outlined mark) and
 * the facts at the right as one group in a fixed order: comments, files, public, who works on it,
 * for how long. The facts move to a line of their own, still at the right, when the tags fill
 * theirs. */
export function TaskCard({ task, overlay, done }: { task: Task; overlay?: boolean; done?: boolean }) {
  const { t } = useTranslation();
  const { project, openTask, agentById, canWrite } = useShell();
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: `t-${task.id}`, disabled: overlay || !canWrite });
  const running = useRunningRuns(project.id);
  const since = useSince();

  const working = task.active_agent_ids.map((id) => agentById(id)).filter((a) => a !== undefined);
  const forYou = !!task.user_action || task.open_decisions > 0;
  const mark = forYou ? "mk-you" : working.length > 0 ? "mk-run" : task.manager_active ? "mk-plan" : done ? "mk-done" : "";
  // The turn on this ticket: a member working on it, or the manager planning it.
  const run = [...running.values()].find((r) => r.task_id === task.id) ?? working.map((a) => running.get(a.id)).find((r) => r !== undefined);
  const active = working.length > 0 || task.manager_active;
  const hasCounts = task.comment_count > 0 || task.attachment_count > 0 || !!task.customer_title;
  const hasTags = task.priority !== "medium" || task.labels.length > 0;

  return (
    <article
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      {...attributes}
      {...listeners}
      data-part="ticket"
      data-for-you={forYou || undefined}
      data-overlay={overlay || undefined}
      onClick={() => !overlay && openTask(task.id)}
      className={clsx(
        "grid cursor-pointer grid-cols-[9px_minmax(0,1fr)] gap-x-2 border-b border-line px-4 py-3 select-none md:px-2.5 md:py-2.5",
        forYou ? "bg-you-bg" : overlay ? "bg-sunk" : "hover:bg-sunk",
        isDragging && "opacity-40",
        overlay && "shadow-lg ring-1 ring-line-2",
      )}
    >
      <span className={clsx("mk mt-[7px] md:mt-[6px]", mark)} title={task.manager_active && !working.length ? t("board.managerPlanning") : undefined} />
      <div className="min-w-0">
        <h3 className="font-medium text-pretty break-words">
          <span className="mr-1.5 font-normal text-fg-3">#{task.number}</span>
          {task.title}
        </h3>
        {task.user_action && (
          <p className="mt-1.5 break-words">
            <ForYouPlate>{t("board.userAction")}</ForYouPlate>
            <span className="line-clamp-3">{task.user_action}</span>
          </p>
        )}
        {task.open_decisions > 0 && (
          <p className="mt-1.5">
            <ForYouPlate>{t("decision.title")}</ForYouPlate>
          </p>
        )}
        {(hasTags || hasCounts || active) && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            {task.priority !== "medium" && <span data-part="tag" data-priority={task.priority} className={clsx(TAG, priorityStyle[task.priority])}>{t(`priority.${task.priority}`)}</span>}
            {task.labels.map((label) => (
              <Label key={label} tag colors={project.label_colors} label={label} className={clsx(TAG, "text-fg-2 ring-line")} />
            ))}
            {(hasCounts || active) && (
              <span data-part="facts" className="ml-auto inline-flex min-h-5 max-w-full flex-wrap items-center justify-end gap-x-2.5 gap-y-1 pl-1.5 whitespace-nowrap text-fg-3">
                {task.comment_count > 0 && (
                  <span className="inline-flex items-center gap-1" title={t("ticket.comments")}>
                    <MessageSquare className="size-3.5" />
                    {task.comment_count}
                  </span>
                )}
                {task.attachment_count > 0 && (
                  <span className="inline-flex items-center gap-1" title={t("ticket.attachments")}>
                    <Paperclip className="size-3.5" />
                    {task.attachment_count}
                  </span>
                )}
                {task.customer_title && (
                  <span className="inline-flex" title={task.customer_title}>
                    <Globe className="size-3.5" />
                  </span>
                )}
                {active && (
                  <span className="inline-flex items-center gap-1.5 text-fg-2" title={working.map((a) => a.name).join(", ") || t("board.managerPlanning")}>
                    {working.map((a) => (
                      <AgentSquare key={a.id} agent={a} />
                    ))}
                    {run ? since(run.started_at ?? run.created_at) : !working.length && t("shell.planning")}
                  </span>
                )}
              </span>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

/** Reversed vermilion plate that names what waits for the user. */
export function ForYouPlate({ children }: { children: ReactNode }) {
  return <b data-part="you-plate" className="caps mr-1.5 inline-block rounded-sm bg-you px-1.5 font-extrabold whitespace-nowrap text-on-you">{children}</b>;
}
