import { Hand } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useTasks } from "../../api/queries";
import { useShell } from "../layout/shell-context";

/** Clickable numbers of the tickets that have a question for the user. */
export function OpenQuestions() {
  const { t } = useTranslation();
  const { project, openTask } = useShell();
  const tasks = (useTasks(project.id).data ?? []).filter((task) => task.open_decisions > 0).sort((a, b) => a.number - b.number);
  if (!tasks.length) return null;
  return (
    <div role="group" aria-label={t("chat.openQuestions")} title={t("chat.openQuestions")} className="flex flex-wrap items-center justify-end gap-1 text-amber-600 dark:text-amber-400">
      <Hand className="size-3.5 shrink-0" aria-hidden />
      {tasks.map((task) => (
        <button
          key={task.id}
          type="button"
          title={task.title}
          onClick={() => openTask(task.id)}
          className="rounded-md bg-amber-500/15 px-1.5 py-0.5 text-[11px] font-medium hover:bg-amber-500/25"
        >
          #{task.number}
        </button>
      ))}
    </div>
  );
}
