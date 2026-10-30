import clsx from "clsx";
import { AlertCircle, Archive, Bell, CalendarClock, ChevronRight, ClipboardList, HelpCircle, Mail, RefreshCw, Ticket, Wrench } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Agent, Message } from "../../api/types";
import { Markdown } from "../../components/Markdown";
import { formatTokens } from "../../lib/format";
import { useShell } from "../layout/shell-context";

/** `details` shows tool activity, compression markers and full event cards; otherwise the chat
 * stays clean (messages only, events as one-line system notes). */
export function MessageItem({ message, agent, details }: { message: Message; agent: Agent; details: boolean }) {
  switch (message.kind) {
    case "tool":
      return details ? <ToolLine message={message} /> : null;
    case "summary":
      return details ? <SummaryDivider message={message} /> : null;
    case "error":
      return (
        <div className="flex items-start gap-2 rounded-md bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/40 dark:text-red-300">
          <AlertCircle className="mt-px size-3.5 shrink-0" />
          <span className="break-words whitespace-pre-wrap">{message.content}</span>
        </div>
      );
  }
  if (message.role === "user") return <UserMessage message={message} details={details} />;
  return (
    <div className="flex gap-2.5">
      <span className="mt-1 size-5 shrink-0 rounded-full text-center text-[10px] leading-5 font-semibold text-white" style={{ background: agent.color }}>
        {agent.name[0]?.toUpperCase()}
      </span>
      <Markdown className="min-w-0 flex-1 text-sm">{message.content}</Markdown>
    </div>
  );
}

function UserMessage({ message, details }: { message: Message; details: boolean }) {
  const { t } = useTranslation();
  const { agentById, openTask } = useShell();
  const trigger: string = message.meta.trigger ?? "chat";
  const inbox: string[] = message.meta.inbox ?? [];
  const source = agentById(message.meta.source_agent_id);

  const briefing: string[] = message.meta.briefing_update ?? [];
  const briefingNote = details && briefing.length > 0 && (
    <SystemNote icon={<RefreshCw className="size-3" />} label={t("chat.briefingUpdated", { sections: briefing.map((k) => t(`chat.section.${k}`, k)).join(", ") })} />
  );
  const updates = inbox.length > 0 && !details ? (
    <SystemNote icon={<Bell className="size-3" />} label={inbox.length === 1 ? firstLine(inbox[0]) : t("chat.updates", { count: inbox.length })}>
      <ul className="list-disc space-y-1 pl-4 text-left">
        {inbox.map((item, i) => (
          <li key={i} className="break-words">
            {item}
          </li>
        ))}
      </ul>
    </SystemNote>
  ) : inbox.length > 0 && (
    <EventCard icon={<Bell className="size-3.5" />} title={t("chat.updates", { count: inbox.length })} defaultOpen={trigger === "wake"}>
      <ul className="list-disc space-y-1 pl-4">
        {inbox.map((item, i) => (
          <li key={i} className="break-words">
            {item}
          </li>
        ))}
      </ul>
    </EventCard>
  );

  if (trigger === "chat") {
    return (
      <>
        {briefingNote}
        {updates}
        <div className="flex justify-end">
          <UserBubble text={message.content} />
        </div>
      </>
    );
  }
  if (trigger === "wake")
    return (
      <>
        {briefingNote}
        {updates}
      </>
    );

  const cards: Record<string, { icon: ReactNode; title: string }> = {
    ticket: { icon: <Ticket className="size-3.5" />, title: t("chat.ticketHandoff") },
    assignment: { icon: <ClipboardList className="size-3.5" />, title: t("chat.assignmentFrom", { name: source?.name ?? t("chat.user") }) },
    ask: { icon: <HelpCircle className="size-3.5" />, title: t("chat.questionFrom", { name: source?.name ?? "?" }) },
    message: { icon: <Mail className="size-3.5" />, title: t("chat.messageFrom", { name: source?.name ?? "?" }) },
    schedule: { icon: <CalendarClock className="size-3.5" />, title: t("chat.scheduled") },
  };
  const card = cards[trigger] ?? { icon: <Mail className="size-3.5" />, title: trigger };
  if (!details) {
    return (
      <>
        {briefingNote}
        {updates}
        <SystemNote
          icon={card.icon}
          label={card.title}
          action={
            message.meta.task_id ? (
              <button className="text-indigo-600 hover:underline dark:text-indigo-400" onClick={() => openTask(message.meta.task_id)}>
                {t("chat.openTicket")}
              </button>
            ) : undefined
          }
        >
          <Markdown className="text-left text-[13px]">{message.content}</Markdown>
        </SystemNote>
      </>
    );
  }
  return (
    <>
      {briefingNote}
      {updates}
      <EventCard
        icon={card.icon}
        title={card.title}
        action={
          message.meta.task_id ? (
            <button className="text-indigo-600 hover:underline dark:text-indigo-400" onClick={() => openTask(message.meta.task_id)}>
              {t("chat.openTicket")}
            </button>
          ) : undefined
        }
      >
        <Markdown className="text-[13px]">{message.content}</Markdown>
      </EventCard>
    </>
  );
}

function firstLine(text: string): string {
  const line = text.split(/\r?\n/)[0];
  return line.length > 110 ? `${line.slice(0, 109)}…` : line;
}

/** Centered, muted one-liner (like system notes in messengers); click to reveal the content. */
function SystemNote({ icon, label, children, action }: { icon: ReactNode; label: string; children?: ReactNode; action?: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col items-center gap-1.5 py-0.5">
      <div className="flex max-w-full items-center gap-2 text-xs text-zinc-400">
        <button onClick={() => children && setOpen(!open)} className="flex min-w-0 items-center gap-1.5 hover:text-zinc-600 dark:hover:text-zinc-300">
          <span className="shrink-0 [&>svg]:size-3">{icon}</span>
          <span className="truncate">{label}</span>
        </button>
        {action}
      </div>
      {open && children && <div className="w-full rounded-lg bg-zinc-50 px-3 py-2 text-xs text-zinc-700 dark:bg-zinc-800/40 dark:text-zinc-300">{children}</div>}
    </div>
  );
}

const COLLAPSE_AT = 1200;

function UserBubble({ text }: { text: string }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const long = text.length > COLLAPSE_AT;
  return (
    <div className="max-w-[85%] rounded-2xl rounded-br-md bg-indigo-600 px-3.5 py-2 text-sm break-words whitespace-pre-wrap text-white">
      {long && !expanded ? `${text.slice(0, COLLAPSE_AT)}…` : text}
      {long && (
        <button onClick={() => setExpanded(!expanded)} className="mt-1 block text-xs text-indigo-200 hover:text-white">
          {expanded ? t("chat.showLess") : t("chat.showMore", { count: text.length })}
        </button>
      )}
    </div>
  );
}

function EventCard({ icon, title, children, action, defaultOpen = false }: { icon: ReactNode; title: string; children: ReactNode; action?: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-lg bg-zinc-50 text-xs ring-1 ring-zinc-200/70 dark:bg-zinc-800/40 dark:ring-zinc-800">
      <div className="flex items-center gap-2 px-3 py-2">
        <button onClick={() => setOpen(!open)} className="flex min-w-0 flex-1 items-center gap-2 text-left font-medium text-zinc-600 dark:text-zinc-300">
          <ChevronRight className={clsx("size-3.5 shrink-0 text-zinc-400 transition-transform", open && "rotate-90")} />
          <span className="text-zinc-400">{icon}</span>
          <span className="truncate">{title}</span>
        </button>
        {action}
      </div>
      {open && <div className="border-t border-zinc-200/70 px-3 py-2 text-zinc-700 dark:border-zinc-800 dark:text-zinc-300">{children}</div>}
    </div>
  );
}

function toolLabel(name: string): string {
  return name.replace(/^mcp__plainboard__/, "").replace(/^mcp__([^_]+)__/, "$1 · ");
}

function ToolLine({ message }: { message: Message }) {
  const [open, setOpen] = useState(false);
  const { name, input, result, is_error } = message.meta as { name?: string; input?: string; result?: string; is_error?: boolean };
  const pending = result === undefined;
  return (
    <div className="text-xs">
      <button onClick={() => setOpen(!open)} className="flex max-w-full items-center gap-1.5 rounded px-1 py-0.5 text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-800">
        <Wrench className={clsx("size-3 shrink-0", is_error ? "text-red-500" : pending ? "animate-pulse text-indigo-500" : "text-zinc-400")} />
        <span className="font-mono">{toolLabel(name ?? message.content)}</span>
        {input && input !== "{}" && <span className="truncate text-zinc-400">{input}</span>}
      </button>
      {open && (
        <div className="mt-1 ml-5 space-y-1.5 rounded-md bg-zinc-50 p-2 font-mono text-[11px] dark:bg-zinc-800/50">
          {input && <pre className="break-words whitespace-pre-wrap text-zinc-600 dark:text-zinc-400">{input}</pre>}
          {result !== undefined && (
            <pre className={clsx("max-h-64 overflow-auto break-words whitespace-pre-wrap", is_error ? "text-red-600" : "text-zinc-800 dark:text-zinc-200")}>{result || "(empty)"}</pre>
          )}
        </div>
      )}
    </div>
  );
}

function SummaryDivider({ message }: { message: Message }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const label = message.meta.provider ? "chat.compactedByProvider" : "chat.compressed";
  return (
    <div className="py-1">
      <button onClick={() => setOpen(!open)} disabled={!message.content} className="flex w-full items-center gap-2 text-[11px] text-zinc-400 enabled:hover:text-zinc-600 dark:enabled:hover:text-zinc-300">
        <span className="h-px flex-1 bg-zinc-200 dark:bg-zinc-800" />
        <Archive className="size-3" />
        {t(label, { tokens: formatTokens(message.meta.tokens_before ?? 0) })}
        <span className="h-px flex-1 bg-zinc-200 dark:bg-zinc-800" />
      </button>
      {open && (
        <div className="mt-2 rounded-lg bg-zinc-50 p-3 dark:bg-zinc-800/40">
          <Markdown className="text-xs">{message.content}</Markdown>
        </div>
      )}
    </div>
  );
}
