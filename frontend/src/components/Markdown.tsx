import clsx from "clsx";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Lightbox } from "./Lightbox";

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
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
          img: ({ src, alt }) => (typeof src === "string" ? <ZoomableImage src={src} alt={alt ?? ""} /> : null),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
