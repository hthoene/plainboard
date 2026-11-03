import clsx from "clsx";
import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useArchivedTasks, useTasks } from "../api/queries";
import { useShell } from "../features/layout/shell-context";
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

const ticketNode = (number: number): MdNode => ({ type: "link", url: `#ticket-${number}`, children: [{ type: "text", value: `#${number}` }] });

/** Remark plugin: turns #12 (also `#12`) into a link, except in other code and in existing links. */
function linkTickets() {
  const walk = (node: MdNode) => {
    node.children = node.children?.flatMap((child) => {
      if (child.type === "text") return ticketParts(child.value ?? "").map((part) => (typeof part === "number" ? ticketNode(part) : { type: "text", value: part }));
      const code = child.type === "inlineCode" && child.value?.match(/^#(\d+)$/);
      if (code) return [ticketNode(Number(code[1]))];
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
      onClick={(e) => {
        e.stopPropagation();
        openTask(task.id);
      }}
      className={className ?? "font-medium text-indigo-600 hover:underline dark:text-indigo-400"}
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
        <img src={src} alt={alt} className="max-h-80 max-w-full rounded-md" />
      </button>
      {open && <Lightbox images={[{ src, alt }]} onClose={() => setOpen(false)} />}
    </>
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
            return ticket ? <TicketLink number={Number(ticket[1])}>{props.children}</TicketLink> : <a {...props} target="_blank" rel="noreferrer" />;
          },
          img: ({ src, alt }) => (typeof src === "string" ? <ZoomableImage src={src} alt={alt ?? ""} /> : null),
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
