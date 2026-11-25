import clsx from "clsx";
import {
  Bold,
  Check,
  ChevronLeft,
  CircleAlert,
  Clock,
  Code,
  ExternalLink,
  FileSymlink,
  Heading2,
  History,
  Italic,
  Link2,
  List,
  ListOrdered,
  Loader2,
  MessageSquareQuote,
  Moon,
  Paperclip,
  RotateCcw,
  Sun,
  SunMoon,
  Table,
  Tag,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import type { Attachment, DocPage, DocSite, DocTranslationItem } from "../../api/types";
import { Button, ErrorText, Field, FieldRow, IconButton, Input, Modal, Segmented, Select, Spinner, Textarea, Toggle } from "../../components/ui";
import { formatDateTime, timeAgo } from "../../lib/format";
import { useAutoGrow, useDismiss, useMediaQuery } from "../../lib/hooks";
import { useDraft } from "../../lib/drafts";
import { diffLines, type Row } from "../chat/ToolCode";
import { DocMarkdown } from "./DocMarkdown";
import { base, rel, releaseHref, releaseOf, useDocOutline, useDocPage, useDocsRefresh, useDocTranslation, useDocVersion, useDocVersions, useFindPages, useRelease } from "./queries";

type Mode = "write" | "preview" | "split";

/** A doc page: its text in Markdown with a preview as the site shows it, files and pictures to drop in, links to
 * other pages, its history with what each version changed, and its translations. Viewers only read it. */
export function PageEditor({ pid, site, pageId, canWrite, onBack, onOpenPage }: { pid: number; site: DocSite; pageId: number; canWrite: boolean; onBack?: () => void; onOpenPage: (id: number) => void }) {
  const { t } = useTranslation();
  const page = useDocPage(pid, pageId);
  const version = releaseOf(site, useRelease());
  if (page.isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }
  if (!page.data) {
    return (
      <div className="flex-1 p-6">
        <ErrorText error={version && !version.working ? t("docs.releases.notInVersion", { name: version.name }) : (page.error ?? t("docs.page.gone"))} />
      </div>
    );
  }
  return <Editor key={page.data.id} pid={pid} site={site} page={page.data} canWrite={canWrite} onBack={onBack} onOpenPage={onOpenPage} />;
}

function Editor({ pid, site, page, canWrite, onBack, onOpenPage }: { pid: number; site: DocSite; page: DocPage; canWrite: boolean; onBack?: () => void; onOpenPage: (id: number) => void }) {
  const { t } = useTranslation();
  const wide = useMediaQuery("(min-width: 1280px)");
  const refresh = useDocsRefresh(pid);
  const release = useRelease();
  const version = releaseOf(site, release);
  const draftKey = canWrite ? `doc:${page.id}:${release ?? ""}` : null;
  const [title, setTitle] = useDraft(draftKey && `${draftKey}:title`, page.title);
  const [body, setBody] = useDraft(draftKey, page.body);
  const [reason, setReason] = useState("");
  const [seen, setSeen] = useState(page.updated_at);
  const [mode, setMode] = useState<Mode>(canWrite ? (wide ? "split" : "write") : "preview");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [history, setHistory] = useState(false);
  const [translation, setTranslation] = useState<string | null>(null);
  const [details, setDetails] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const dirty = title !== page.title || body !== page.body;
  const changedMeanwhile = page.updated_at !== seen && dirty;

  // The doc agent (or someone else) saved meanwhile: what is not being edited here follows at once.
  useEffect(() => {
    if (!dirty) {
      setTitle(page.title);
      setBody(page.body);
      setSeen(page.updated_at);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page.updated_at]);
  useEffect(() => {
    if (!wide && mode === "split") setMode("write");
  }, [wide, mode]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.patch(rel(`${base(pid)}/pages/${page.id}`, release), { title: title.trim() || page.title, body, reason: reason.trim() });
      setReason("");
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2000);
      await refresh();
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  };
  // Ctrl+S saves.
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && canWrite) {
        e.preventDefault();
        if (dirtyRef.current) void saveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canWrite]);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  const href = `${releaseHref(site, version)}${page.path}`;
  const preview = <DocMarkdown text={body} onOpenPage={onOpenPage} />;

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {/* Head: back (phones), the title, what to do with the page. */}
      <div className="flex shrink-0 items-center gap-1.5 border-b border-line px-2 py-1.5 sm:px-3">
        {onBack && (
          <IconButton label={t("docs.back")} onClick={onBack} className="md:hidden">
            <ChevronLeft className="size-4" />
          </IconButton>
        )}
        {canWrite ? (
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            aria-label={t("docs.page.title")}
            className="h-9 min-w-0 flex-1 rounded-sm bg-transparent px-1.5 font-extrabold outline-none hover:bg-sunk focus:bg-sunk"
          />
        ) : (
          <h2 className="min-w-0 flex-1 truncate px-1.5 font-extrabold">{page.title}</h2>
        )}
        <IconButton label={t("docs.page.history", { count: page.versions })} onClick={() => setHistory(true)}>
          <History className="size-4" />
        </IconButton>
        {site.published && (
          <a href={href} target="_blank" rel="noreferrer" title={t("docs.page.onSite")} aria-label={t("docs.page.onSite")} className="inline-flex size-7 items-center justify-center rounded-sm text-fg-3 hover:bg-sunk hover:text-fg touch:size-11">
            <ExternalLink className="size-4" />
          </a>
        )}
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        <div className="space-y-3 px-3 pt-3 sm:px-4">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-fg-3">
            {version && site.releases.length > 1 && (
              <span className={clsx("inline-flex items-center gap-1 rounded-sm px-1.5 font-medium ring-1 ring-inset", version.working ? "text-fg-2 ring-line-2" : "bg-you-bg text-you-fg ring-you")}>
                <Tag className="size-3.5" aria-hidden />
                {version.name}
              </span>
            )}
            <span className="truncate font-mono">/{page.path}</span>
            <span>{t("docs.page.edited", { who: page.edited_by || "–", when: timeAgo(page.edited_at) })}</span>
            <button type="button" className="hover:text-fg hover:underline" onClick={() => setHistory(true)}>
              {t("docs.page.versions", { count: page.versions })}
            </button>
            {canWrite && (
              <button type="button" className="hover:text-fg hover:underline" onClick={() => setDetails((d) => !d)} aria-expanded={details}>
                {t("docs.page.settings")}
              </button>
            )}
          </div>
          {version && !version.working && <ReleasedNote site={site} name={version.name} canWrite={canWrite} />}
          {page.flagged_at && <FlagBanner pid={pid} page={page} canWrite={canWrite} />}
          {page.translations.length > 0 && <Translations items={page.translations} onOpen={setTranslation} />}
          {details && canWrite && <PageDetails pid={pid} page={page} />}
          {changedMeanwhile && (
            <p role="status" className="flex flex-wrap items-center gap-2 rounded-sm bg-you-bg px-3 py-2 text-you-fg">
              <CircleAlert className="size-4 shrink-0" />
              {t("docs.page.changedMeanwhile", { who: page.edited_by })}
              <Button
                size="sm"
                onClick={() => {
                  setTitle(page.title);
                  setBody(page.body);
                  setSeen(page.updated_at);
                }}
              >
                {t("docs.page.takeTheirs")}
              </Button>
            </p>
          )}
        </div>

        {canWrite && (
          <div className="sticky top-0 z-10 mt-3 flex items-center gap-2 border-y border-line bg-bg px-2 py-1 sm:px-3">
            {mode !== "preview" && <Toolbar pid={pid} site={site} area={area} setBody={setBody} />}
            <div className="flex-1" />
            <Segmented
              value={mode}
              onChange={setMode}
              aria-label={t("docs.page.mode")}
              options={[
                { value: "write", label: t("docs.page.write") },
                ...(wide ? [{ value: "split" as Mode, label: t("docs.page.split") }] : []),
                { value: "preview", label: t("docs.page.preview") },
              ]}
            />
          </div>
        )}

        <div className={clsx("min-h-[50vh]", mode === "split" && "grid grid-cols-2 divide-x divide-line")}>
          {mode !== "preview" && canWrite && (
            <MarkdownArea pid={pid} site={site} area={area} value={body} onChange={setBody} />
          )}
          {mode !== "write" && <div className="min-w-0 px-4 py-5 sm:px-6">{body.trim() ? preview : <p className="text-fg-3">{t("docs.page.emptyBody")}</p>}</div>}
        </div>
      </div>

      {canWrite && (dirty || saved || error !== null) && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-line px-3 py-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
          {dirty && (
            <>
              <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("docs.page.reason")} aria-label={t("docs.page.reasonLabel")} className="min-w-40 flex-1" />
              <Button
                variant="ghost"
                onClick={() => {
                  setTitle(page.title);
                  setBody(page.body);
                }}
              >
                {t("docs.page.discard")}
              </Button>
            </>
          )}
          {!dirty && saved && (
            <span role="status" className="flex flex-1 items-center gap-1 text-ok">
              <Check className="size-4" /> {t("settingsPage.saved")}
            </span>
          )}
          <ErrorText error={error} />
          {dirty && (
            <Button variant="primary" onClick={save} loading={saving} title="Ctrl+S">
              {t("common.save")}
            </Button>
          )}
        </div>
      )}

      {history && <HistoryDialog pid={pid} page={page} canWrite={canWrite} onClose={() => setHistory(false)} />}
      {translation && <TranslationDialog pid={pid} site={site} page={page} lang={translation} canWrite={canWrite} onClose={() => setTranslation(null)} />}
    </div>
  );
}

// --- the text -------------------------------------------------------------------------------------------------

/** Puts text around the selection (or at the cursor) and selects what is between. */
function wrap(area: HTMLTextAreaElement, setBody: (v: string) => void, before: string, after = "", fallback = "") {
  const { selectionStart: s, selectionEnd: e, value } = area;
  const inner = value.slice(s, e) || fallback;
  const next = value.slice(0, s) + before + inner + after + value.slice(e);
  setBody(next);
  requestAnimationFrame(() => {
    area.focus();
    area.setSelectionRange(s + before.length, s + before.length + inner.length);
  });
}

/** Puts a block on lines of its own at the cursor. */
function insertBlock(area: HTMLTextAreaElement, setBody: (v: string) => void, block: string) {
  const { selectionStart: s, selectionEnd: e, value } = area;
  const lead = s === 0 || value[s - 1] === "\n" ? (s > 1 && value[s - 2] !== "\n" ? "\n" : "") : "\n\n";
  const text = `${lead}${block}\n`;
  setBody(value.slice(0, s) + text + value.slice(e));
  requestAnimationFrame(() => {
    area.focus();
    area.setSelectionRange(s + text.length, s + text.length);
  });
}

/** Every line of the selection starts with `prefix`. */
function prefixLines(area: HTMLTextAreaElement, setBody: (v: string) => void, prefix: (i: number) => string) {
  const { selectionStart: s, selectionEnd: e, value } = area;
  const start = value.lastIndexOf("\n", s - 1) + 1;
  const lines = value.slice(start, e).split("\n").map((l, i) => prefix(i) + l);
  const text = lines.join("\n");
  setBody(value.slice(0, start) + text + value.slice(e));
  requestAnimationFrame(() => {
    area.focus();
    area.setSelectionRange(start, start + text.length);
  });
}

/** ``mode``: a picture shown only in light or dark mode (#125); files are the same in both. */
function fileMarkdown(file: Attachment, mode?: "light" | "dark"): string {
  const name = file.filename.replace(/[[\]]/g, "");
  const image = file.content_type.startsWith("image/");
  return `${image ? "!" : ""}[${name}](doc-file:${file.id}${image && mode ? `#${mode}` : ""})`;
}

export async function upload(pid: number, site: DocSite, files: File[]): Promise<Attachment[]> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  return api.post<Attachment[]>(`${base(pid)}/sites/${site.id}/files`, form);
}

export function MarkdownArea({ pid, site, area, value, onChange }: { pid: number; site: DocSite; area: RefObject<HTMLTextAreaElement | null>; value: string; onChange: (v: string) => void }) {
  useAutoGrow(area, value);
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const drop = async (files: File[]) => {
    if (!files.length || !area.current) return;
    setBusy(true);
    setError("");
    try {
      const done = await upload(pid, site, files);
      insertBlock(area.current, onChange, done.map((f) => fileMarkdown(f)).join("\n"));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="relative min-w-0">
      <textarea
        ref={area}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onPaste={(e) => {
          const files = Array.from(e.clipboardData.files);
          if (files.length) {
            e.preventDefault();
            void drop(files);
          }
        }}
        onDrop={(e) => {
          const files = Array.from(e.dataTransfer.files);
          if (files.length) {
            e.preventDefault();
            void drop(files);
          }
        }}
        onKeyDown={(e) => {
          if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey) {
            e.preventDefault();
            const el = e.currentTarget;
            const { selectionStart: s, selectionEnd: end } = el;
            onChange(el.value.slice(0, s) + "  " + el.value.slice(end));
            requestAnimationFrame(() => el.setSelectionRange(s + 2, s + 2));
          }
        }}
        spellCheck
        aria-label={t("docs.page.body")}
        placeholder={t("docs.page.bodyPlaceholder")}
        className="block min-h-[60vh] w-full resize-none bg-transparent px-4 py-4 font-mono leading-relaxed text-fg outline-none placeholder:text-fg-3 sm:px-5"
      />
      {(busy || error) && (
        <p role="status" className={clsx("absolute right-3 bottom-3 flex items-center gap-1.5 rounded-sm bg-bg px-2 py-1 shadow-lg ring-1 ring-line-2", error && "text-you-fg")}>
          {busy && <Loader2 className="size-3.5 animate-spin" />}
          {error || t("docs.page.uploading")}
        </p>
      )}
    </div>
  );
}

export function Toolbar({ pid, site, area, setBody }: { pid: number; site: DocSite; area: RefObject<HTMLTextAreaElement | null>; setBody: (v: string) => void }) {
  const { t } = useTranslation();
  const file = useRef<HTMLInputElement>(null);
  const [linking, setLinking] = useState(false);
  const [busy, setBusy] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const mode = useRef<"light" | "dark" | undefined>(undefined);
  const [menu, setMenu] = useState<{ top: number; left: number } | null>(null);
  useDismiss(menuRef, !!menu, () => setMenu(null));
  const run = (f: (el: HTMLTextAreaElement) => void) => () => area.current && f(area.current);
  const tool = (label: string, icon: ReactNode, onClick: () => void) => (
    <IconButton key={label} label={label} onClick={onClick}>
      {icon}
    </IconButton>
  );
  return (
    <div className="scroll-thin -my-1 flex min-w-0 items-center gap-0.5 overflow-x-auto py-1">
      {tool(t("docs.tool.heading"), <Heading2 className="size-4" />, run((el) => prefixLines(el, setBody, () => "## ")))}
      {tool(t("docs.tool.bold"), <Bold className="size-4" />, run((el) => wrap(el, setBody, "**", "**", t("docs.tool.boldText"))))}
      {tool(t("docs.tool.italic"), <Italic className="size-4" />, run((el) => wrap(el, setBody, "_", "_", t("docs.tool.italicText"))))}
      {tool(t("docs.tool.code"), <Code className="size-4" />, run((el) => (el.value.slice(el.selectionStart, el.selectionEnd).includes("\n") ? wrap(el, setBody, "```\n", "\n```") : wrap(el, setBody, "`", "`", "code"))))}
      {tool(t("docs.tool.link"), <Link2 className="size-4" />, run((el) => wrap(el, setBody, "[", "](https://)", t("docs.tool.linkText"))))}
      {tool(t("docs.tool.list"), <List className="size-4" />, run((el) => prefixLines(el, setBody, () => "- ")))}
      {tool(t("docs.tool.numbered"), <ListOrdered className="size-4" />, run((el) => prefixLines(el, setBody, (i) => `${i + 1}. `)))}
      {tool(t("docs.tool.callout"), <MessageSquareQuote className="size-4" />, run((el) => insertBlock(el, setBody, `> [!NOTE]\n> ${t("docs.tool.calloutText")}`)))}
      {tool(t("docs.tool.table"), <Table className="size-4" />, run((el) => insertBlock(el, setBody, "| Column | Column |\n| --- | --- |\n| Value | Value |")))}
      <span aria-hidden className="mx-1 h-5 w-px shrink-0 bg-line-2" />
      {tool(t("docs.tool.pageLink"), <FileSymlink className="size-4" />, () => setLinking(true))}
      <div ref={menuRef} className="contents">
      <IconButton
        label={t("docs.tool.attach")}
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          setMenu(menu ? null : { top: r.bottom + 4, left: Math.max(8, Math.min(r.left, document.documentElement.clientWidth - 264)) });
        }}
        aria-expanded={!!menu}
        disabled={busy}
      >
        {busy ? <Loader2 className="size-4 animate-spin" /> : <Paperclip className="size-4" />}
      </IconButton>
      {menu && (
        <div role="menu" style={menu} className="fixed z-40 w-64 rounded-sm bg-bg py-1 shadow-lg ring-1 ring-line-2">
          {(
            [
              [undefined, SunMoon, "docs.tool.attachBoth"],
              ["light", Sun, "docs.tool.attachLight"],
              ["dark", Moon, "docs.tool.attachDark"],
            ] as const
          ).map(([m, Icon, label]) => (
            <button
              key={label}
              type="button"
              role="menuitem"
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-sunk touch:py-2.5"
              onClick={() => {
                mode.current = m;
                setMenu(null);
                file.current?.click();
              }}
            >
              <Icon className="size-4 shrink-0 text-fg-3" />
              {t(label)}
            </button>
          ))}
        </div>
      )}
      </div>
      <input
        ref={file}
        type="file"
        multiple
        hidden
        onChange={async (e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = "";
          if (!files.length || !area.current) return;
          setBusy(true);
          try {
            const done = await upload(pid, site, files);
            insertBlock(area.current, setBody, done.map((f) => fileMarkdown(f, mode.current)).join("\n"));
          } finally {
            setBusy(false);
          }
        }}
      />
      {linking && (
        <PagePicker
          pid={pid}
          site={site}
          onClose={() => setLinking(false)}
          onPick={(p) => {
            setLinking(false);
            if (area.current) wrap(area.current, setBody, "[", `](doc-page:${p.id})`, p.title);
          }}
        />
      )}
    </div>
  );
}

function PagePicker({ pid, site, onClose, onPick }: { pid: number; site: DocSite; onClose: () => void; onPick: (p: { id: number; title: string }) => void }) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const found = useFindPages(pid, site.id, q.trim());
  return (
    <Modal open onClose={onClose} title={t("docs.tool.pageLink")} width="max-w-lg">
      <div className="space-y-3">
        <p className="text-fg-2">{t("docs.tool.pageLinkHint")}</p>
        <Input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("docs.move.search")} />
        <ul className="divide-y divide-line">
          {(found.data ?? []).map((p) => (
            <li key={p.id}>
              <button type="button" className="flex w-full flex-col px-2 py-2 text-left hover:bg-sunk" onClick={() => onPick(p)}>
                <span className="font-medium">{p.title}</span>
                <span className="text-fg-3">/{p.path}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </Modal>
  );
}

// --- around the text ------------------------------------------------------------------------------------------

/** Editing a released version: corrections only, what is new belongs in the working version. */
function ReleasedNote({ site, name, canWrite }: { site: DocSite; name: string; canWrite: boolean }) {
  const { t } = useTranslation();
  const working = site.releases.find((r) => r.working)?.name ?? "next";
  return (
    <div role="note" className="flex items-start gap-3 rounded-sm bg-you-bg px-3 py-2 text-you-fg">
      <Tag className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{t("docs.releases.editingReleased", { name })}</p>
        {canWrite && <p>{t("docs.releases.correctionsOnly", { working })}</p>}
      </div>
    </div>
  );
}

function FlagBanner({ pid, page, canWrite }: { pid: number; page: DocPage; canWrite: boolean }) {
  const { t } = useTranslation();
  const refresh = useDocsRefresh(pid);
  const release = useRelease();
  const [busy, setBusy] = useState(false);
  return (
    <div role="status" className="flex flex-wrap items-start gap-x-3 gap-y-2 border-l-2 border-fg-2 bg-sunk px-3 py-2">
      <CircleAlert className="mt-0.5 size-4 shrink-0 text-fg-2" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{t("docs.page.mayBeOutdated")}</p>
        <p className="text-fg-2">{page.flag_reason || t("docs.page.flaggedNoReason")}</p>
        <p className="text-fg-3">{t("docs.page.flaggedHint", { when: timeAgo(page.flagged_at) })}</p>
      </div>
      {canWrite && (
        <Button
          size="sm"
          loading={busy}
          icon={<Check className="size-3.5" />}
          onClick={async () => {
            setBusy(true);
            await api.post(rel(`${base(pid)}/pages/${page.id}/confirm`, release)).finally(() => setBusy(false));
            await refresh();
          }}
        >
          {t("docs.page.stillRight")}
        </Button>
      )}
    </div>
  );
}

export function languageLabel(code: string): string {
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
  } catch {
    return code;
  }
}

const STATE_ICON = {
  current: <Check className="size-3.5 text-ok" />,
  outdated: <Clock className="size-3.5 text-fg-2" />,
  missing: <span className="size-2.5 rounded-full border border-dashed border-fg-3" />,
  failed: <X className="size-3.5 text-you-fg" />,
};

function Translations({ items, onOpen }: { items: DocTranslationItem[]; onOpen: (lang: string) => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-fg-3">{t("docs.page.translations")}</span>
      {items.map((tr) => (
        <button
          key={tr.lang}
          type="button"
          data-part="tag"
          onClick={() => onOpen(tr.lang)}
          title={`${languageLabel(tr.lang)}: ${t(`docs.state.${tr.state}`)}${tr.error ? ` – ${tr.error}` : ""}`}
          className="inline-flex h-7 items-center gap-1.5 rounded-sm px-2 ring-1 ring-line-2 ring-inset hover:bg-sunk touch:h-9"
        >
          {STATE_ICON[tr.state]}
          <span className="font-medium uppercase">{tr.lang}</span>
          <span className="text-fg-3">{t(`docs.state.${tr.state}`)}</span>
        </button>
      ))}
    </div>
  );
}

function PageDetails({ pid, page }: { pid: number; page: DocPage }) {
  const { t } = useTranslation();
  const refresh = useDocsRefresh(pid);
  const release = useRelease();
  const [slug, setSlug] = useState(page.slug);
  const [paths, setPaths] = useState(page.paths.join("\n"));
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const outline = useDocOutline(pid, page.site_id);
  const [area, setArea] = useState(page.area_id ?? null);
  const [group, setGroup] = useState(page.group_id ?? null);
  const [humanOnly, setHumanOnly] = useState(!!page.human_only);
  const areas = outline.data?.areas ?? [];
  const groups = (outline.data?.groups ?? []).filter((g) => area === null || g.area_id === null || g.area_id === area);
  const placeable = page.parent_id === null && areas.length + (outline.data?.groups.length ?? 0) > 0;
  const placed = area !== (page.area_id ?? null) || group !== (page.group_id ?? null);
  const list = paths.split("\n").map((p) => p.trim()).filter(Boolean);
  const dirty = slug !== page.slug || list.join("\n") !== page.paths.join("\n") || placed || humanOnly !== !!page.human_only;
  return (
    <div className="space-y-3 border-y border-line py-3">
      <Field label={t("docs.page.slug")} hint={t("docs.page.slugHint")}>
        <Input value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))} />
      </Field>
      {placeable && (
        <FieldRow>
          <Field label={t("docs.outline.area")} hint={t("docs.page.areaHint")} group>
            <Select
              value={area ?? ""}
              aria-label={t("docs.outline.area")}
              onChange={(e) => {
                const next = e.target.value ? Number(e.target.value) : null;
                setArea(next);
                const g = outline.data?.groups.find((x) => x.id === group);
                if (g && g.area_id !== null && g.area_id !== next) setGroup(null);
              }}
            >
              <option value="">{t("docs.outline.noArea")}</option>
              {areas.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.title}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("docs.outline.group")} hint={t("docs.page.groupHint")} group>
            <Select
              value={group ?? ""}
              aria-label={t("docs.outline.group")}
              onChange={(e) => {
                const next = e.target.value ? Number(e.target.value) : null;
                setGroup(next);
                const g = outline.data?.groups.find((x) => x.id === next);
                if (g?.area_id != null) setArea(g.area_id);
              }}
            >
              <option value="">{t("docs.outline.noGroup")}</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.title}
                </option>
              ))}
            </Select>
          </Field>
        </FieldRow>
      )}
      <Field label={t("docs.page.paths")} hint={t("docs.page.pathsHint")}>
        <Textarea rows={3} value={paths} onChange={(e) => setPaths(e.target.value)} placeholder="app/plainboard/services/docs.py" className="font-mono" />
      </Field>
      <div className="space-y-1">
        <Toggle checked={humanOnly} onChange={setHumanOnly} label={t("docs.page.humanOnly")} />
        <p className="text-fg-3">{t("docs.page.humanOnlyHint")}</p>
      </div>
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          disabled={!dirty}
          loading={busy}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await api.patch(rel(`${base(pid)}/pages/${page.id}`, release), { ...(slug !== page.slug ? { slug } : {}), ...(placed ? { area_id: area, group_id: group } : {}), ...(humanOnly !== !!page.human_only ? { human_only: humanOnly } : {}), paths: list });
              await refresh();
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          {t("common.save")}
        </Button>
        <ErrorText error={error} />
      </div>
    </div>
  );
}

// --- history --------------------------------------------------------------------------------------------------

const CONTEXT = 3;

/** Unchanged runs longer than a few lines fold to a marker, as a diff reads best. */
function folded(rows: Row[]): (Row | { k: "fold"; count: number })[] {
  const out: (Row | { k: "fold"; count: number })[] = [];
  let i = 0;
  while (i < rows.length) {
    if (rows[i].k !== " ") {
      // A changed block reads as what went, then what came.
      let j = i;
      while (j < rows.length && rows[j].k !== " ") j++;
      const block = rows.slice(i, j);
      out.push(...block.filter((r) => r.k === "-"), ...block.filter((r) => r.k !== "-"));
      i = j;
      continue;
    }
    let j = i;
    while (j < rows.length && rows[j].k === " ") j++;
    const run = rows.slice(i, j);
    const head = i === 0 ? 0 : CONTEXT;
    const tail = j === rows.length ? 0 : CONTEXT;
    if (run.length > head + tail + 1) {
      out.push(...run.slice(0, head), { k: "fold", count: run.length - head - tail }, ...run.slice(run.length - tail));
    } else out.push(...run);
    i = j;
  }
  return out;
}

function HistoryDialog({ pid, page, canWrite, onClose }: { pid: number; page: DocPage; canWrite: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const versions = useDocVersions(pid, page.id, true);
  const list = versions.data ?? [];
  const [chosen, setChosen] = useState<number | null>(null);
  const index = Math.max(0, list.findIndex((v) => v.id === chosen));
  const version = list[index];
  const before = list[index + 1];
  const a = useDocVersion(pid, page.id, before?.id ?? null);
  const b = useDocVersion(pid, page.id, version?.id ?? null);
  const refresh = useDocsRefresh(pid);
  const release = useRelease();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const rows = useMemo(() => (b.data ? folded(diffLines(a.data?.body ?? "", b.data.body ?? "")) : []), [a.data, b.data]);
  const loading = b.isLoading || (!!before && a.isLoading);
  return (
    <Modal open onClose={onClose} title={t("docs.history.title", { title: page.title })} width="max-w-5xl">
      {versions.isLoading ? (
        <Spinner />
      ) : (
        <div className="flex flex-col gap-4 md:flex-row">
          <ol className="scroll-thin max-h-56 shrink-0 overflow-y-auto border-y border-line md:max-h-[65vh] md:w-64">
            {list.map((v, i) => (
              <li key={v.id}>
                <button type="button" onClick={() => setChosen(v.id)} className={clsx("w-full border-b border-line px-2.5 py-2 text-left last:border-0", v.id === version?.id ? "bg-sunk" : "hover:bg-sunk")}>
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate font-medium">{v.author || "–"}</span>
                    {i === 0 && <span className="shrink-0 text-ok">{t("docs.history.current")}</span>}
                  </span>
                  <span className="block text-fg-3">{formatDateTime(v.created_at)}</span>
                  {v.reason && <span className="line-clamp-2 block text-fg-2">{v.reason}</span>}
                </button>
              </li>
            ))}
          </ol>
          <div className="min-w-0 flex-1 space-y-3">
            {version && (
              <div className="flex flex-wrap items-center gap-2">
                <p className="min-w-0 flex-1 text-fg-2">
                  {before ? t("docs.history.changed", { who: version.author || "–", when: formatDateTime(version.created_at) }) : t("docs.history.first")}
                </p>
                {canWrite && index > 0 && (
                  <Button
                    icon={<RotateCcw className="size-4" />}
                    loading={busy}
                    onClick={async () => {
                      setBusy(true);
                      setError(null);
                      try {
                        await api.post(rel(`${base(pid)}/pages/${page.id}/versions/${version.id}/restore`, release));
                        await refresh();
                        onClose();
                      } catch (e) {
                        setError(e);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    {t("docs.history.restore")}
                  </Button>
                )}
              </div>
            )}
            <ErrorText error={error} />
            {b.data && a.data && a.data.title !== b.data.title && (
              <p className="font-mono">
                <span className="bg-del/15 px-1 text-del line-through">{a.data.title}</span> → <span className="bg-ok/15 px-1 text-ok">{b.data.title}</span>
              </p>
            )}
            {loading ? (
              <Spinner />
            ) : (
              <div className="scroll-thin max-h-[60vh] overflow-auto rounded-sm font-mono ring-1 ring-line ring-inset">
                {rows.length === 0 && <p className="p-3 text-fg-3">{t("docs.history.noChange")}</p>}
                {rows.map((r, i) =>
                  r.k === "fold" ? (
                    <div key={i} className="bg-sunk px-3 py-0.5 text-fg-3">
                      {t("docs.history.folded", { count: r.count })}
                    </div>
                  ) : (
                    <div key={i} className={clsx("flex whitespace-pre-wrap", r.k === "+" && "bg-ok/15 text-ok", r.k === "-" && "bg-del/15 text-del")}>
                      <span className="w-6 shrink-0 text-center select-none opacity-70">{r.k === " " ? "" : r.k}</span>
                      <span className="min-w-0 flex-1 break-words">{r.c || " "}</span>
                    </div>
                  ),
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}

// --- a translation --------------------------------------------------------------------------------------------

function TranslationDialog({ pid, site, page, lang, canWrite, onClose }: { pid: number; site: DocSite; page: DocPage; lang: string; canWrite: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const tr = useDocTranslation(pid, page.id, lang);
  const refresh = useDocsRefresh(pid);
  const release = useRelease();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [asked, setAsked] = useState(false);
  const start = () => {
    setTitle(tr.data?.title || page.title);
    setBody(tr.data?.body || page.body);
    setEditing(true);
  };
  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-3xl"
      title={t("docs.translation.title", { language: languageLabel(lang), title: page.title })}
      footer={
        canWrite &&
        (editing ? (
          <>
            <Button variant="ghost" onClick={() => setEditing(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="primary"
              loading={busy}
              onClick={async () => {
                setBusy(true);
                setError(null);
                try {
                  await api.put(rel(`${base(pid)}/pages/${page.id}/translations/${lang}`, release), { title, body });
                  await refresh();
                  setEditing(false);
                } catch (e) {
                  setError(e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {t("common.save")}
            </Button>
          </>
        ) : (
          <>
            {site.translator && tr.data && tr.data.state !== "current" && (
              <Button
                disabled={asked}
                onClick={async () => {
                  await api.post(`${base(pid)}/sites/${site.id}/translate`);
                  setAsked(true);
                }}
              >
                {t(asked ? "docs.translation.asked" : "docs.translation.now")}
              </Button>
            )}
            <Button variant="primary" onClick={start}>
              {t("docs.translation.edit")}
            </Button>
          </>
        ))
      }
    >
      {tr.isLoading || !tr.data ? (
        <Spinner />
      ) : editing ? (
        <div className="space-y-3">
          <p className="text-fg-2">{t("docs.translation.byHandHint")}</p>
          <Field label={t("docs.page.title")}>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={18} className="font-mono" aria-label={t("docs.page.body")} />
          <ErrorText error={error} />
        </div>
      ) : (
        <div className="space-y-4">
          <p className="flex flex-wrap items-center gap-2 text-fg-2">
            {STATE_ICON[tr.data.state]}
            <span className="font-medium text-fg">{t(`docs.state.${tr.data.state}`)}</span>
            {tr.data.translated_at && <span>· {t("docs.translation.by", { who: tr.data.translated_by || "–", when: timeAgo(tr.data.translated_at) })}</span>}
          </p>
          <p className="text-fg-3">{t(`docs.translation.hint.${tr.data.state}`)}</p>
          {tr.data.error && <ErrorText error={tr.data.error} />}
          {tr.data.body ? (
            <div className="rounded-sm p-4 ring-1 ring-line ring-inset">
              <h2 className="mb-3 font-extrabold">{tr.data.title}</h2>
              <DocMarkdown text={tr.data.body} />
            </div>
          ) : null}
        </div>
      )}
    </Modal>
  );
}
