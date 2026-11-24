import clsx from "clsx";
import { ArrowUpRight, Rocket } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { usePromote, usePromotionPreview } from "../../api/queries";
import type { FlowRepo, Promotion } from "../../api/types";
import {
  Button,
  ErrorText,
  Field,
  Input,
  Modal,
  Spinner,
} from "../../components/ui";
import { useSince } from "../../lib/time";
import { useShell } from "../layout/shell-context";
import { Mark } from "./parts";

const COMMITS_SHOWN = 30;

/** The last promotion of a repository's base branch to its release branch, in one line (#129). */
export function PromotionLine({ promotion }: { promotion?: Promotion }) {
  const { t } = useTranslation();
  const since = useSince(30_000);
  if (!promotion) return null;
  const sha = promotion.sha.slice(0, 7);
  const tag = promotion.tag ? ` · ${promotion.tag}` : "";
  return (
    <span
      className={clsx(
        "flex min-w-0 items-baseline gap-1",
        promotion.status === "failed" ? "text-you-fg" : "text-fg-2",
      )}
      title={promotion.detail || undefined}
      data-part="flow-promotion"
    >
      <Mark
        kind={
          promotion.status === "checking"
            ? "run"
            : promotion.status === "failed"
              ? "you"
              : "done"
        }
        className="self-center"
      />
      <span className="truncate">
        {t(`flow.promotion.${promotion.status}`, {
          release: promotion.release,
          sha,
        })}
        {tag}
        {promotion.status !== "checking" &&
          ` · ${since(promotion.finished_at)}`}
      </span>
      {promotion.status === "pull_request" && promotion.url && (
        <a
          href={promotion.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center underline"
        >
          {t("flow.promotion.open")} <ArrowUpRight className="size-3" />
        </a>
      )}
    </span>
  );
}

/** Promote the base branch to the release branch: what goes is shown first, then one check run on exactly that commit. */
export function PromoteButton({
  repo,
  running,
}: {
  repo: FlowRepo;
  running: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        size="sm"
        icon={<Rocket className="size-3.5" />}
        onClick={() => setOpen(true)}
        disabled={running}
        data-part="flow-promote"
      >
        {t("flow.promotion.button", { release: repo.release })}
      </Button>
      {open && <PromoteDialog repo={repo} onClose={() => setOpen(false)} />}
    </>
  );
}

function PromoteDialog({
  repo,
  onClose,
}: {
  repo: FlowRepo;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { project, openTask } = useShell();
  const preview = usePromotionPreview(repo.id, true);
  const promote = usePromote(project.id);
  const [tag, setTag] = useState("");
  const p = preview.data;
  const empty = p !== undefined && p.total === 0;
  const submit = () =>
    p &&
    promote.mutate(
      { repoId: repo.id, tag: tag.trim(), sha: p.sha },
      { onSuccess: onClose },
    );
  return (
    <Modal
      open
      onClose={onClose}
      title={t("flow.promotion.title", {
        base: p?.base || repo.base,
        release: repo.release,
      })}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            onClick={submit}
            loading={promote.isPending}
            disabled={!p || empty || !!p.running}
          >
            {t("flow.promotion.confirm", { release: repo.release })}
          </Button>
        </>
      }
    >
      {preview.isLoading && <Spinner />}
      <ErrorText error={preview.error} />
      {p && (
        <div className="space-y-4" data-part="promotion-preview">
          {empty ? (
            <p className="text-fg-2">
              {t("flow.promotion.nothing", {
                release: p.release,
                base: p.base,
              })}
            </p>
          ) : (
            <>
              <p>
                {t("flow.promotion.summary", {
                  count: p.total,
                  base: p.base,
                  sha: p.sha.slice(0, 7),
                  release: p.release,
                })}{" "}
                <span className="text-fg-2">
                  {t(
                    p.fast_forward
                      ? "flow.promotion.fastForward"
                      : "flow.promotion.merge",
                    { release: p.release },
                  )}
                </span>
              </p>
              <p className="text-fg-2">
                {t(
                  p.checked ? "flow.promotion.checked" : "flow.promotion.check",
                )}
              </p>
              {p.running && (
                <p className="text-you-fg">
                  {t("flow.promotion.running", {
                    sha: p.running.sha.slice(0, 7),
                  })}
                </p>
              )}
              <section>
                <h3 className="caps rule-b pb-1">
                  {t("flow.promotion.tickets", { count: p.tickets.length })}
                </h3>
                {p.tickets.length === 0 && (
                  <p className="py-2 text-fg-3">
                    {t("flow.promotion.noTickets")}
                  </p>
                )}
                {p.tickets.map((x) => (
                  <button
                    key={x.branch}
                    type="button"
                    disabled={!x.task_id}
                    onClick={() => x.task_id && openTask(x.task_id)}
                    className="flex w-full min-w-0 items-baseline gap-2 border-b border-line py-1.5 text-left enabled:hover:bg-sunk"
                  >
                    <span className="w-14 shrink-0 font-semibold">
                      {x.task_number ? `#${x.task_number}` : ""}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{x.title}</span>
                    <span className="max-w-[40%] truncate font-mono text-fg-3">
                      {x.branch}
                    </span>
                  </button>
                ))}
              </section>
              <section>
                <h3 className="caps rule-b pb-1">
                  {t("flow.promotion.commits", { count: p.commits.length })}
                </h3>
                {p.commits.slice(0, COMMITS_SHOWN).map((c) => (
                  <div
                    key={c.sha}
                    className="flex min-w-0 items-baseline gap-2 border-b border-line py-1"
                  >
                    <span className="shrink-0 font-mono text-fg-3">
                      {c.sha.slice(0, 7)}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{c.message}</span>
                    <span className="shrink-0 text-fg-3">{c.author}</span>
                  </div>
                ))}
                {p.commits.length > COMMITS_SHOWN && (
                  <p className="py-1 text-fg-3">
                    {t("flow.promotion.moreCommits", {
                      count: p.commits.length - COMMITS_SHOWN,
                    })}
                  </p>
                )}
              </section>
              <Field
                label={t("flow.promotion.tag")}
                hint={t("flow.promotion.tagHint")}
              >
                <Input
                  value={tag}
                  onChange={(e) => setTag(e.target.value)}
                  placeholder="v1.4.0"
                  maxLength={100}
                />
              </Field>
            </>
          )}
          <ErrorText error={promote.error} />
        </div>
      )}
    </Modal>
  );
}
