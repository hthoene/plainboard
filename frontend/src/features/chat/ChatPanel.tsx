import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { ArrowUp, ChevronDown, Eye, EyeOff, Loader2, Maximize2, MoreHorizontal, Paperclip, Square, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { useStreamingText } from "../../api/events";
import { qk, useConversation, useRuns } from "../../api/queries";
import type { Attachment, Conversation } from "../../api/types";
import { Markdown } from "../../components/Markdown";
import { AgentDot, IconButton, Select, Spinner } from "../../components/ui";
import { formatCost, formatTokens } from "../../lib/format";
import { useStored } from "../../lib/hooks";
import { useShell } from "../layout/shell-context";
import { MessageItem } from "./MessageItem";

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
  const { project, agents, agentById } = useShell();
  const qc = useQueryClient();
  const agent = agentById(agentId);
  const conversation = useConversation(agentId);
  const runs = useRuns(project.id);
  const streaming = useStreamingText(agentId);
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [details, setDetails] = useStored<boolean>("plainboard.chatDetails", false);
  const [files, setFiles] = useState<File[]>([]);

  const activeRuns = (runs.data ?? []).filter((r) => r.agent_id === agentId && (r.status === "queued" || r.status === "running"));
  const running = activeRuns.find((r) => r.status === "running");
  const queued = activeRuns.filter((r) => r.status === "queued" && r.trigger === "chat");
  const messages = conversation.data?.messages ?? [];

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
      <header className="flex items-center gap-2 border-b border-zinc-100 px-3 py-2 sm:px-4 dark:border-zinc-800">
        <div className="min-w-0 flex-1">
          <Select
            value={agentId}
            onChange={(e) => onSelectAgent(Number(e.target.value))}
            aria-label={t("chat.selectAgent")}
            trigger={
              <span className="flex items-center gap-2">
                <AgentDot color={agent.color} status={agent.status} />
                <span className="block min-w-0">
                  <span className="flex items-center gap-1 text-sm font-semibold">
                    <span className="truncate">{agent.name}</span>
                    <ChevronDown className="size-3.5 shrink-0 text-zinc-400" />
                  </span>
                  <span className="block truncate text-xs text-zinc-500">
                    {agent.role} · {agent.model}
                  </span>
                </span>
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
        <IconButton label={details ? t("chat.hideDetails") : t("chat.showDetails")} onClick={() => setDetails(!details)} className={clsx(details && "text-indigo-600 dark:text-indigo-400")}>
          {details ? <Eye className="size-4" /> : <EyeOff className="size-4" />}
        </IconButton>
        <ChatMenu
          agentId={agentId}
          cost={agent.total_cost_usd}
          context={`${formatTokens(conversation.data?.context_tokens ?? agent.context_tokens)} / ${formatTokens(agent.context_limit_tokens)}`}
          disabled={!!running}
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

      <div ref={scroller} onScroll={onScroll} className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
        <div className={clsx("space-y-3", column)}>
        {conversation.isLoading && (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        )}
        {conversation.data?.has_more && (
          <div className="flex justify-center">
            <button onClick={loadOlder} className="text-xs text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200" disabled={loadingOlder}>
              {loadingOlder ? t("common.loading") : t("chat.loadOlder")}
            </button>
          </div>
        )}
        {!conversation.isLoading && messages.length === 0 && !queued.length && <ChatIntro isManager={agent.is_manager} />}
        {messages.map((m) => (
          <MessageItem key={m.id} message={m} agent={agent} details={details} />
        ))}
        {queued.map((r) => (
          <div key={r.id} className="flex justify-end">
            <div className="max-w-[85%] rounded-2xl rounded-br-md bg-indigo-600/60 px-3.5 py-2 text-sm whitespace-pre-wrap text-white">
              {r.input_text}
              {r.attachment_ids.length > 0 && (
                <span className="flex items-center gap-1 text-xs text-indigo-100">
                  <Paperclip className="size-3" />
                  {t("chat.fileCount", { count: r.attachment_ids.length })}
                </span>
              )}
            </div>
          </div>
        ))}
        {running && streaming && (
          <div className="text-sm">
            <Markdown>{streaming}</Markdown>
          </div>
        )}
        </div>
      </div>

      <Composer
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
        files={files}
        onFilesChange={setFiles}
      />
    </div>
  );
}

function ChatIntro({ isManager }: { isManager: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="mx-auto max-w-xs space-y-2 py-10 text-center text-sm text-zinc-500">
      <p className="font-medium text-zinc-700 dark:text-zinc-300">{isManager ? t("chat.introManagerTitle") : t("chat.introAgentTitle")}</p>
      <p>{isManager ? t("chat.introManager") : t("chat.introAgent")}</p>
    </div>
  );
}

function ContextGauge({ used, limit, base }: { used: number; limit: number; base: number }) {
  const { t } = useTranslation();
  const ratio = limit ? Math.min(1, used / limit) : 0;
  const color = ratio < 0.6 ? "bg-emerald-500" : ratio < 0.9 ? "bg-amber-500" : "bg-red-500";
  return (
    <div className="hidden flex-col items-end gap-1 sm:flex" title={t("chat.contextTitle", { used: formatTokens(used), limit: formatTokens(limit), base: formatTokens(base) })}>
      <span className="text-[10px] text-zinc-500 tabular-nums">
        {formatTokens(used)} / {formatTokens(limit)}
      </span>
      <span className="h-1 w-16 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
        <span className={clsx("block h-full rounded-full transition-all", color)} style={{ width: `${Math.max(2, ratio * 100)}%` }} />
      </span>
    </div>
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
        <div className="absolute top-8 right-0 z-40 w-64 rounded-lg bg-white p-1 text-sm shadow-lg ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-800">
          <MenuItem label={t("chat.compressNow")} hint={t("chat.compressHint")} busy={busy === "compress"} disabled={disabled} onClick={() => action("compress")} />
          <MenuItem label={t("chat.resetSession")} hint={t("chat.resetHint")} busy={busy === "reset-session"} disabled={disabled} onClick={() => action("reset-session")} />
          <div className="border-t border-zinc-100 px-2 py-1.5 text-xs text-zinc-500 dark:border-zinc-800">{t("chat.context", { context })} · {t("chat.totalCost", { cost: formatCost(cost) })}</div>
          {note && <div className="px-2 pb-1.5 text-xs text-amber-600">{note}</div>}
        </div>
      )}
    </div>
  );
}

function MenuItem({ label, hint, onClick, busy, disabled }: { label: string; hint: string; onClick: () => void; busy: boolean; disabled: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled || busy} className="block w-full rounded-md px-2 py-1.5 text-left hover:bg-zinc-100 disabled:opacity-50 dark:hover:bg-zinc-800">
      <div className="flex items-center gap-1.5 font-medium">
        {busy && <Loader2 className="size-3 animate-spin" />}
        {label}
      </div>
      <div className="text-xs text-zinc-500">{hint}</div>
    </button>
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
        <img src={preview} alt={name} className="size-16 rounded-lg object-cover ring-1 ring-zinc-200 dark:ring-zinc-800" />
      ) : (
        <div className="flex h-8 items-center gap-1.5 rounded-lg bg-zinc-100 pr-7 pl-2 text-xs text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
          <Paperclip className="size-3 shrink-0" />
          <span className="truncate">{name}</span>
        </div>
      )}
      <button
        type="button"
        onClick={onRemove}
        aria-label={t("chat.removeFile", { name })}
        className={clsx("absolute flex size-5 items-center justify-center rounded-full text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100", preview ? "top-0.5 right-0.5 bg-white/90 dark:bg-zinc-900/90" : "top-1.5 right-1")}
      >
        <X className="size-3" />
      </button>
    </div>
  );
}

function Composer({
  agentId,
  agentName,
  runningId,
  disabled,
  className,
  status,
  files,
  onFilesChange,
}: {
  agentId: number;
  agentName: string;
  runningId?: number;
  disabled: boolean;
  className?: string;
  status?: string;
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
    <div className="border-t border-zinc-100 p-3 dark:border-zinc-800">
      <div className={className}>
      {error && <p className="mb-2 text-xs text-red-600">{error}</p>}
      {status && (
        <div role="status" className="mb-2 flex items-center gap-2 px-1 text-xs font-medium text-indigo-600 dark:text-indigo-400">
          <Loader2 className="size-3.5 shrink-0 animate-spin" />
          <span className="truncate">{status}</span>
        </div>
      )}
      {files.length > 0 && (
        <div className="mb-2 flex flex-wrap items-end gap-2">
          {files.map((file, i) => (
            <PendingFile key={`${file.name}-${file.lastModified}-${i}`} file={file} onRemove={() => onFilesChange(files.filter((_, j) => j !== i))} />
          ))}
        </div>
      )}
      <div className="flex items-end gap-2 rounded-xl bg-zinc-50 p-1.5 ring-1 ring-zinc-200 focus-within:ring-2 focus-within:ring-indigo-500 dark:bg-zinc-950 dark:ring-zinc-800">
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
        <IconButton label={t("chat.attachFiles")} disabled={disabled} onClick={() => picker.current?.click()} className="size-8 shrink-0 rounded-lg">
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
          className="scroll-thin max-h-[200px] min-h-8 flex-1 resize-none bg-transparent px-2 py-1.5 text-base outline-none placeholder:text-zinc-400 sm:text-sm"
        />
        {runningId ? (
          <IconButton label={t("chat.stop")} onClick={() => api.post(`/runs/${runningId}/cancel`)} className="size-8 rounded-lg bg-zinc-200 dark:bg-zinc-800">
            <Square className="size-3.5 fill-current" />
          </IconButton>
        ) : null}
        <button
          onClick={send}
          disabled={(!text.trim() && !files.length) || disabled || sending}
          aria-label={t("chat.send")}
          className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-indigo-600 text-white transition-colors hover:bg-indigo-500 disabled:bg-zinc-300 dark:disabled:bg-zinc-800"
        >
          <ArrowUp className="size-4" />
        </button>
      </div>
      {runningId && <p className="mt-1.5 text-[11px] text-zinc-400">{t("chat.queueHint")}</p>}
      </div>
    </div>
  );
}
