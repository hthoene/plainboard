import clsx from "clsx";
import { useEffect, useRef } from "react";
import { GitBranch } from "lucide-react";
import { useTranslation } from "react-i18next";
import { AgentSquare, Badge } from "../../components/ui";
import type { Agent, FlowBranch } from "../../api/types";

/** A branch that does not land until its author hands it over again or the manager releases it. */
export function Held({ branch }: { branch?: Pick<FlowBranch, "held" | "held_reason"> }) {
  const { t } = useTranslation();
  if (!branch?.held) return null;
  const hint = t(`flow.held.${branch.held}`) + (branch.held_reason ? ` (${branch.held_reason})` : "");
  return (
    <span title={hint} data-part="flow-held" className="shrink-0">
      <Badge tone="amber">{t("flow.held.label")}</Badge>
    </span>
  );
}

export function Who({ agent, className }: { agent?: Agent; className?: string }) {
  if (!agent) return null;
  return (
    <span className={clsx("inline-flex min-w-0 items-center gap-1.5", className)}>
      <AgentSquare agent={agent} />
      <span className="truncate">{agent.name}</span>
    </span>
  );
}

export function Branch({ name, className }: { name: string; className?: string }) {
  return (
    <span data-part="branch" className={clsx("flex min-w-0 max-w-full items-center gap-1 font-mono text-fg-2", className)}>
      <GitBranch className="size-3.5 shrink-0 text-fg-3" />
      <span className="truncate">{name}</span>
    </span>
  );
}

/** Time against the typical duration of a check run. The bar stays full when it takes longer.
 * It grows smoothly: one linear animation over the typical duration, set to the elapsed time and only
 * corrected when the clock drifts by more than a second. With reduced motion it moves with the 1 s tick. */
export function Progress({ elapsed, typical, className }: { elapsed: number; typical: number; className?: string }) {
  const pct = Math.min(100, (elapsed / Math.max(typical, 1)) * 100);
  const duration = Math.max(typical, 1) * 1000;
  const bar = useRef<HTMLDivElement>(null);
  const anim = useRef<Animation | null>(null);
  useEffect(() => {
    const el = bar.current;
    if (!el?.animate || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const a = el.animate([{ width: "0%" }, { width: "100%" }], { duration, easing: "linear", fill: "forwards" });
    anim.current = a;
    return () => {
      a.cancel();
      anim.current = null;
    };
  }, [duration]);
  useEffect(() => {
    const a = anim.current;
    if (!a) return;
    const target = Math.min(elapsed * 1000, duration);
    if (Math.abs(Number(a.currentTime ?? 0) - target) > 1000) a.currentTime = target;
  }, [elapsed, duration]);
  return (
    <div className={clsx("h-1.5 w-full overflow-hidden rounded-sm bg-line", className)} data-part="progress" role="progressbar" aria-valuenow={Math.round(pct)}>
      <div ref={bar} className="flow-stripes h-full rounded-sm bg-fg-2" style={{ width: `${pct}%` }} />
    </div>
  );
}

/** The state mark of DESIGN.md's fixed cell: filled = working, hollow = waits or in the background, diamond = needs someone, check = done. */
export function Mark({ kind, className }: { kind: "run" | "plan" | "done" | "you"; className?: string }) {
  return <span className={clsx("mk", className, kind === "run" && "mk-run", kind === "plan" && "mk-plan", kind === "done" && "mk-done", kind === "you" && "mk-you")} />;
}
