import clsx from "clsx";
import { ArrowUp, FileText, Info, RotateCcw, Search, SquarePen, Sparkle, Square, X } from "lucide-react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type KeyboardEvent, type MouseEvent } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { parseUrl } from "./api";
import { CodeBlock } from "./Content";
import type { PageView, SiteView } from "./types";

/** POST /api/public/docs/<slug>/assistant (orchestration/docs_assistant.py): limits and timings as the server keeps them. */
const MAX_CHARS = 1000;
const KEEP_MS = 30 * 60 * 1000; // the server forgets a conversation after 30 quiet minutes
const PHONE = "(max-width: 639px)";

interface Source {
  title: string;
  url: string;
}

type Problem = "failed" | "offline" | "signIn" | "off" | "tooLong" | "busy" | "tooFast" | "today" | "siteDay" | "long" | "still";

interface Msg {
  id: number;
  role: "user" | "assistant";
  text: string;
  sources?: Source[];
  cut?: boolean;
  /** Still coming: the step it is at (a look-up's tool name, or "thinking"). */
  streaming?: string;
  stopped?: boolean;
  problem?: Problem;
  /** The question an answer belongs to, to ask it again. */
  question?: string;
}

interface Saved {
  conversation: string | null;
  messages: Msg[];
  at: number;
}

const storeKey = (slug: string) => `plainboard.docs-assistant.${slug}`;

function restore(slug: string): Saved | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(storeKey(slug)) ?? "null") as Saved | null;
    return saved && Date.now() - saved.at < KEEP_MS ? saved : null;
  } catch {
    return null;
  }
}

/** The server's refusals, as the visitor reads them: by status, a 429 by its sentence (services/docs_assistant.py). */
function problemOf(status: number, detail: string): Problem {
  if (status === 401 || status === 403) return "signIn";
  if (status === 404) return "off";
  if (status === 409) return "still";
  if (status === 413) return "tooLong";
  if (status === 429) {
    if (/conversation is long/i.test(detail)) return "long";
    if (/today's limit/i.test(detail)) return "today";
    if (/may today/i.test(detail)) return "siteDay";
    if (/busy/i.test(detail)) return "busy";
    return "tooFast";
  }
  return "failed";
}

let nextId = 1;

export default function AssistantPanel({
  site,
  lang,
  page,
  open,
  onClose,
  onNavigate,
  onAnswer,
}: {
  site: SiteView;
  lang: string;
  page: PageView | null;
  open: boolean;
  onClose: () => void;
  onNavigate: (href: string) => void;
  onAnswer: () => void;
}) {
  const { t } = useTranslation();
  const saved = useMemo(() => restore(site.slug), [site.slug]);
  const [messages, setMessages] = useState<Msg[]>(() => {
    const kept = saved?.messages ?? [];
    nextId = Math.max(nextId, ...kept.map((m) => m.id + 1));
    return kept;
  });
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const conversation = useRef<string | null>(saved?.conversation ?? null);
  const abort = useRef<AbortController | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  // Kept for the tab's session: a reload or a page opened by address goes on with it, as long as the server does.
  useEffect(() => {
    if (busy) return;
    try {
      if (messages.length) sessionStorage.setItem(storeKey(site.slug), JSON.stringify({ conversation: conversation.current, messages, at: Date.now() } satisfies Saved));
      else sessionStorage.removeItem(storeKey(site.slug));
    } catch {
      /* private mode or full: the conversation lives in the page only */
    }
  }, [messages, busy, site.slug]);

  useEffect(() => {
    if (!open) return;
    requestAnimationFrame(() => field.current?.focus({ preventScroll: true }));
    stick.current = true;
    if (list.current) list.current.scrollTop = list.current.scrollHeight;
    // The sheet covers the whole phone: the page below must not scroll with it.
    if (!matchMedia(PHONE).matches) return;
    const before = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = before;
    };
  }, [open]);

  // Follows the answer as it comes, unless the visitor scrolled up to read.
  useLayoutEffect(() => {
    if (stick.current && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [messages]);

  // The field grows with the question, up to a few lines.
  useLayoutEffect(() => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [input, open]);

  useEffect(() => () => abort.current?.abort(), []);

  const update = (id: number, change: Partial<Msg>) => setMessages((all) => all.map((m) => (m.id === id ? { ...m, ...change } : m)));

  const ask = useCallback(
    async (question: string, again?: number) => {
      const text = question.trim();
      if (!text || busy) return;
      if (text.length > MAX_CHARS) return;
      const answerId = nextId++;
      setMessages((all) => {
        const kept = again === undefined ? all : all.filter((m) => m.id !== again);
        const user: Msg[] = again === undefined ? [{ id: nextId++, role: "user", text }] : [];
        return [...kept, ...user, { id: answerId, role: "assistant", text: "", streaming: "thinking", question: text }];
      });
      setInput("");
      setBusy(true);
      stick.current = true;
      const ctrl = new AbortController();
      abort.current = ctrl;
      let got = "";
      const finish = (change: Partial<Msg>) => update(answerId, { streaming: undefined, ...change });
      try {
        let resp: Response | null = null;
        for (let attempt = 0; attempt < 2 && !resp; attempt++) {
          const r = await fetch(`${site.api}/assistant`, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
            body: JSON.stringify({ text, conversation: conversation.current, lang, ...(site.version && !site.version.default ? { release: site.version.slug } : {}) }),
            signal: ctrl.signal,
          });
          if (r.ok) resp = r;
          else {
            const detail = await r
              .json()
              .then((d: { detail?: unknown }) => (typeof d.detail === "string" ? d.detail : ""))
              .catch(() => "");
            // The server forgot the conversation (quiet for half an hour, or restarted): the question starts a new one.
            if (r.status === 404 && conversation.current && attempt === 0) {
              conversation.current = null;
              continue;
            }
            finish({ problem: problemOf(r.status, detail) });
            return;
          }
        }
        if (!resp?.body) {
          finish({ problem: "failed" });
          return;
        }
        const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        let ended = false;
        const handle = (line: string) => {
          const ev = JSON.parse(line) as { type: string; id?: string; text?: string; name?: string; sources?: Source[]; cut?: boolean };
          if (ev.type === "conversation" && ev.id) conversation.current = ev.id;
          else if (ev.type === "text") {
            got = ev.text ?? "";
            update(answerId, { text: got, streaming: "writing" });
          } else if (ev.type === "tool") {
            got = ""; // what came before a look-up was a step on the way, not the answer
            update(answerId, { text: "", streaming: ev.name ?? "thinking" });
          } else if (ev.type === "done") {
            ended = true;
            finish({ text: ev.text ?? got, sources: ev.sources ?? [], cut: !!ev.cut });
            onAnswer();
          } else if (ev.type === "error") {
            ended = true;
            finish({ text: "", problem: "failed" });
          }
        };
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          let at;
          while ((at = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, at).trim();
            buffer = buffer.slice(at + 1);
            if (line) {
              try {
                handle(line);
              } catch {
                /* a broken line: the next one goes on */
              }
            }
          }
        }
        if (!ended) finish(got ? { text: got, cut: true } : { problem: "failed" });
      } catch {
        if (ctrl.signal.aborted) finish({ text: got, stopped: true });
        else finish({ text: got, problem: got ? undefined : "offline", cut: !!got });
      } finally {
        abort.current = null;
        setBusy(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busy, site.api, lang, onAnswer],
  );

  const restart = () => {
    abort.current?.abort();
    const id = conversation.current;
    conversation.current = null;
    if (id) void fetch(`${site.api}/assistant/${encodeURIComponent(id)}`, { method: "DELETE", credentials: "same-origin" }).catch(() => undefined);
    setMessages([]);
    setInput("");
    requestAnimationFrame(() => field.current?.focus());
  };

  const follow = useCallback(
    (href: string) => {
      if (matchMedia(PHONE).matches) onClose();
      else requestAnimationFrame(() => field.current?.focus({ preventScroll: true })); // the next question can follow at once
      onNavigate(href);
    },
    [onClose, onNavigate],
  );

  const starters = [
    t("docsSite.assistant.starterWhat", { name: site.name }),
    page && page.path ? t("docsSite.assistant.starterPage", { title: page.title }) : t("docsSite.assistant.starterStart"),
    site.nav.items[1] ? t("docsSite.assistant.starterTopic", { title: site.nav.items[1].title }) : null,
  ].filter((s): s is string => !!s);

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void ask(input);
    }
  };
  const tooLong = input.length > MAX_CHARS;

  return (
    <section id="doc-ai-panel" role="dialog" aria-modal="false" aria-label={t("docsSite.assistant.title")} className="doc-ai-panel" hidden={!open}>
      <header className="doc-ai-head">
        <span className="doc-ai-mark" aria-hidden>
          <Sparkle className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="doc-ai-title">{t("docsSite.assistant.title")}</p>
          <p className="doc-ai-sub">{t("docsSite.assistant.subtitle", { name: site.name })}</p>
        </div>
        {messages.length > 0 && (
          <button type="button" className="doc-icon-btn" onClick={restart} aria-label={t("docsSite.assistant.newChat")} title={t("docsSite.assistant.newChat")}>
            <SquarePen className="size-[18px]" />
          </button>
        )}
        <button type="button" className="doc-icon-btn" onClick={onClose} aria-label={t("docsSite.assistant.close")} title={t("docsSite.assistant.close")}>
          <X className="size-5" />
        </button>
      </header>

      <div
        ref={list}
        className="doc-ai-body"
        data-scroll
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        {messages.length === 0 ? (
          <div className="doc-ai-hello">
            <span className="doc-ai-mark is-big" aria-hidden>
              <Sparkle className="size-6" />
            </span>
            <p className="doc-ai-hello-title">{t("docsSite.assistant.hello")}</p>
            <p className="doc-ai-hello-text">{t("docsSite.assistant.helloText", { name: site.name })}</p>
            <ul className="doc-ai-starters" aria-label={t("docsSite.assistant.starters")}>
              {starters.map((s) => (
                <li key={s}>
                  <button type="button" className="doc-ai-starter" onClick={() => void ask(s)}>
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <ol className="doc-ai-log" aria-live="polite" aria-busy={busy}>
            {messages.map((m) =>
              m.role === "user" ? (
                <li key={m.id} className="doc-ai-q">
                  <span className="sr-only">{t("docsSite.assistant.you")}: </span>
                  {m.text}
                </li>
              ) : (
                <Answer key={m.id} msg={m} siteBase={site} onFollow={follow} onRetry={() => m.question && void ask(m.question, m.id)} onRestart={restart} busy={busy} />
              ),
            )}
          </ol>
        )}
      </div>

      <form
        className="doc-ai-form"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(input);
        }}
      >
        <div className={clsx("doc-ai-field", tooLong && "is-over")}>
          <textarea
            ref={field}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            rows={1}
            placeholder={t("docsSite.assistant.placeholder")}
            aria-label={t("docsSite.assistant.placeholder")}
            enterKeyHint="send"
          />
          {busy ? (
            <button type="button" className="doc-ai-send is-stop" onClick={() => abort.current?.abort()} aria-label={t("docsSite.assistant.stop")} title={t("docsSite.assistant.stop")}>
              <Square className="size-3.5" fill="currentColor" />
            </button>
          ) : (
            <button type="submit" className="doc-ai-send" disabled={!input.trim() || tooLong} aria-label={t("docsSite.assistant.send")} title={t("docsSite.assistant.send")}>
              <ArrowUp className="size-[18px]" />
            </button>
          )}
        </div>
        <p className="doc-ai-note">
          {input.length > MAX_CHARS * 0.8 ? (
            <span className={clsx(tooLong && "doc-ai-over")}>{t("docsSite.assistant.chars", { count: input.length, max: MAX_CHARS })}</span>
          ) : (
            <>
              <Info className="size-3.5 shrink-0" aria-hidden />
              {t("docsSite.assistant.disclaimer")}
            </>
          )}
        </p>
      </form>
    </section>
  );
}

const Answer = memo(function Answer({
  msg,
  siteBase,
  onFollow,
  onRetry,
  onRestart,
  busy,
}: {
  msg: Msg;
  siteBase: SiteView;
  onFollow: (href: string) => void;
  onRetry: () => void;
  onRestart: () => void;
  busy: boolean;
}) {
  const { t } = useTranslation();
  const site = siteBase;
  const components = useMemo<Components>(
    () => ({
      a: ({ node: _n, href, children }: ComponentProps<"a"> & { node?: unknown }) => {
        const url = href ? new URL(href, location.href) : null;
        const inSite = url && parseUrl(site, url);
        return inSite ? (
          <a
            href={url.pathname + url.hash}
            onClick={(e: MouseEvent) => {
              if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
              e.preventDefault();
              onFollow(url.pathname + url.hash);
            }}
          >
            {children}
          </a>
        ) : (
          <a href={href} target="_blank" rel="noreferrer nofollow" className="doc-external">
            {children}
          </a>
        );
      },
      // No pictures from an answer: nothing it says loads anything.
      img: ({ alt }) => <>{alt}</>,
      pre: ({ node }) => {
        const code = node?.children[0] as { properties?: { className?: string[] }; children?: { value?: string }[] } | undefined;
        const lang = code?.properties?.className?.find((c) => c.startsWith("language-"))?.slice(9).toLowerCase() ?? "";
        const body = (code?.children ?? []).map((c) => c.value ?? "").join("").replace(/\n$/, "");
        return <CodeBlock text={body} lang={lang} />;
      },
      table: ({ node: _n, ...props }) => (
        <div className="doc-table">
          <table {...props} />
        </div>
      ),
    }),
    [site, onFollow],
  );

  const working = msg.streaming && !msg.text;
  return (
    <li className="doc-ai-a">
      <span className="sr-only">{t("docsSite.assistant.title")}: </span>
      {working && (
        <p className="doc-ai-status" role="status">
          {msg.streaming === "thinking" ? <span className="doc-ai-dots" aria-hidden><i /><i /><i /></span> : <Search className="size-3.5 doc-ai-pulse" aria-hidden />}
          {t(`docsSite.assistant.step.${msg.streaming}`, { defaultValue: t("docsSite.assistant.step.lookup") })}
        </p>
      )}
      {msg.text && (
        <div className={clsx("doc-prose doc-ai-prose", msg.streaming && "is-streaming")}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
            {msg.text}
          </ReactMarkdown>
        </div>
      )}
      {msg.problem && <Problem kind={msg.problem} onRetry={onRetry} onRestart={onRestart} busy={busy} />}
      {(msg.cut || msg.stopped) && !msg.problem && <p className="doc-ai-meta">{t(msg.stopped ? "docsSite.assistant.stopped" : "docsSite.assistant.cut")}</p>}
      {!!msg.sources?.length && (
        <div className="doc-ai-sources">
          <p className="doc-ai-sources-head">{t("docsSite.assistant.sources")}</p>
          <ul>
            {msg.sources.map((s) => (
              <li key={s.url}>
                <a
                  href={s.url}
                  onClick={(e) => {
                    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
                    e.preventDefault();
                    onFollow(s.url);
                  }}
                >
                  <FileText className="size-3.5 shrink-0" aria-hidden />
                  <span>{s.title}</span>
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
});

function Problem({ kind, onRetry, onRestart, busy }: { kind: Problem; onRetry: () => void; onRestart: () => void; busy: boolean }) {
  const { t } = useTranslation();
  const retry = kind === "failed" || kind === "offline" || kind === "busy" || kind === "tooFast" || kind === "still";
  return (
    <div className={clsx("doc-ai-problem", (kind === "failed" || kind === "offline") && "is-error")} role="alert">
      <p>{t(`docsSite.assistant.problem.${kind}`)}</p>
      {retry && (
        <button type="button" className="doc-ai-chip" onClick={onRetry} disabled={busy}>
          <RotateCcw className="size-3.5" /> {t("docsSite.assistant.retry")}
        </button>
      )}
      {kind === "long" && (
        <button type="button" className="doc-ai-chip" onClick={onRestart}>
          <SquarePen className="size-3.5" /> {t("docsSite.assistant.newChat")}
        </button>
      )}
      {kind === "signIn" && (
        <button type="button" className="doc-ai-chip" onClick={() => location.reload()}>
          {t("docsSite.signIn")}
        </button>
      )}
    </div>
  );
}
