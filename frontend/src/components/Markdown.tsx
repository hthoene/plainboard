import clsx from "clsx";
import { useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useArchivedTasks, useTasks } from "../api/queries";
import { AppWindow, Check, Copy } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useShell } from "../features/layout/shell-context";
import { browserAddress } from "../lib/preview";
import { Lightbox } from "./Lightbox";

// Not inside words, URL paths or entities ("a#1", "x/#1", "&#1"), but "#1/#2" links both.
const TICKET_REF = /(?<![\w#&])(?<![^\s\d]\/)#(\d+)\b/g;
const OTHER_NUMBERING = /\b(?:PR|MR|pull request|issue)\s*$/i; // numbers on GitHub, not tickets

type MdNode = { type: string; value?: string; url?: string; children?: MdNode[] };

/** Splits text into plain parts and ticket numbers. */
function ticketParts(text: string): (string | number)[] {
  const parts: (string | number)[] = [];
  let last = 0;
  for (const match of text.matchAll(TICKET_REF)) {
    if (OTHER_NUMBERING.test(text.slice(0, match.index))) continue;
    if (match.index > last) parts.push(text.slice(last, match.index));
    parts.push(Number(match[1]));
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

// A preview link written as plain text: "/preview/5173/" or "/preview/5173/docs". Not inside URLs.
const PREVIEW_REF = /(?<![\w/.:-])\/preview\/\d+\/[^\s<>"')\]]*/g;

const previewNode = (url: string): MdNode => ({ type: "link", url, children: [{ type: "text", value: url }] });

/** Splits text into plain text and links to previews and tickets. */
function linkedNodes(text: string): MdNode[] {
  const nodes: MdNode[] = [];
  const plain = (part: string) => {
    for (const piece of ticketParts(part)) nodes.push(typeof piece === "number" ? ticketNode(piece) : { type: "text", value: piece });
  };
  let last = 0;
  for (const match of text.matchAll(PREVIEW_REF)) {
    const url = match[0].replace(/[.,;:!?]+$/, ""); // the sentence's punctuation
    if (match.index > last) plain(text.slice(last, match.index));
    nodes.push(previewNode(url));
    last = match.index + url.length;
  }
  if (last < text.length) plain(text.slice(last));
  return nodes;
}

const ticketNode = (number: number): MdNode => ({ type: "link", url: `#ticket-${number}`, children: [{ type: "text", value: `#${number}` }] });

/** Remark plugin: turns #12 (also `#12`) and preview paths into links, except in other code and in existing links. */
function linkTickets() {
  const walk = (node: MdNode) => {
    node.children = node.children?.flatMap((child) => {
      if (child.type === "text") return linkedNodes(child.value ?? "");
      const code = child.type === "inlineCode" && child.value?.match(/^#(\d+)$/);
      if (code) return [ticketNode(Number(code[1]))];
      if (child.type === "inlineCode" && /^\/preview\/\d+\/\S*$/.test(child.value ?? "")) return [previewNode(child.value ?? "")];
      if (child.type !== "link" && child.type !== "linkReference") walk(child);
      return [child];
    });
  };
  return walk;
}

function TicketLink({ number, children, className }: { number: number; children: ReactNode; className?: string }) {
  const { project, openTask } = useShell();
  const board = useTasks(project.id);
  const onBoard = board.data?.find((t) => t.number === number);
  // Older tickets are archived: look there only when a number isn't on the board.
  const archived = useArchivedTasks(project.id, !!board.data && !onBoard);
  const task = onBoard ?? archived.data?.find((t) => t.number === number);
  if (!task) return <>{children}</>;
  return (
    <button
      type="button"
      title={task.title}
      // A ticket that exists stands out of the text as a chip (in colour where the design has one).
      data-part={className ? undefined : "ticket-ref"}
      onClick={(e) => {
        e.stopPropagation();
        openTask(task.id);
      }}
      className={className ?? "rounded-sm bg-sunk px-1 font-semibold whitespace-nowrap text-fg ring-1 ring-line-2 ring-inset hover:bg-line"}
    >
      {children}
    </button>
  );
}

/** Plain text with clickable ticket numbers, for places that don't render Markdown. */
export function TicketText({ text, linkClassName }: { text: string; linkClassName?: string }) {
  return (
    <>
      {ticketParts(text).map((part, i) =>
        typeof part === "number" ? (
          <TicketLink key={i} number={part} className={linkClassName}>
            #{part}
          </TicketLink>
        ) : (
          part
        ),
      )}
    </>
  );
}

function ZoomableImage({ src, alt }: { src: string; alt: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className="inline-block max-w-full cursor-zoom-in align-top">
        <img src={src} alt={alt} className="max-h-80 max-w-full rounded-sm" />
      </button>
      {open && <Lightbox images={[{ src, alt }]} onClose={() => setOpen(false)} />}
    </>
  );
}

/** A link to a preview, and next to it the way into the browser window, where the same page runs
 * at its real address (some pages only work there). */
function PreviewLink({ address, children }: { address: string; children: ReactNode }) {
  const { t } = useTranslation();
  const { openBrowser } = useShell();
  return (
    <>
      {children}
      <button
        type="button"
        onClick={() => openBrowser(address)}
        title={t("browser.openHere")}
        aria-label={t("browser.openHere")}
        className="ml-1 inline-flex size-5 items-center justify-center rounded-sm align-text-bottom text-fg-3 ring-1 ring-line-2 ring-inset hover:bg-sunk hover:text-fg"
      >
        <AppWindow className="size-3.5" />
      </button>
    </>
  );
}

/** Copies text; falls back to a hidden textarea where navigator.clipboard is missing (plain HTTP). */
async function copyText(text: string) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // fall through to the textarea
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
  } finally {
    area.remove();
  }
}

const hastText = (node: { value?: string; children?: unknown[] }): string =>
  node.value ?? (node.children as { value?: string; children?: unknown[] }[] | undefined)?.map(hastText).join("") ?? "";

/** A fenced code block with a copy button in its top-right corner (on hover where there is one, else always). */
function CodeBlock({ text, children }: { text: string; children: ReactNode }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const timer = useRef<number>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const label = t(copied ? "common.copied" : "common.copy");
  return (
    <div className="group/code relative my-2">
      {/* Right padding keeps the button off the text. */}
      <pre style={{ margin: 0, paddingRight: "2.5rem" }} className="scroll-thin">
        {children}
      </pre>
      <button
        type="button"
        title={label}
        aria-label={label}
        onClick={async () => {
          await copyText(text);
          setCopied(true);
          window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => setCopied(false), 1500);
        }}
        className="absolute top-1.5 right-1.5 inline-flex size-7 items-center justify-center rounded-sm text-fg-3 hover:bg-line hover:text-fg focus-visible:opacity-100 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover/code:opacity-100"
      >
        {copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />}
        <span role="status" className="sr-only">
          {copied ? label : ""}
        </span>
      </button>
    </div>
  );
}

export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={clsx("prose-chat break-words", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, linkTickets]}
        components={{
          a: ({ node: _node, ...props }) => {
            const ticket = props.href?.match(/^#ticket-(\d+)$/);
            if (ticket) return <TicketLink number={Number(ticket[1])}>{props.children}</TicketLink>;
            const link = <a {...props} target="_blank" rel="noreferrer" />;
            const address = browserAddress(props.href);
            return address ? <PreviewLink address={address}>{link}</PreviewLink> : link;
          },
          img: ({ src, alt }) => (typeof src === "string" ? <ZoomableImage src={src} alt={alt ?? ""} /> : null),
          pre: ({ node, children }) => (
            <CodeBlock text={(node ? hastText(node) : "").replace(/\r?\n$/, "")}>{children}</CodeBlock>
          ),
          table: ({ node: _node, ...props }) => (
            <div className="scroll-thin overflow-x-auto">
              <table {...props} />
            </div>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
