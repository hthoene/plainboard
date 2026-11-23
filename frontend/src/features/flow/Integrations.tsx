import clsx from "clsx";
import { ArrowRight, Check } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useTaskFlow } from "../../api/queries";
import { useSince } from "../../lib/time";
import { clock, elapsedOf, typicalOf, useFlowModel } from "./flow";
import { Branch, Held, Mark, Progress } from "./parts";

/** The ticket's branches on their way to the base branch, as steps: handed over, check failed, repair, checked, landed. */
export function FlowTicket({ taskId }: { taskId: number }) {
  const { t } = useTranslation();
  const flow = useTaskFlow(taskId).data;
  const m = useFlowModel();
  const since = useSince(1000);
  if (!flow?.length) return null;
  return (
    <section data-part="flow-ticket">
      <h3 className="caps mb-2 text-fg-2">{t("flow.ticket.title")}</h3>
      <div className="space-y-2">
        {flow.map(({ branch, repository, train, steps }) => {
          const typical = typicalOf(m?.repoOf(branch.repository_id));
          return (
            <div key={branch.id} className="rounded-sm ring-1 ring-line">
              <div className="flex items-center gap-2 border-b border-line px-3 py-2">
                <Branch name={branch.branch} className="flex-1" />
                <Held branch={branch} />
                <span className="text-fg-3">{[repository, train ? t("flow.train", { n: train.id }) : null].filter(Boolean).join(" · ")}</span>
              </div>
              <ol className="px-3 py-2">
                {steps.map((s, i) => (
                  <li key={`${s.key}${i}`} className="relative grid grid-cols-[14px_minmax(0,1fr)_auto] items-baseline gap-x-2.5 py-1">
                    {i < steps.length - 1 && <span className="absolute top-[17px] bottom-[-5px] left-[6px] w-px bg-line-2" />}
                    <span className="relative z-1 flex justify-center bg-bg">
                      {s.state === "done" && s.key !== "failed" ? (
                        <Check className="size-3.5 text-ok" />
                      ) : s.key === "failed" || s.key === "stuck" ? (
                        <span className="mk mk-you" />
                      ) : s.state === "now" ? (
                        <span className="mk mk-run flow-pulse" />
                      ) : (
                        <span className="mk mk-plan opacity-50" />
                      )}
                    </span>
                    <span className={clsx(s.state === "todo" ? "text-fg-3" : s.state === "now" || s.key === "failed" ? "font-semibold" : "text-fg-2")}>
                      {t(`flow.step.${s.key}`)}
                      {s.key === "checking" && s.state === "done" && train && train.check_seconds > 0 && <span className="font-normal text-fg-3"> · {clock(train.check_seconds)}</span>}
                      {s.detail && <span className="font-normal text-fg-3"> · {s.detail}</span>}
                    </span>
                    <span className="text-fg-3">{s.at ? since(s.at) : ""}</span>
                    {s.key === "checking" && s.state === "now" && train && m && typical && (
                      <span className="col-start-2 col-end-4 mt-1 mb-0.5">
                        <Progress elapsed={elapsedOf(train, m.now)} typical={typical} />
                        <span className="mt-1 flex justify-between text-fg-3">
                          <span>{clock(elapsedOf(train, m.now))}</span>
                          <span>~{clock(typical)}</span>
                        </span>
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/** What a team member does right now: the turn with its ticket, the jobs it left running, the branch that waits or is checked. */
export function FlowMember({ agentId }: { agentId: number }) {
  const { t } = useTranslation();
  const m = useFlowModel();
  const since = useSince(1000);
  if (!m) return null;
  const work = m.live.work.find((w) => w.agent_id === agentId);
  const branch = m.live.branches.find((b) => b.agent_id === agentId && (b.status === "waiting" || b.status === "checking" || b.status === "repairing") && b.branch !== work?.branch);
  if (!work && !branch) return null;
  const turn = work && work.status !== "background" ? work : undefined;
  return (
    <div className="col-start-3 mt-0.5 space-y-1 border-t border-dashed border-line pt-2 xl:col-span-3 xl:col-start-3" data-part="flow-member">
      {turn && (
        <p className="flex min-w-0 items-baseline gap-2">
          <Mark kind={turn.status === "running" ? "run" : "plan"} />
          <span className="min-w-0 flex-1 truncate">
            {turn.task_number ? `#${turn.task_number} ` : ""}
            {turn.repair ? t("flow.repair") : turn.title || t(turn.status === "queued" ? "agentStatus.queued" : "flow.member.turn")}
          </span>
          {turn.since && <span className="text-fg-3">{since(turn.since)}</span>}
        </p>
      )}
      {turn?.branch && (
        <p className="flex min-w-0 items-baseline gap-2 text-fg-2">
          <span className="mk mk-you" />
          <Branch name={turn.branch} />
          <span className="flex-1 truncate">{t("flow.member.repairs")}</span>
        </p>
      )}
      {work?.jobs.map((j) => (
        <p key={j.id} className="flex items-baseline gap-2 font-mono text-fg-2">
          <Mark kind="plan" />
          <span className="min-w-0 flex-1 truncate">$ {j.command}</span>
          <span className="text-fg-3">{since(j.started_at)}</span>
        </p>
      ))}
      {branch && (
        <p className="flex min-w-0 items-baseline gap-2 text-fg-2">
          {branch.status === "repairing" ? <span className="mk mk-you" /> : <Mark kind="plan" />}
          <Branch name={branch.branch} />
          <span className="flex-1 truncate">{t(`flow.member.${branch.status}`, { n: branch.train_id })}</span>
        </p>
      )}
    </div>
  );
}

/** The sidebar block (desktop) and the line above the board (phone): the train that is checked, what waits, what is repaired. */
export function FlowSide({ onOpen, variant, compact = false, bare = false, gone = false }: { onOpen: () => void; variant: "table" | "strip"; compact?: boolean; bare?: boolean; gone?: boolean }) {
  const { t } = useTranslation();
  const m = useFlowModel();
  if (!m || m.live.repositories.length === 0) return null;
  const { live } = m;
  const train = live.trains[0];
  const waiting = live.branches.filter((b) => b.status === "waiting").length;
  const repairing = live.branches.filter((b) => b.status === "repairing").length;
  const typical = train ? typicalOf(m.repoOf(train.repository_id)) : null;
  if (!train && !waiting && !repairing) return variant === "strip" ? null : (
    <section className="flex shrink-0 flex-col px-3.5">
      <h2 className="caps shrink-0 rule-b pb-1.5">{t("flow.side.title")}</h2>
      <button type="button" onClick={onOpen} className="block w-full border-b border-line py-[7px] text-left text-fg-3 hover:bg-sunk">
        {t("flow.side.quiet")}
      </button>
    </section>
  );
  if (variant === "strip")
    return (
      <button type="button" onClick={onOpen} className="flex shrink-0 items-center gap-2 overflow-x-auto px-4 pb-2.5 text-left whitespace-nowrap text-fg-2 [scrollbar-width:none]">
        <ArrowRight className="size-3.5 text-fg-3" />
        {train && (
          <span className="font-semibold text-fg">
            {t("flow.train", { n: train.id })} · {clock(elapsedOf(train, m.now))}
            {typical ? ` / ~${clock(typical)}` : ""}
          </span>
        )}
        {waiting > 0 && <span>{t("flow.side.waiting", { count: waiting })}</span>}
        {repairing > 0 && <span>{t("flow.side.repairing", { count: repairing })}</span>}
      </button>
    );
  const body = (
    <>
      {train && (
        <div className="border-b border-line py-[7px]">
          <div className="flex items-baseline gap-2">
            <span className="mk mk-run" />
            <span className="flex-1 truncate">
              {t("flow.train", { n: train.id })} · {t("flow.branches", { count: train.size })}
            </span>
            <span className="text-fg-2">{clock(elapsedOf(train, m.now))}</span>
          </div>
          {typical && <Progress elapsed={elapsedOf(train, m.now)} typical={typical} className="mt-1.5" />}
        </div>
      )}
      <p className="flex items-baseline gap-2 border-b border-line py-[7px]">
        <span className="mk mk-plan" />
        <span className="flex-1">{t("flow.handed")}</span>
        <span className="text-fg-2">{waiting}</span>
      </p>
      {repairing > 0 && (
        <p className="flex items-baseline gap-2 border-b border-line py-[7px]">
          <span className="mk mk-you" />
          <span className="flex-1">{t("flow.repair")}</span>
          <span className="text-fg-2">{repairing}</span>
        </p>
      )}
    </>
  );
  // Little height: one line, the train if there is one, else what waits. The full block stays as an invisible probe to measure against.
  const one = train ? (
    <p className="flex items-baseline gap-2 border-b border-line py-[7px]">
      <span className="mk mk-run" />
      <span className="flex-1 truncate">
        {t("flow.train", { n: train.id })} · {t("flow.branches", { count: train.size })}
      </span>
      <span className="text-fg-2">{clock(elapsedOf(train, m.now))}</span>
    </p>
  ) : (
    <p className="flex items-baseline gap-2 border-b border-line py-[7px]">
      <span className={repairing && !waiting ? "mk mk-you" : "mk mk-plan"} />
      <span className="flex-1">{repairing && !waiting ? t("flow.repair") : t("flow.handed")}</span>
      <span className="text-fg-2">{repairing && !waiting ? repairing : waiting}</span>
    </p>
  );
  return (
    <section className={clsx("relative flex shrink-0 flex-col px-3.5", gone && "-mb-5 h-0 overflow-hidden")} inert={gone} data-part="flow-side">
      {!bare && <h2 className="caps shrink-0 rule-b pb-1.5">{t("flow.side.title")}</h2>}
      <button type="button" onClick={onOpen} className="block w-full text-left hover:bg-sunk" title={t("nav.flow")}>
        {compact ? one : body}
      </button>
      {compact && (
        <div data-flow-probe aria-hidden className="pointer-events-none invisible absolute inset-x-3.5 top-0 -z-10">
          <h2 className="caps rule-b pb-1.5">{t("flow.side.title")}</h2>
          {body}
        </div>
      )}
    </section>
  );
}
