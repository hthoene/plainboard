import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { ArrowUp, ChevronDown, Eye, EyeOff, Loader2, Maximize2, MoreHorizontal, Paperclip, Square, Trash2, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { useStreamingText } from "../../api/events";
import { qk, useConversation, useMe, useRuns, useSettings } from "../../api/queries";
import type { Attachment, Conversation } from "../../api/types";
import { Avatar } from "../../components/Avatar";
import { Markdown } from "../../components/Markdown";
import { AgentSquare, IconButton, Select, Spinner } from "../../components/ui";
import { formatCost, formatTokens } from "../../lib/format";
import { useStored } from "../../lib/hooks";
import { useNow } from "../../lib/time";
import { useShell } from "../layout/shell-context";
import { LimitsRing } from "./LimitsRing";
import { CHAT_ROW, Head, MessageItem, usePeople, writtenParts } from "./MessageItem";
import { StoppingAgents } from "./StoppingAgents";

const GROUP_MS = 5 * 60_000;

export function ChatPanel({
  agentId,
  onSelectAgent,
  onClose,
  onExpand,
  variant = "panel",
}: {
  agentId: number;
  onSelectAgent: (id: number) => void;
  onClose?: () => void;
  onExpand?: () => void;
  variant?: "panel" | "page";
}) {
  const { t } = useTranslation();
  const { project, agents, agentById, canWrite, openSettings } = useShell();
  const qc = useQueryClient();
  const agent = agentById(agentId);
  const settings = useSettings();
  const me = useMe();
  const conversation = useConversation(agentId);
  const runs = useRuns(project.id);
  const streaming = useStreamingText(agentId);
  const people = usePeople();
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [details, setDetails] = useStored<boolean>("plainboard.chatDetails", false);
  const [files, setFiles] = useState<File[]>([]);
  const [seed, setSeed] = useState<{ text: string; n: number } | null>(null);

  const activeRuns = (runs.data ?? []).filter((r) => r.agent_id === agentId && (r.status === "queued" || r.status === "running"));
  const running = activeRuns.find((r) => r.status === "running");
  const queued = activeRuns.filter((r) => r.status === "queued" && r.trigger === "chat");
  const messages = conversation.data?.messages ?? [];
  // A message the agent has not read yet stays at the very bottom, below whatever the agent still
  // writes, in the order it was sent. Once read it takes its place: at the time it was read, above
  // what the agent wrote after that. (By time, not by id: a busy agent can read a message and react
  // to it before the turn that answers it starts.)
  const unread = queued.filter((r) => !r.read_at);
  const timeline = [
    ...messages.map((m) => ({ at: Date.parse(m.created_at), message: m, run: null })),
    ...queued.filter((r) => r.read_at).map((r) => ({ at: Date.parse(r.read_at!), message: null, run: r })),
  ]
    .map((item, index) => ({ ...item, index }))
    .sort((a, b) => a.at - b.at || a.index - b.index);
  // What the agent writes in one go reads as one answer: the name stands on its first part only.
  const said = (m: (typeof messages)[number] | null) => !!m && m.role !== "user" && (m.kind === "text" || m.kind === "files");
  const shown = timeline.filter(({ message }) => !message || details || (message.kind !== "tool" && message.kind !== "summary"));
  const follows = new Set(
    shown.filter((item, i) => i > 0 && said(item.message) && said(shown[i - 1].message) && item.at - shown[i - 1].at < GROUP_MS).map((item) => item.message!.id),
  );
  // Works only until the agent's turn starts; if it just did, the refreshed list shows the message as sent.
  const deleteWaiting = (runId: number) =>
    api
      .del(`/runs/${runId}/message`)
      .catch(() => undefined)
      .then(() => qc.invalidateQueries({ queryKey: qk.runs(project.id) }));

  const waitingRow = (r: (typeof queued)[number]) => (
    <div key={`run-${r.id}`} data-part="msg" data-role="queued" className={clsx(CHAT_ROW, "bg-sunk")}>
      <Head name={r.author_name ?? t("chat.you")} avatar={r.author_name ? <Avatar name={r.author_name} person={people.get(r.author_name.toLowerCase())} /> : undefined}>
        <span className="mk mk-plan self-center" />
        <span className="min-w-0 flex-1 truncate text-fg-3">{t(r.read_at ? "chat.readWaiting" : "chat.waiting", { name: agent?.name ?? "" })}</span>
        {!r.read_at && (
          <IconButton label={t("chat.deleteWaiting")} onClick={() => deleteWaiting(r.id)} className="-my-1 self-center">
            <Trash2 className="size-4" />
          </IconButton>
        )}
      </Head>
      <div className="break-words whitespace-pre-wrap text-fg-2">
        {writtenParts(r.input_text, r.author_name, people)
          .map((part) => (part.author && part.author !== r.author_name ? `${part.author}: ${part.text}` : part.text))
          .join("\n")}
      </div>
      {r.attachment_ids.length > 0 && (
        <span className="mt-1 flex items-center gap-1 text-fg-3">
          <Paperclip className="size-4" />
          {t("chat.fileCount", { count: r.attachment_ids.length })}
        </span>
      )}
    </div>
  );

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages.length, streaming, queued.length, agentId, activeRuns.length]);

  useEffect(() => {
    stickToBottom.current = true;
  }, [agentId]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const loadOlder = async () => {
    const conv = conversation.data;
    if (!conv?.messages.length) return;
    setLoadingOlder(true);
    try {
      const older = await api.get<Conversation>(`/agents/${agentId}/conversation?before_id=${conv.messages[0].id}`);
      qc.setQueryData<Conversation>(qk.conversation(agentId), (cur) =>
        cur ? { ...cur, messages: [...older.messages, ...cur.messages], has_more: older.has_more } : cur,
      );
    } finally {
      setLoadingOlder(false);
    }
  };

  if (!agent) return null;
  // On the full page, keep a comfortable reading width.
  const membersOff = !!agent && !agent.is_manager && !!settings.data && !settings.data.values.member_chat;
  const column = variant === "page" ? "mx-auto w-full max-w-3xl" : "";
  const hasFiles = (e: DragEvent) => e.dataTransfer.types.includes("Files");
  const onDragOver = (e: DragEvent) => {
    if (hasFiles(e)) e.preventDefault();
  };
  const onDrop = (e: DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (agent.enabled) setFiles((cur) => [...cur, ...Array.from(e.dataTransfer.files)]);
  };

  return (
    <div className="flex h-full min-h-0 flex-col" onDragOver={onDragOver} onDrop={onDrop}>
      <header className="flex h-12 shrink-0 items-center gap-0.5 border-b border-line pr-2 pl-3">
        <div className="min-w-0 flex-1">
          <Select
            value={agentId}
            onChange={(e) => onSelectAgent(Number(e.target.value))}
            aria-label={t("chat.selectAgent")}
            trigger={
              <span className="flex h-8 items-center gap-2" title={`${agent.role} · ${agent.model}`}>
                <AgentSquare agent={agent} />
                <span className="truncate font-extrabold">{agent.name}</span>
                <ChevronDown className="size-3.5 shrink-0 text-fg-3" />
                <span className="min-w-0 flex-1 truncate text-fg-3">{agent.role}</span>
              </span>
            }
          >
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} — {a.role}
              </option>
            ))}
          </Select>
        </div>
        {details && <ContextGauge used={conversation.data?.context_tokens ?? agent.context_tokens} limit={agent.context_limit_tokens} base={agent.base_context_tokens} />}
        <IconButton label={details ? t("chat.hideDetails") : t("chat.showDetails")} onClick={() => setDetails(!details)} className={clsx(details && "bg-sunk text-fg")}>
          {details ? <Eye className="size-4" /> : <EyeOff className="size-4" />}
        </IconButton>
        <ChatMenu
          agentId={agentId}
          cost={agent.total_cost_usd}
          context={`${formatTokens(conversation.data?.context_tokens ?? agent.context_tokens)} / ${formatTokens(agent.context_limit_tokens)}`}
          disabled={!!running || !canWrite}
        />
        {onExpand && (
          <IconButton label={t("chat.expand")} onClick={onExpand}>
            <Maximize2 className="size-4" />
          </IconButton>
        )}
        {onClose && (
          <IconButton label={t("common.close")} onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        )}
      </header>

      <div ref={scroller} onScroll={onScroll} className="scroll-thin min-h-0 flex-1 overflow-x-hidden overflow-y-auto">
        <div className={column}>
        {conversation.isLoading && (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        )}
        {conversation.data?.has_more && (
          <button type="button" onClick={loadOlder} className="block w-full border-b border-line px-4 py-2 text-center text-fg-2 hover:bg-sunk hover:text-fg" disabled={loadingOlder}>
            {loadingOlder ? t("common.loading") : t("chat.loadOlder")}
          </button>
        )}
        {!conversation.isLoading && messages.length === 0 && !queued.length && (
          <ChatIntro isManager={agent.is_manager} onPick={canWrite && agent.enabled ? (text) => setSeed((last) => ({ text, n: (last?.n ?? 0) + 1 })) : undefined} />
        )}
        {timeline.map(({ message, run: r }) =>
          message ? <MessageItem key={message.id} message={message} agent={agent} details={details} follows={follows.has(message.id)} /> : r && waitingRow(r),
        )}
        {running && streaming && (
          <div data-part="msg" data-role="agent" data-follows={said(shown[shown.length - 1]?.message ?? null) ? "" : undefined} className={CHAT_ROW}>
            <Head name={agent.name} />
            <Markdown>{streaming}</Markdown>
          </div>
        )}
        {unread.map(waitingRow)}
        </div>
      </div>

      {membersOff ? (
        <MembersOffHint className={column} isAdmin={!!me?.is_admin} onOpenSettings={() => openSettings("general")} />
      ) : !canWrite ? (
        <p className={clsx(column, "shrink-0 border-t border-line py-3 text-fg-3")}>{t("members.readOnlyShort")}</p>
      ) : (
      <Composer
        seed={seed}
        agentId={agentId}
        agentName={agent.name}
        runningId={running?.id}
        disabled={!agent.enabled}
        className={column}
        status={
          activeRuns.length > 0
            ? t(running ? "chat.working" : "chat.waiting", { name: agent.name })
            : agent.running_jobs > 0
              ? t("chat.jobsRunning", { count: agent.running_jobs })
              : undefined
        }
        statusTitle={activeRuns.length === 0 ? agent.job_commands.join("\n") : undefined}
        runningSince={running ? (running.started_at ?? running.created_at) : undefined}
        files={files}
        onFilesChange={setFiles}
      />
      )}
    </div>
  );
}

/** Writing to a member is off (settings): the field gives way to a quiet note. */
function MembersOffHint({ className, isAdmin, onOpenSettings }: { className?: string; isAdmin: boolean; onOpenSettings: () => void }) {
  const { t } = useTranslation();
  return (
    <div data-part="composer-off" className="shrink-0 border-t border-line px-3 py-3 text-fg-3">
      <p className={className}>
        {t("chat.membersOff")}{" "}
        {isAdmin && (
          <button type="button" onClick={onOpenSettings} className="text-fg-2 underline underline-offset-2 hover:text-fg touch:py-2">
            {t("chat.membersOffSettings")}
          </button>
        )}
      </p>
    </div>
  );
}

const STARTERS = ["build", "existing", "change"] as const;

/** An empty chat says what it is for. With the manager it also offers a few ways to begin: a click
 * puts a sentence to complete into the message field, nothing is sent. */
function ChatIntro({ isManager, onPick }: { isManager: boolean; onPick?: (text: string) => void }) {
  const { t } = useTranslation();
  return (
    <div className="mx-auto max-w-sm space-y-2 px-4 py-10 text-fg-2">
      <p className="text-center font-extrabold text-fg">{isManager ? t("chat.introManagerTitle") : t("chat.introAgentTitle")}</p>
      <p className="text-center">{isManager ? t("chat.introManager") : t("chat.introAgent")}</p>
      {isManager && onPick && (
        <div className="pt-4">
          <p className="pb-1 text-fg-3">{t("chat.starters.hint")}</p>
          <ul className="border-t border-line">
            {STARTERS.map((id) => (
              <li key={id} className="border-b border-line">
                <button type="button" onClick={() => onPick(t(`chat.starters.${id}.text`))} className="block w-full px-1 py-2 text-left text-fg hover:bg-sunk touch:py-3">
                  {t(`chat.starters.${id}.label`)}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ContextGauge({ used, limit, base }: { used: number; limit: number; base: number }) {
  const { t } = useTranslation();
  const ratio = limit ? Math.min(1, used / limit) : 0;
  return (
    <span
      className={clsx("mr-1.5 hidden whitespace-nowrap sm:inline", ratio < 0.9 ? "text-fg-3" : "font-semibold text-fg")}
      title={t("chat.contextTitle", { used: formatTokens(used), limit: formatTokens(limit), base: formatTokens(base) })}
    >
      {formatTokens(used)} / {formatTokens(limit)}
    </span>
  );
}

function ChatMenu({ agentId, cost, context, disabled }: { agentId: number; cost: number; context: string; disabled: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const action = async (name: "compress" | "reset-session") => {
    setBusy(name);
    setNote(null);
    try {
      const res = await api.post<Record<string, boolean>>(`/agents/${agentId}/${name}`);
      if (name === "compress" && !res.compressed) setNote(t("chat.nothingToCompress"));
      else setOpen(false);
      qc.invalidateQueries({ queryKey: qk.conversation(agentId) });
    } catch (err) {
      setNote(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="relative">
      <IconButton label={t("chat.menu")} onClick={() => setOpen(!open)}>
        <MoreHorizontal className="size-4" />
      </IconButton>
      {open && (
        <div data-part="menu" className="absolute top-full right-0 z-40 mt-1 w-64 max-w-[calc(100vw-1rem)] rounded-sm bg-bg p-1 shadow-lg ring-1 ring-line-2">
          <MenuItem label={t("chat.compressNow")} hint={t("chat.compressHint")} busy={busy === "compress"} disabled={disabled} onClick={() => action("compress")} />
          <MenuItem label={t("chat.resetSession")} hint={t("chat.resetHint")} busy={busy === "reset-session"} disabled={disabled} onClick={() => action("reset-session")} />
          <div className="border-t border-line px-2 py-1.5 text-fg-3">{t("chat.context", { context })} · {t("chat.totalCost", { cost: formatCost(cost) })}</div>
          {note && <div className="px-2 pb-1.5 text-you-fg">{note}</div>}
        </div>
      )}
    </div>
  );
}

function MenuItem({ label, hint, onClick, busy, disabled }: { label: string; hint: string; onClick: () => void; busy: boolean; disabled: boolean }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled || busy} className="block w-full rounded-sm px-2 py-1.5 text-left hover:bg-sunk disabled:opacity-50">
      <div className="flex items-center gap-1.5 font-semibold">
        {busy && <Loader2 className="size-4 animate-spin" />}
        {label}
      </div>
      <div className="text-fg-3">{hint}</div>
    </button>
  );
}

/** Minutes and seconds since a turn started, counting on screen. */
function Elapsed({ since }: { since: string }) {
  const seconds = Math.max(0, Math.floor((useNow(1000) - Date.parse(since)) / 1000));
  return (
    <time className="shrink-0 font-normal text-fg-3">
      {Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}
    </time>
  );
}

function PendingFile({ file, onRemove }: { file: File; onRemove: () => void }) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<string>();
  useEffect(() => {
    if (!file.type.startsWith("image/")) return;
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  const name = file.name || "pasted.png";
  return (
    <div className="relative max-w-40" title={name}>
      {preview ? (
        <img src={preview} alt={name} className="size-16 rounded-sm object-cover ring-1 ring-line-2" />
      ) : (
        <div className="flex h-8 items-center gap-1.5 rounded-sm pr-8 pl-2 text-fg ring-1 ring-line-2 ring-inset">
          <Paperclip className="size-4 shrink-0 text-fg-3" />
          <span className="truncate">{name}</span>
        </div>
      )}
      <button
        type="button"
        onClick={onRemove}
        aria-label={t("chat.removeFile", { name })}
        className={clsx("absolute flex size-6 items-center justify-center rounded-sm text-fg-2 hover:text-fg", preview ? "top-0.5 right-0.5 bg-bg" : "top-1 right-1")}
      >
        <X className="size-4" />
      </button>
    </div>
  );
}

function Composer({
  seed,
  agentId,
  agentName,
  runningId,
  disabled,
  className,
  status,
  statusTitle,
  runningSince,
  files,
  onFilesChange,
}: {
  /** A text to start from, put into the field when it changes. */
  seed?: { text: string; n: number } | null;
  agentId: number;
  agentName: string;
  runningId?: number;
  disabled: boolean;
  className?: string;
  status?: string;
  statusTitle?: string;
  runningSince?: string;
  files: File[];
  onFilesChange: (files: File[]) => void;
}) {
  const { t } = useTranslation();
  const { project } = useShell();
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (!seed) return;
    setText(seed.text);
    area.current?.focus();
  }, [seed]);
  const picker = useRef<HTMLInputElement>(null);

  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  const send = async () => {
    const value = text.trim();
    const pending = files;
    if ((!value && !pending.length) || sending) return;
    setError(null);
    setText("");
    onFilesChange([]);
    setSending(true);
    try {
      let attachment_ids: number[] = [];
      if (pending.length) {
        const form = new FormData();
        for (const file of pending) form.append("files", file, file.name || "pasted.png");
        attachment_ids = (await api.post<Attachment[]>(`/agents/${agentId}/attachments`, form)).map((a) => a.id);
      }
      await api.post(`/agents/${agentId}/messages`, { text: value, attachment_ids });
      qc.invalidateQueries({ queryKey: qk.runs(project.id) });
    } catch (err) {
      setText(value);
      onFilesChange(pending);
      setError(errorMessage(err));
    } finally {
      setSending(false);
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    // Office apps also put a picture of copied text on the clipboard: text wins.
    const pasted = Array.from(e.clipboardData.files);
    if (pasted.length && !e.clipboardData.getData("text/plain")) {
      e.preventDefault();
      onFilesChange([...files, ...pasted]);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Touch keyboards: Enter inserts a newline, the send button sends.
    const touch = window.matchMedia("(pointer: coarse)").matches;
    if (e.key === "Enter" && !e.shiftKey && !touch && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className="shrink-0 border-t border-line px-3 pt-2.5 pb-3">
      <div className={className}>
      {error && <p className="mb-2 text-you-fg">{error}</p>}
      <StoppingAgents />
      {status && (
        <div role="status" title={statusTitle || undefined} className="mb-2 flex items-center gap-2 font-semibold">
          <span className={clsx("mk", runningId ? "mk-run motion-safe:animate-pulse" : "mk-plan")} />
          <span className="min-w-0 flex-1 truncate">{status}</span>
          {runningSince && <Elapsed since={runningSince} />}
        </div>
      )}
      {files.length > 0 && (
        <div className="mb-2 flex flex-wrap items-end gap-2">
          {files.map((file, i) => (
            <PendingFile key={`${file.name}-${file.lastModified}-${i}`} file={file} onRemove={() => onFilesChange(files.filter((_, j) => j !== i))} />
          ))}
        </div>
      )}
      <div data-part="composer" className="flex items-end gap-1 rounded-sm p-1 ring-1 ring-line-2 ring-inset focus-within:ring-2 focus-within:ring-fg">
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            onFilesChange([...files, ...Array.from(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
        <IconButton label={t("chat.attachFiles")} disabled={disabled} onClick={() => picker.current?.click()} className="size-8 touch:size-11">
          <Paperclip className="size-4" />
        </IconButton>
        <textarea
          ref={area}
          rows={1}
          value={text}
          disabled={disabled}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          onPaste={onPaste}
          placeholder={disabled ? t("chat.agentDisabled") : t("chat.placeholder", { name: agentName })}
          aria-label={t("chat.placeholder", { name: agentName })}
          className="scroll-thin max-h-[200px] min-h-8 min-w-0 flex-1 resize-none bg-transparent px-1.5 py-1.5 outline-none touch:min-h-11 touch:py-2.5"
        />
        {runningId ? (
          <IconButton label={t("chat.stop")} onClick={() => api.post(`/runs/${runningId}/cancel`)} className="size-8 text-fg-2 ring-1 ring-line-2 ring-inset touch:size-11">
            <Square className="size-3.5 fill-current" />
          </IconButton>
        ) : null}
        <button
          onClick={send}
          disabled={(!text.trim() && !files.length) || disabled || sending}
          aria-label={t("chat.send")}
          className="plate flex size-8 shrink-0 items-center justify-center rounded-sm transition-colors hover:bg-fg-2 disabled:bg-line disabled:text-fg-3 touch:size-11"
        >
          <ArrowUp className="size-4" />
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        {runningId && <p className="min-w-0 flex-1 basis-48 text-fg-3">{t("chat.queueHint")}</p>}
        <div className="ml-auto flex flex-wrap items-center justify-end gap-x-2">
          <LimitsRing />
        </div>
      </div>
      </div>
    </div>
  );
}
