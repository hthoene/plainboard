import clsx from "clsx";
import { Check, ChevronDown, Pencil, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Project } from "../../api/types";

export function ProjectSwitcher({
  projects,
  current,
  onSelect,
  onNew,
  onEdit,
}: {
  projects: Project[];
  current: Project;
  onSelect: (id: number) => void;
  onNew: () => void;
  onEdit: () => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex h-8 max-w-56 items-center gap-1.5 rounded-md px-2 text-sm font-medium hover:bg-zinc-100 dark:hover:bg-zinc-800"
      >
        <span className="truncate">{current.name}</span>
        <ChevronDown className="size-3.5 shrink-0 text-zinc-400" />
      </button>
      {open && (
        <div className="absolute top-9 left-0 z-40 w-64 rounded-lg bg-white p-1 shadow-lg ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-800">
          {projects.map((p) => (
            <button
              key={p.id}
              onClick={() => {
                onSelect(p.id);
                setOpen(false);
              }}
              className={clsx(
                "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-zinc-100 dark:hover:bg-zinc-800",
                p.id === current.id && "font-medium",
              )}
            >
              <span className="flex-1 truncate">{p.name}</span>
              {p.id === current.id && <Check className="size-3.5 text-indigo-600" />}
            </button>
          ))}
          <div className="my-1 border-t border-zinc-100 dark:border-zinc-800" />
          <button
            onClick={() => {
              onEdit();
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            <Pencil className="size-3.5" /> {t("projects.settings")}
          </button>
          <button
            onClick={() => {
              onNew();
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            <Plus className="size-3.5" /> {t("projects.new")}
          </button>
        </div>
      )}
    </div>
  );
}
