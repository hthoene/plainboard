import clsx from "clsx";
import { ArrowRight, Copy, GripVertical, MessageCircleQuestion, Send, Sparkles, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { showIdeaChange } from "../../api/events";
import type { Idea, IdeaChange } from "../../api/types";
import { IconButton, Input } from "../../components/ui";

/** The AI's voice: a small sparkle and a line of text. */
export function AiLine({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p className={clsx("flex items-start gap-1.5 text-fg-2", className)}>
      <Sparkles className="mt-[3px] size-3.5 shrink-0 text-fg-3" />
      <span className="min-w-0">{children}</span>
    </p>
  );
}

export const ideaTitle = (idea: Idea) => idea.title || (idea.text.length > 60 ? `${idea.text.slice(0, 60)}…` : idea.text);

/** One idea on the board: what it says, what the AI noticed, its open question; drags to another topic. */
export function IdeaCard({
  idea,
  byId,
  canWrite,
  draggable,
  entering,
  onOpen,
  onShow,
}: {
  idea: Idea;
  byId: Map<number, Idea>;
  canWrite: boolean;
  draggable: boolean;
  entering: boolean;
  onOpen: () => void;
  /** Bring another idea into view (a similar one, the original of a duplicate). */
  onShow: (id: number) => void;
}) {
  const { t } = useTranslation();
  const leaving = idea.status !== "open";
  const rot = ((idea.id * 37) % 5) - 2;
  const original = idea.duplicate_of !== null ? byId.get(idea.duplicate_of) : undefined;
  const twins = [...byId.values()].filter((i) => i.duplicate_of === idea.id && i.status === "open");
  const similar = idea.similar.map((id) => byId.get(id)).filter((i): i is Idea => !!i && i.id !== idea.duplicate_of && !twins.includes(i));
  return (
    <div
      data-idea={idea.id}
      className={clsx("relative", leaving && "idea-leaving", entering && "idea-enter")}
      style={{ rotate: `${rot * 0.35}deg` }}
    >
      {twins.length > 0 && <div aria-hidden className="absolute inset-0 translate-x-1.5 translate-y-1.5 rounded-md bg-[var(--card,var(--sunk))] ring-1 ring-line" />}
      <div
        role="button"
        tabIndex={0}
        data-part="idea-card"
        aria-label={ideaTitle(idea)}
        draggable={draggable && !leaving}
        onDragStart={(e) => {
          e.dataTransfer.setData("application/x-plainboard-idea", String(idea.id));
          e.dataTransfer.effectAllowed = "move";
        }}
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            onOpen();
          }
        }}
        className={clsx(
          "group relative flex cursor-pointer flex-col gap-1.5 rounded-md bg-[var(--card,var(--bg))] px-3 py-2.5 text-left shadow-[var(--shadow-card,none)] ring-1 ring-line transition-shadow hover:ring-line-2",
          original && "opacity-75",
        )}
      >
        {draggable && <GripVertical aria-hidden className="absolute top-3 left-0.5 size-3.5 text-fg-3 opacity-0 group-hover:opacity-100 max-sm:hidden" />}
        <div className="leading-snug font-medium text-fg">{ideaTitle(idea)}</div>
        {idea.title && idea.text && <p className="line-clamp-4 leading-snug whitespace-pre-line text-fg-2">{idea.text}</p>}
        {idea.question_status === "open" && <IdeaQuestion idea={idea} canWrite={canWrite} />}
        {original && (
          <AiLine className="text-[0.92em]">
            <Link onClick={() => onShow(original.id)}>{t("ideas.duplicateOf", { title: ideaTitle(original) })}</Link>
          </AiLine>
        )}
        {twins.map((twin) => (
          <AiLine key={twin.id} className="text-[0.92em]">
            <Link onClick={() => onShow(twin.id)}>
              <Copy className="mr-1 inline size-3.5 align-[-2px]" />
              {t("ideas.duplicateHere", { title: ideaTitle(twin) })}
            </Link>
          </AiLine>
        ))}
        {similar.map((other) => (
          <AiLine key={other.id} className="text-[0.92em]">
            <Link onClick={() => onShow(other.id)}>{t("ideas.similarTo", { title: ideaTitle(other) })}</Link>
          </AiLine>
        ))}
        {!idea.title && idea.sort_state === "pending" && <AiLine className="text-[0.92em]">{t("ideas.reading")}</AiLine>}
        {idea.hint && !similar.length && !original && <AiLine className="text-[0.92em]">{idea.hint}</AiLine>}
        {(idea.status === "taken" || idea.status === "merged") && (
          <span className="inline-flex items-center gap-1 self-end font-semibold text-ok">
            <ArrowRight className="size-3.5" />
            {idea.status === "taken" && idea.task_number !== null ? `#${idea.task_number}` : t("ideas.merged")}
          </span>
        )}
      </div>
    </div>
  );
}

function Link({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className="text-left hover:text-fg hover:underline"
    >
      {children}
    </button>
  );
}

/** The AI's question about an idea, as a decision: the recommended option first, or an own answer. */
export function IdeaQuestion({ idea, canWrite }: { idea: Idea; canWrite: boolean }) {
  const { t } = useTranslation();
  const [own, setOwn] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const base = `/projects/${idea.project_id}/ideas/${idea.id}`;
  const send = async (body: { option_index?: number; text?: string }) => {
    setBusy(true);
    setError("");
    try {
      showIdeaChange(await api.post<IdeaChange>(`${base}/answer`, body));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const withdraw = async () => {
    try {
      await api.del(`${base}/question`);
    } catch (err) {
      setError(errorMessage(err));
    }
  };
  return (
    <div data-part="idea-question" onClick={(e) => e.stopPropagation()} className="flex cursor-default flex-col gap-2 rounded-sm bg-you-bg px-2.5 py-2">
      <div className="flex items-start gap-1.5">
        <MessageCircleQuestion className="mt-[2px] size-4 shrink-0 text-you-fg" />
        <span className="min-w-0 flex-1 font-medium text-fg">{idea.question}</span>
        {canWrite && (
          <IconButton label={t("ideas.question.withdraw")} onClick={withdraw} className="-mt-1 -mr-1.5">
            <X className="size-3.5" />
          </IconButton>
        )}
      </div>
      <div className="flex flex-col gap-1">
        {idea.question_options.map((option, n) => (
          <button
            key={n}
            type="button"
            disabled={!canWrite || busy}
            onClick={() => send({ option_index: n })}
            className="flex flex-col rounded-sm bg-bg px-2 py-1.5 text-left ring-1 ring-line-2 ring-inset enabled:hover:bg-sunk disabled:cursor-default touch:min-h-11"
          >
            {n === 0 && <span className="caps text-fg-3">{t("ideas.question.recommended")}</span>}
            <span className="font-medium text-fg">{option.label}</span>
            {option.description && <span className="text-fg-2">{option.description}</span>}
          </button>
        ))}
      </div>
      {canWrite &&
        (own === null ? (
          <button type="button" onClick={() => setOwn("")} className="self-start text-fg-2 hover:text-fg hover:underline">
            {t("ideas.question.own")}
          </button>
        ) : (
          <form
            className="flex gap-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              if (own.trim()) void send({ text: own.trim() });
            }}
          >
            <Input autoFocus value={own} onChange={(e) => setOwn(e.target.value)} aria-label={t("ideas.question.own")} placeholder={t("ideas.question.ownPlaceholder")} />
            <IconButton label={t("ideas.question.send")} type="submit" disabled={busy || !own.trim()}>
              <Send className="size-4" />
            </IconButton>
          </form>
        ))}
      {error && <p className="text-you-fg">{error}</p>}
    </div>
  );
}
