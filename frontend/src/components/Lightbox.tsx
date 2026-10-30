import { ChevronLeft, ChevronRight, ExternalLink, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

export interface LightboxImage {
  src: string;
  alt: string;
}

function ControlButton({ label, onClick, autoFocus, className, children }: { label: string; onClick: () => void; autoFocus?: boolean; className?: string; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      autoFocus={autoFocus}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={`flex size-10 items-center justify-center rounded-full bg-black/50 text-white transition-colors hover:bg-black/75 focus-visible:outline-2 focus-visible:outline-white ${className ?? ""}`}
    >
      {children}
    </button>
  );
}

/** Full-size image view with optional gallery navigation. */
export function Lightbox({ images, start = 0, onClose }: { images: LightboxImage[]; start?: number; onClose: () => void }) {
  const { t } = useTranslation();
  const [index, setIndex] = useState(start);
  const current = Math.min(index, images.length - 1);
  const image = images[current];
  const many = images.length > 1;
  const step = (by: number) => setIndex((current + by + images.length) % images.length);

  useEffect(() => {
    // Capture + stopPropagation: Escape closes only this view, not the modal listening on window.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft" && many) step(-1);
      else if (e.key === "ArrowRight" && many) step(1);
      else return;
      e.preventDefault();
      e.stopPropagation();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  });

  if (!image) return null;
  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={image.alt}
      // React events bubble through portals: keep them away from the surrounding modal.
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => {
        e.stopPropagation();
        onClose();
      }}
      className="fixed inset-0 z-[70] flex flex-col bg-zinc-950/90 pb-[env(safe-area-inset-bottom)]"
    >
      <div className="flex shrink-0 items-center gap-3 p-3 text-sm text-zinc-100">
        <span className="min-w-0 flex-1 truncate">
          {image.alt}
          {many && <span className="ml-2 text-zinc-400">{t("lightbox.position", { current: current + 1, total: images.length })}</span>}
        </span>
        <a
          href={image.src}
          target="_blank"
          rel="noreferrer"
          aria-label={t("lightbox.openOriginal")}
          title={t("lightbox.openOriginal")}
          onClick={(e) => e.stopPropagation()}
          className="flex size-10 items-center justify-center rounded-full bg-black/50 text-white transition-colors hover:bg-black/75 focus-visible:outline-2 focus-visible:outline-white"
        >
          <ExternalLink className="size-4" />
        </a>
        <ControlButton label={t("common.close")} onClick={onClose} autoFocus>
          <X className="size-5" />
        </ControlButton>
      </div>
      <div className="relative flex min-h-0 flex-1 items-center justify-center px-3 pb-3">
        <img src={image.src} alt={image.alt} onClick={(e) => e.stopPropagation()} className="max-h-full max-w-full rounded object-contain" />
        {many && (
          <>
            <ControlButton label={t("lightbox.previous")} onClick={() => step(-1)} className="absolute left-3 top-1/2 -translate-y-1/2">
              <ChevronLeft className="size-5" />
            </ControlButton>
            <ControlButton label={t("lightbox.next")} onClick={() => step(1)} className="absolute right-3 top-1/2 -translate-y-1/2">
              <ChevronRight className="size-5" />
            </ControlButton>
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
