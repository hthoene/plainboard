import clsx from "clsx";
import { ArrowRight, ChevronRight, Radio } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useFlowHistory } from "../../api/queries";
import type { FlowHistoryItem } from "../../api/types";
import { Badge, Button, EmptyState, PageHead, Segmented, Spinner } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { useSince } from "../../lib/time";
import { useShell } from "../layout/shell-context";
import { clock, elapsedOf, ms, typicalOf, useFlowModel, type FlowModel } from "./flow";
import { Branch, Held, Mark, Progress, Who } from "./parts";

const ROW = "grid grid-cols-[9px_minmax(0,1fr)_auto] items-baseline gap-x-2 border-b border-line px-2.5 py-2";

function Lane({ title, count, children, last }: { title: string; count: number; children: ReactNode; last?: boolean }) {
  return (
    <section className="min-w-0">
      <h3 className="caps rule-b flex h-9 items-center gap-1.5 pr-1 pl-2.5">
        <span className="min-w-0 flex-1 truncate">{title}</span>
        <span className="font-normal text-fg-3">{count || ""}</span>
        {!last && <ChevronRight className="hidden size-3.5 shrink-0 text-fg-3 @3xl:block" />}
      </h3>
      {children}
    </section>
  );
}
const Empty = ({ children }: { children: ReactNode }) => <p className="border-b border-line px-2.5 py-2 text-fg-3">{children}</p>;

/** The four stages with how many things are in each: the one line the sidebar and the phone strip also tell. */
function Stages({ m }: { m: FlowModel }) {
  const { t } = useTranslation();
  const { live } = m;
  const items = [
    [t("flow.working"), live.work.filter((w) => w.status !== "background").length],
    [t("flow.handed"), live.branches.filter((b) => b.status === "waiting" || b.status === "repairing" || b.status === "stuck").length],
    [t("flow.checking"), live.trains.reduce((n, x) => n + x.size, 0)],
    [t("flow.landed"), live.branches.filter((b) => b.status === "merged" && Date.now() - ms(b.merged_at) < 24 * 3600_000).length],
  ] as const;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-4 py-3" data-part="flow-stages">
      {items.map(([label, n], i) => (
        <div key={label} className="flex items-center gap-2">
          <span className={clsx("inline-flex items-baseline gap-1.5", i === 2 && n > 0 && "font-semibold")}>
            <span className="text-fg-2">{label}</span>
            <span className="font-extrabold">{n}</span>
          </span>
          {i < items.length - 1 && <ArrowRight className="size-3.5 text-fg-3" />}
        </div>
      ))}
    </div>
  );
}

function WorkRows({ m, agentIds }: { m: FlowModel; agentIds: number[] }) {
  const { t } = useTranslation();
  const { agentById, openTask, openChat } = useShell();
  const since = useSince(1000);
  const rows = m.live.work.filter((w) => agentIds.includes(w.agent_id));
  if (!rows.length) return <Empty>{t("flow.nobody")}</Empty>;
  return (
    <>
      {rows.map((w) => (
        <button key={w.agent_id} type="button" onClick={() => (w.task_id ? openTask(w.task_id) : openChat(w.agent_id))} className={clsx(ROW, "w-full text-left hover:bg-sunk")}>
          <Mark kind={w.status === "running" ? "run" : "plan"} />
          <span className="min-w-0">
            <span className="block truncate font-medium">
              <Who agent={agentById(w.agent_id)} />
            </span>
            {w.status !== "background" && (
              <span className="block truncate text-fg-2">
                {w.task_number ? `#${w.task_number} ` : ""}
                {w.repair ? t("flow.repair") : w.title || (w.status === "queued" ? t("agentStatus.queued") : "")}
              </span>
            )}
            {w.branch && <Branch name={w.branch} className="text-fg-3" />}
            {w.jobs.map((j) => (
              <span key={j.id} className="block truncate font-mono text-fg-3" title={j.command}>
                $ {j.command}
              </span>
            ))}
          </span>
          <span className="text-fg-3">{w.since ? since(w.since) : ""}</span>
        </button>
      ))}
    </>
  );
}

export function FlowView() {
  const { t } = useTranslation();
  const { openTask, agentById } = useShell();
  const m = useFlowModel();
  const since = useSince(1000);
  const [repoFilter, setRepoFilter] = useState<number | null>(null);
  const { project } = useShell();
  const history = useFlowHistory(project.id, repoFilter);
  if (!m) return null;
  const { live } = m;
  const repos = live.repositories;
  const busy = live.work.map((w) => w.agent_id);

  return (
    <div className="flex h-full flex-col">
      <PageHead title={t("flow.title")}>
        <span className="flex items-center gap-1.5 text-fg-2">
          <Radio className="flow-pulse size-3.5" /> {t("flow.live")}
        </span>
      </PageHead>
      <div className="scroll-thin @container min-h-0 flex-1 overflow-y-auto">
        <Stages m={m} />
        {repos.length === 0 && (
          <div className="mb-6 px-2 @3xl:max-w-xl">
            <Lane title={t("flow.working")} count={live.work.length} last>
              <WorkRows m={m} agentIds={busy} />
            </Lane>
            <p className="px-2.5 py-3 text-fg-2">{t("flow.noRepos")}</p>
          </div>
        )}
        {repos.map((repo) => {
          const mine = live.branches.filter((b) => b.repository_id === repo.id);
          const handed = mine.filter((b) => b.status === "repairing" || b.status === "stuck" || b.status === "waiting");
          const trains = live.trains.filter((x) => x.repository_id === repo.id);
          const landed = mine.filter((b) => b.status === "merged").sort((a, b) => ms(b.merged_at) - ms(a.merged_at));
          const typical = typicalOf(repo);
          const agentIds = busy.filter((id) => m.repoOfAgent(id) === repo.id);
          return (
            <div key={repo.id} className="mb-6" data-part="flow-repo">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-4 pb-2">
                <h2 className="font-extrabold">{repo.name}</h2>
                <span className="font-mono text-fg-3">{repo.base}</span>
                {repo.land_first && <Badge>{t("flow.landFirst")}</Badge>}
                <span className="flex-1" />
                {typical && <span className="text-fg-3">{t("flow.typical", { time: clock(typical) })}</span>}
                <span className={clsx("flex items-baseline gap-1", repo.red ? "text-you-fg" : "text-fg-2")}>
                  <Mark kind={repo.red ? "you" : "done"} className="self-center" />
                  <span>{t(repo.red ? "flow.baseRed" : "flow.baseGreen")}</span>
                </span>
              </div>
              <div className="grid gap-y-5 px-2 @3xl:grid-cols-4 @3xl:gap-x-3">
                <Lane title={t("flow.working")} count={agentIds.length}>
                  <WorkRows m={m} agentIds={agentIds} />
                </Lane>

                <Lane title={t("flow.handed")} count={handed.length}>
                  {handed.map((l) => (
                    <button key={l.id} type="button" onClick={() => l.task_id && openTask(l.task_id)} className={clsx(ROW, "w-full text-left", l.status === "waiting" ? "hover:bg-sunk" : "bg-sunk")}>
                      {l.status === "waiting" && !l.held ? <Mark kind="plan" /> : <Mark kind="you" />}
                      <span className="min-w-0">
                        <Branch name={l.branch} />
                        <span className="flex min-w-0 items-center gap-1.5 text-fg-2">
                          <Held branch={l} />
                          <span className="truncate">
                            {l.task_number ? `#${l.task_number} · ` : ""}
                            {l.held ? t("flow.state.held") : t(`flow.state.${l.status}`, { count: l.failures })}
                          </span>
                        </span>
                      </span>
                      <span className="text-fg-3">{since(l.since)}</span>
                    </button>
                  ))}
                  {handed.length === 0 && <Empty>{t("flow.noneHanded")}</Empty>}
                </Lane>

                <Lane title={t("flow.checking")} count={trains.reduce((n, x) => n + x.size, 0)}>
                  {trains.map((train) => (
                    <div key={train.id} className="border-b border-line px-2.5 py-2">
                      <div className="flex items-baseline gap-2">
                        <Mark kind={train.status === "blocked" ? "you" : "run"} />
                        <span className="font-extrabold">{t("flow.train", { n: train.id })}</span>
                        <span className="flex-1" />
                        <span className="text-fg-2">{t("flow.branches", { count: train.size })}</span>
                      </div>
                      {train.status === "blocked" ? (
                        <p className="my-2 text-fg-2">{t("flow.blocked")}</p>
                      ) : (
                        <>
                          {typical ? <Progress elapsed={elapsedOf(train, m.now)} typical={typical} className="my-2" /> : <div className="h-2" />}
                          <div className="flex justify-between text-fg-2">
                            <span className="font-semibold">{clock(elapsedOf(train, m.now))}</span>
                            {typical && <span className="text-fg-3">~{clock(typical)}</span>}
                          </div>
                        </>
                      )}
                      {train.repaired_group && <p className="mt-1 text-fg-3">{t("flow.repairedGroup")}</p>}
                      <div className="mt-2 space-y-1.5">
                        {train.branches.map((name) => (
                          <div key={name} className="flex min-w-0 items-center gap-2">
                            <Who agent={agentById(m.branchByName(repo.id, name)?.agent_id)} className="shrink-0" />
                            <Branch name={name} className="flex-1" />
                            <Held branch={m.branchByName(repo.id, name)} />
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                  {trains.length === 0 && <Empty>{repo.land_first ? t("flow.landFirstIdle") : t("flow.noTrain")}</Empty>}
                </Lane>

                <Lane title={t("flow.landed")} count={0} last>
                  {landed.map((l) => (
                    <button key={l.id} type="button" onClick={() => l.task_id && openTask(l.task_id)} className={clsx(ROW, "w-full text-left hover:bg-sunk")}>
                      <Mark kind="done" />
                      <span className="min-w-0">
                        <Branch name={l.branch} />
                        <span className="block truncate text-fg-2">
                          {l.task_number ? `#${l.task_number} ` : ""}
                          {l.title}
                        </span>
                      </span>
                      <span className="text-fg-3">{since(l.merged_at)}</span>
                    </button>
                  ))}
                  {landed.length === 0 && <Empty>{t("flow.noneLanded")}</Empty>}
                </Lane>
              </div>
            </div>
          );
        })}

        <div className="px-4 pb-8">
          <div className="rule-b flex min-h-9 flex-wrap items-center gap-2 pb-1">
            <h2 className="caps flex-1 pt-1.5">{t("flow.history")}</h2>
            {repos.length > 1 && (
              <Segmented<number>
                value={repoFilter ?? 0}
                onChange={(v) => setRepoFilter(v || null)}
                aria-label={t("flow.repository")}
                options={[{ value: 0, label: t("flow.allRepos") }, ...repos.map((r) => ({ value: r.id, label: r.name }))]}
              />
            )}
          </div>
          {history.isLoading && <Spinner className="mt-4" />}
          {history.data?.pages.flatMap((p) => p.items).map((item) => (
            <HistoryRow key={item.id} item={item} m={m} />
          ))}
          {history.data && history.data.pages[0].items.length === 0 && <EmptyState title={t("flow.historyEmpty")} />}
          {history.hasNextPage && (
            <div className="pt-3">
              <Button onClick={() => history.fetchNextPage()} disabled={history.isFetchingNextPage}>
                {t("flow.more")}
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function HistoryRow({ item, m }: { item: FlowHistoryItem; m: FlowModel }) {
  const { t } = useTranslation();
  const { agentById } = useShell();
  const since = useSince(30_000);
  const repo = item.repository_id ? m.repoOf(item.repository_id)?.name : undefined;
  const train = item.train;
  const failed = train?.status === "failed" || train?.status === "blocked";
  let title: string;
  if (item.kind === "sweep") title = t("flow.h.sweep");
  else if (item.kind === "stuck") title = t("flow.h.stuck");
  else if (train!.size === 0) title = t(train!.status === "landed" ? "flow.h.basePassed" : "flow.h.baseFailed", { n: train!.id });
  else if (train!.direct) title = t(train!.status === "landed" ? "flow.h.directPassed" : "flow.h.directFailed", { n: train!.id, count: train!.size });
  else title = t(`flow.h.train.${train!.status}`, { n: train!.id, count: train!.size });
  return (
    <div className="grid grid-cols-[9px_minmax(0,1fr)_auto] gap-x-2 border-b border-line py-2" data-part="flow-history-row">
      <span className="pt-[7px]">{item.kind === "sweep" ? <Mark kind="plan" /> : failed || item.kind === "stuck" ? <Mark kind="you" /> : train!.status === "aborted" ? <Mark kind="plan" /> : <Mark kind="done" />}</span>
      <div className="min-w-0">
        <p className={clsx(failed && "font-semibold")}>
          {title}
          {repo && <span className="font-normal text-fg-3"> · {repo}</span>}
        </p>
        {item.detail && <p className="text-fg-2 break-words">{item.detail}</p>}
        {train && train.branches.length > 0 && (
          <p className="flex flex-wrap gap-x-3">
            {train.branches.map((b) => (
              <Branch key={b} name={b} />
            ))}
          </p>
        )}
        {train && train.failed_checks.length > 0 && (
          <div className="mt-1 text-fg-2">
            {train.failed_checks.map((c) => (
              <p key={c} className="font-mono break-words">
                ✕ {c}
              </p>
            ))}
          </div>
        )}
        {train?.repaired_group && <p className="text-fg-3">{t("flow.repairedGroup")}</p>}
        {train?.escalated && <p className="text-fg-2">{t("flow.escalated")}</p>}
        {item.repairs.map((r, i) => (
          <p key={i} className="text-fg-2">
            {t("flow.h.repairOrdered", { name: agentById(r.agent_id)?.name ?? t("flow.h.someone") })} · {since(r.at)}
          </p>
        ))}
      </div>
      <div className="text-right text-fg-3" title={formatDateTime(item.at)}>
        <p>{since(item.at)}</p>
        {train && train.check_seconds > 0 && <p>{clock(train.check_seconds)}</p>}
      </div>
    </div>
  );
}
