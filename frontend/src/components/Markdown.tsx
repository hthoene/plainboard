import clsx from "clsx";
import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTasks } from "../api/queries";
import { useShell } from "../features/layout/shell-context";
import { Lightbox } from "./Lightbox";

const TICKET_REF = /(?<![\w/#&])#(\d+)\b/g;
const OTHER_NUMBERING = /\b(?:PR|MR|pull request|issue)\s*$/i; // numbers on GitHub, not tickets

type MdNode = { type: string; value?: string; url?: string; children?: MdNode[] };

function splitTicketRefs(text: string): MdNode[] {
  const nodes: MdNode[] = [];
  let last = 0;
  for (const match of text.matchAll(TICKET_REF)) {
    if (OTHER_NUMBERING.test(text.slice(0, match.index))) continue;
    if (match.index > last) nodes.push({ type: "text", value: text.slice(last, match.index) });
    nodes.push({ type: "link", url: `#ticket-${match[1]}`, children: [{ type: "text", value: match[0] }] });
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push({ type: "text", value: text.slice(last) });
  return nodes;
}

/** Remark plugin: turns #12 into a link, except in code and in existing links. */
function linkTickets() {
  const walk = (node: MdNode) => {
    node.children = node.children?.flatMap((child) => {
      if (child.type === "text") return splitTicketRefs(child.value ?? "");
      if (child.type !== "link" && child.type !== "linkReference") walk(child);
      return [child];
    });
  };
  return walk;
}

function TicketLink({ number, children }: { number: number; children: ReactNode }) {
  const { project, openTask } = useShell();
  const task = useTasks(project.id).data?.find((t) => t.number === number);
  if (!task) return <>{children}</>;
  return (
    <button type="button" title={task.title} onClick={() => openTask(task.id)} className="font-medium text-indigo-600 hover:underline dark:text-indigo-400">
      {children}
    </button>
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
