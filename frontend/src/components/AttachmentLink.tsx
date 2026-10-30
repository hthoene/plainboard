import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Markdown } from "./Markdown";
import { ErrorText, Modal, Spinner } from "./ui";

interface FileRef {
  id: number;
  filename: string;
  content_type: string;
}

const isMarkdown = (file: FileRef) => /\.(md|markdown)$/i.test(file.filename) || file.content_type.startsWith("text/markdown");

/** Link to an attachment. Markdown files open in the built-in viewer, everything else in the browser. */
export function AttachmentLink({
  file,
  download,
  title,
  className,
  children,
}: {
  file: FileRef;
  download?: boolean;
  title?: string;
  className?: string;
  children: ReactNode;
}) {
  const [viewing, setViewing] = useState(false);
  if (!isMarkdown(file)) {
    return download ? (
      <a href={`/api/attachments/${file.id}?inline=false`} title={title} className={className}>
        {children}
      </a>
    ) : (
      <a href={`/api/attachments/${file.id}`} target="_blank" rel="noreferrer" title={title} className={className}>
        {children}
      </a>
    );
  }
  return (
    <>
      <button type="button" onClick={() => setViewing(true)} title={title} className={`text-left ${className ?? ""}`}>
        {children}
      </button>
      {viewing && <MarkdownViewer file={file} onClose={() => setViewing(false)} />}
    </>
  );
}

function MarkdownViewer({ file, onClose }: { file: FileRef; onClose: () => void }) {
  const { t } = useTranslation();
  const content = useQuery({
    queryKey: ["attachment-text", file.id],
    queryFn: async () => {
      const resp = await fetch(`/api/attachments/${file.id}`);
      if (!resp.ok) throw new Error(resp.statusText);
      return resp.text();
    },
  });

  useEffect(() => {
    // Capture + stopPropagation: Escape closes only this view, not a modal listening on window.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return createPortal(
    <Modal
      open
      onClose={onClose}
      width="max-w-3xl"
      title={
        <div className="flex items-center gap-2">
          <span className="truncate">{file.filename}</span>
          <a
            href={`/api/attachments/${file.id}?inline=false`}
            aria-label={t("common.download")}
            title={t("common.download")}
            className="shrink-0 rounded-md p-1 text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            <Download className="size-4" />
          </a>
        </div>
      }
    >
      {content.isLoading && <Spinner />}
      {content.error && <ErrorText error={content.error} />}
      {content.data !== undefined && <Markdown>{content.data}</Markdown>}
    </Modal>,
    document.body,
  );
}
