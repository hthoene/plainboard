import clsx from "clsx";
import {
  ArrowDown,
  ArrowUp,
  ChevronRight,
  CircleAlert,
  CornerDownRight,
  CornerLeftUp,
  FilePlus2,
  FolderInput,
  Loader2,
  MoreHorizontal,
  Search,
  Trash2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import type { DocPageItem, DocPages, DocSite } from "../../api/types";
import { Button, ErrorText, IconButton, Input, Modal, Spinner } from "../../components/ui";
import { useDismiss, useInView } from "../../lib/hooks";
import { base, rel, useDocChildren, useDocsRefresh, useFindPages, useRelease } from "./queries";

export interface TreeActions {
  /** A new page below this one (null: at the top). */
  onNew: (parent: DocPageItem | null) => void;
}

/** The site's pages as a tree that is read level by level (a site of thousands of pages opens as fast as a small
 * one), with a filter over all of them. Members arrange it: up, down, in, out, anywhere, and delete. */
export function PageTree({
  pid,
  site,
  selected,
  opened,
  canWrite,
  onSelect,
  onNew,
}: {
  pid: number;
  site: DocSite;
  selected: number | null;
  /** Pages to open (the way down to the chosen page). */
  opened: number[];
  canWrite: boolean;
  onSelect: (id: number) => void;
  onNew: (parent: DocPageItem | null) => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set(opened));
  const [moving, setMoving] = useState<DocPageItem | null>(null);
  const found = useFindPages(pid, site.id, query.trim());
  const release = useRelease();
  const key = opened.join(",");
  useEffect(() => {
    if (opened.length) setExpanded((old) => new Set([...old, ...opened]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const toggle = (id: number) =>
    setExpanded((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const ctx: Ctx = { pid, site, release, selected, expanded, canWrite, onSelect, onNew, toggle, onMove: setMoving };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b border-line p-2">
        <div className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-3" />
          <Input className="pl-8" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("docs.tree.filter")} aria-label={t("docs.tree.filter")} />
        </div>
        {canWrite && (
          <IconButton label={t("docs.tree.newTop")} onClick={() => onNew(null)} className="ring-1 ring-line-2 ring-inset">
            <FilePlus2 className="size-4" />
          </IconButton>
        )}
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto py-1.5" role="tree" aria-label={t("docs.tree.label")}>
        {query.trim() ? (
          <ul>
            {(found.data ?? []).map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  onClick={() => onSelect(p.id)}
                  className={clsx("flex w-full flex-col px-3 py-1.5 text-left touch:py-2.5", selected === p.id ? "bg-sunk" : "hover:bg-sunk")}
                >
                  <span className="truncate font-medium">{p.title}</span>
                  <span className="truncate text-fg-3">/{p.path}</span>
                </button>
              </li>
            ))}
            {found.data && found.data.length === 0 && <li className="px-3 py-6 text-center text-fg-3">{t("docs.tree.noMatch")}</li>}
            {found.isFetching && !found.data && (
              <li className="flex justify-center py-4">
                <Spinner />
              </li>
            )}
          </ul>
        ) : (
          <Level ctx={ctx} parent={null} depth={0} />
        )}
      </div>
      {moving && <MoveDialog pid={pid} site={site} page={moving} onClose={() => setMoving(null)} />}
    </div>
  );
}

interface Ctx {
  pid: number;
  site: DocSite;
  release: number | null;
  selected: number | null;
  expanded: Set<number>;
  canWrite: boolean;
  onSelect: (id: number) => void;
  onNew: (parent: DocPageItem | null) => void;
  toggle: (id: number) => void;
  onMove: (page: DocPageItem) => void;
}

/** One level, the first 200; "more" reads the next ones. */
function Level({ ctx, parent, depth, parentItem }: { ctx: Ctx; parent: number | null; depth: number; parentItem?: DocPageItem }) {
  const { t } = useTranslation();
  const first = useDocChildren(ctx.pid, ctx.site.id, parent);
  const [more, setMore] = useState<DocPageItem[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  useEffect(() => setMore([]), [first.data]);
  if (first.isLoading) {
    return (
      <div className="flex py-1.5" style={{ paddingLeft: pad(depth) + 24 }}>
        <Loader2 className="size-3.5 animate-spin text-fg-3" />
      </div>
    );
  }
  if (!first.data) return null;
  const items = [...first.data.items, ...more];
  if (depth === 0 && items.length === 0) {
    return (
      <div className="px-4 py-8 text-center text-fg-3">
        <p>{t("docs.tree.empty")}</p>
        {ctx.canWrite && (
          <Button className="mt-3" icon={<FilePlus2 className="size-4" />} onClick={() => ctx.onNew(null)}>
            {t("docs.tree.firstPage")}
          </Button>
        )}
      </div>
    );
  }
  const rest = first.data.total - items.length;
  return (
    <ul role={depth ? "group" : undefined}>
      {items.map((item, i) => (
        <Row key={item.id} ctx={ctx} item={item} depth={depth} before={items[i - 1]} last={i === items.length - 1 && rest <= 0} parentItem={parentItem} />
      ))}
      {rest > 0 && (
        <li>
          <button
            type="button"
            disabled={loadingMore}
            className="flex items-center gap-1.5 py-1 text-fg-2 hover:text-fg"
            style={{ paddingLeft: pad(depth) + 28 }}
            onClick={async () => {
              setLoadingMore(true);
              try {
                const next = await api.get<DocPages>(rel(`${base(ctx.pid)}/sites/${ctx.site.id}/pages?limit=200&offset=${items.length}${parent === null ? "" : `&parent=${parent}`}`, ctx.release));
                setMore((m) => [...m, ...next.items]);
              } finally {
                setLoadingMore(false);
              }
            }}
          >
            {loadingMore && <Loader2 className="size-3.5 animate-spin" />}
            {t("docs.tree.more", { count: rest })}
          </button>
        </li>
      )}
    </ul>
  );
}

const pad = (depth: number) => Math.min(depth, 5) * 14 + Math.max(0, depth - 5) * 6 + 6;

function Row({ ctx, item, depth, before, last, parentItem }: { ctx: Ctx; item: DocPageItem; depth: number; before?: DocPageItem; last: boolean; parentItem?: DocPageItem }) {
  const { t } = useTranslation();
  const open = ctx.expanded.has(item.id);
  const current = ctx.selected === item.id;
  return (
    <li role="treeitem" aria-expanded={item.children ? open : undefined} aria-selected={current}>
      <div
        className={clsx("group/row flex min-h-8 items-center gap-0.5 pr-1 touch:min-h-11", current ? "bg-sunk font-semibold" : "hover:bg-sunk")}
        style={{ paddingLeft: pad(depth) }}
      >
        {item.children > 0 ? (
          <button
            type="button"
            onClick={() => ctx.toggle(item.id)}
            aria-label={t(open ? "docs.tree.collapse" : "docs.tree.expand")}
            className="inline-flex size-6 shrink-0 items-center justify-center rounded-sm text-fg-3 hover:bg-line hover:text-fg touch:size-9"
          >
            <ChevronRight className={clsx("size-3.5 transition-transform", open && "rotate-90")} />
          </button>
        ) : (
          <span className="size-6 shrink-0 touch:size-9" />
        )}
        <button
          type="button"
          onClick={() => {
            ctx.onSelect(item.id);
            if (item.children > 0 && !open) ctx.toggle(item.id);
          }}
          className="min-w-0 flex-1 truncate py-1 text-left"
          title={`/${item.path}`}
        >
          {item.title}
        </button>
        {item.flagged_at && (
          <span title={t("docs.page.flagged", { reason: item.flag_reason || "–" })} className="shrink-0 text-fg-2">
            <CircleAlert className="size-3.5" aria-label={t("docs.page.mayBeOutdated")} />
          </span>
        )}
        {ctx.canWrite && <RowMenu ctx={ctx} item={item} before={before} last={last} parentItem={parentItem} />}
      </div>
      {open && item.children > 0 && <Level ctx={ctx} parent={item.id} depth={depth + 1} parentItem={item} />}
    </li>
  );
}

function RowMenu({ ctx, item, before, last, parentItem }: { ctx: Ctx; item: DocPageItem; before?: DocPageItem; last: boolean; parentItem?: DocPageItem }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const refresh = useDocsRefresh(ctx.pid);
  const [menuRef, inView] = useInView<HTMLDivElement>(open);
  useDismiss(ref, open, () => {
    setOpen(false);
    setConfirm(false);
  });
  const move = async (body: Record<string, unknown>) => {
    setOpen(false);
    try {
      await api.patch(rel(`${base(ctx.pid)}/pages/${item.id}`, ctx.release), body);
      setError("");
    } catch (e) {
      setError(errorMessage(e));
    }
    await refresh();
  };
  const entry = "flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-sunk disabled:opacity-40 disabled:hover:bg-transparent touch:py-2.5";
  return (
    <div ref={ref} className="relative shrink-0">
      <IconButton
        label={t("docs.tree.actions", { title: item.title })}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className={clsx("size-6 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover/row:opacity-100", open && "opacity-100!")}
      >
        <MoreHorizontal className="size-4" />
      </IconButton>
      {error && (
        <span role="alert" className="absolute top-full right-0 z-30 w-56 bg-bg p-2 shadow-lg ring-1 ring-line-2" onClick={() => setError("")}>
          <ErrorText error={error} />
        </span>
      )}
      {open && (
        <div ref={menuRef} style={inView} role="menu" className="absolute top-full right-0 z-30 w-56 max-w-[calc(100vw-1rem)] rounded-sm bg-bg py-1 font-normal shadow-lg ring-1 ring-line-2">
          <button type="button" role="menuitem" className={entry} onClick={() => (setOpen(false), ctx.onNew(item))}>
            <FilePlus2 className="size-4 text-fg-3" /> {t("docs.tree.newBelow")}
          </button>
          <button type="button" role="menuitem" className={entry} disabled={item.position === 0} onClick={() => move({ position: item.position - 1 })}>
            <ArrowUp className="size-4 text-fg-3" /> {t("docs.tree.up")}
          </button>
          <button type="button" role="menuitem" className={entry} disabled={last} onClick={() => move({ position: item.position + 1 })}>
            <ArrowDown className="size-4 text-fg-3" /> {t("docs.tree.down")}
          </button>
          <button type="button" role="menuitem" className={entry} disabled={!before} onClick={() => before && move({ move: true, parent_id: before.id })}>
            <CornerDownRight className="size-4 text-fg-3" /> {t("docs.tree.indent", { title: before?.title ?? "" })}
          </button>
          <button
            type="button"
            role="menuitem"
            className={entry}
            disabled={item.parent_id === null}
            onClick={() => move({ move: true, parent_id: parentItem?.parent_id ?? null, position: (parentItem?.position ?? 0) + 1 })}
          >
            <CornerLeftUp className="size-4 text-fg-3" /> {t("docs.tree.outdent")}
          </button>
          <button type="button" role="menuitem" className={entry} onClick={() => (setOpen(false), ctx.onMove(item))}>
            <FolderInput className="size-4 text-fg-3" /> {t("docs.tree.moveTo")}
          </button>
          <div className="my-1 border-t border-line" />
          {confirm ? (
            <button
              type="button"
              role="menuitem"
              className={clsx(entry, "font-semibold text-you-fg")}
              onClick={async () => {
                setOpen(false);
                setConfirm(false);
                await api.del(rel(`${base(ctx.pid)}/pages/${item.id}`, ctx.release)).catch((e: unknown) => setError(errorMessage(e)));
                await refresh();
              }}
            >
              <Trash2 className="size-4" /> {item.children ? t("docs.tree.deleteAll", { count: item.children }) : t("docs.tree.deleteSure")}
            </button>
          ) : (
            <button type="button" role="menuitem" className={entry} onClick={() => setConfirm(true)}>
              <Trash2 className="size-4 text-fg-3" /> {t("docs.tree.delete")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** Another place in the tree: below a page found by its title, or at the top. */
function MoveDialog({ pid, site, page, onClose }: { pid: number; site: DocSite; page: DocPageItem; onClose: () => void }) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [error, setError] = useState<unknown>(null);
  const refresh = useDocsRefresh(pid);
  const found = useFindPages(pid, site.id, query.trim());
  const top = useDocChildren(pid, site.id, null, !query.trim());
  const release = useRelease();
  const candidates = (query.trim() ? found.data : top.data?.items) ?? [];
  const to = async (parent: number | null) => {
    try {
      await api.patch(rel(`${base(pid)}/pages/${page.id}`, release), { move: true, parent_id: parent });
      await refresh();
      onClose();
    } catch (e) {
      setError(e);
    }
  };
  const below = (p: DocPageItem) => p.id === page.id || p.path.startsWith(page.path + "/");
  return (
    <Modal open onClose={onClose} title={t("docs.move.title", { title: page.title })} width="max-w-lg">
      <div className="space-y-3">
        <p className="text-fg-2">{t("docs.move.hint")}</p>
        <Input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("docs.move.search")} />
        <ul className="divide-y divide-line border-y border-line">
          {page.parent_id !== null && (
            <li>
              <button type="button" className="w-full px-2 py-2 text-left font-medium hover:bg-sunk" onClick={() => to(null)}>
                {t("docs.move.top")}
              </button>
            </li>
          )}
          {candidates
            .filter((p) => !below(p) && p.id !== page.parent_id)
            .map((p) => (
              <li key={p.id}>
                <button type="button" className="flex w-full flex-col px-2 py-2 text-left hover:bg-sunk" onClick={() => to(p.id)}>
                  <span className="font-medium">{p.title}</span>
                  <span className="text-fg-3">/{p.path}</span>
                </button>
              </li>
            ))}
        </ul>
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}
