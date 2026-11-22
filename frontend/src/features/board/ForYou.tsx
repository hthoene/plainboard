import clsx from "clsx";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useTasks } from "../../api/queries";
import { useShell } from "../layout/shell-context";

/** The one vermilion element of a screen: how many tickets wait for the user, with the list
 * behind it. Hidden while nothing waits. `side` fills the sidebar, `rail` is the count alone. */
export function ForYou({ variant = "plate" }: { variant?: "plate" | "side" | "rail" }) {
  const { t } = useTranslation();
  const { project, openTask } = useShell();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const tasks = (useTasks(project.id).data ?? []).filter((task) => task.open_decisions > 0 || task.user_action).sort((a, b) => a.number - b.number);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!tasks.length) return null;
  return (
    <div ref={ref} className={clsx("relative shrink-0", variant === "side" && "w-full")}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        title={t("chat.openQuestions")}
        data-part="you"
        onClick={() => setOpen(!open)}
        className={clsx(
          "caps inline-flex h-8 items-center gap-2 rounded-sm bg-you font-extrabold whitespace-nowrap text-on-you touch:h-11",
          variant === "side" ? "w-full px-2" : variant === "rail" ? "mx-auto w-8 justify-center tabular-nums" : "px-2.5",
        )}
      >
        {variant !== "rail" && <span className="mk bg-on-you [transform:rotate(45deg)_scale(0.86)]" />}
        {variant === "rail" ? tasks.length : t("shell.forYou", { n: tasks.length })}
      </button>
      {open && (
        <div role="menu" data-part="menu" className={clsx("absolute top-full z-40 mt-1 w-72 max-w-[calc(100vw-1.5rem)] rounded-sm bg-bg p-1 shadow-lg ring-1 ring-line-2", variant === "plate" ? "right-0" : "left-0")}>
          {tasks.map((task) => (
            <button
              key={task.id}
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                openTask(task.id);
              }}
              className="flex w-full items-baseline gap-2 rounded-sm px-2 py-2 text-left hover:bg-sunk"
            >
              <span className="text-fg-3">#{task.number}</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{task.title}</span>
                <span className="line-clamp-2 text-fg-2">{task.user_action || t("decision.title")}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
