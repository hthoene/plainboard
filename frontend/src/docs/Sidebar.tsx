import clsx from "clsx";
import { ChevronRight, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { pageUrl } from "./api";
import type { Nav, NavArea, NavGroup, NavItem, SiteView } from "./types";

export const levelKey = (lang: string, parent: number | null) => `${lang}:${parent ?? "top"}`;

export interface TreeProps {
  site: SiteView;
  lang: string;
  levels: Map<string, Nav>;
  expanded: Set<number>;
  loading: Set<string>;
  currentId: number | null;
  onToggle: (item: NavItem) => void;
  onMore: (parent: number | null) => void;
  onNavigate: (href: string) => void;
  onPrefetch: (href: string) => void;
  /** The area shown (#159): only its pages at the top, and those of no area. */
  area?: NavArea | null;
  /** The address of the page shown: a closed group opens for it. */
  currentPath?: string;
}

/** The site's pages as a tree: each level comes when it is opened, long levels a part at a time, so a site of
 * thousands of pages shows as fast as one of ten. The current page stays in view. */
export function Tree(props: TreeProps) {
  const { t } = useTranslation();
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const current = ref.current?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!current) return;
    const box = ref.current!.closest("[data-scroll]") as HTMLElement | null;
    if (!box) return;
    const a = current.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (a.top < b.top + 8 || a.bottom > b.bottom - 8) box.scrollTop += a.top - b.top - b.height / 3;
  }, [props.currentId]);
  return (
    <nav ref={ref} aria-label={t("docsSite.pages")} className="doc-tree">
      <Level {...props} parent={null} depth={0} />
    </nav>
  );
}

function Level(props: TreeProps & { parent: number | null; depth: number }) {
  const { t } = useTranslation();
  const { site, lang, levels, parent, depth, loading } = props;
  const key = levelKey(lang, parent);
  const level = levels.get(key);
  if (!level) {
    return loading.has(key) ? (
      <div className="doc-tree-wait" style={{ paddingLeft: indent(depth) + 8 }}>
        <Loader2 className="size-3.5 animate-spin" />
      </div>
    ) : null;
  }
  const more = level.total - level.items.length;
  if (depth === 0 && (site.areas?.length || site.groups?.length)) return <Grouped {...props} items={level.items} more={more} />;
  return (
    <ul role={depth ? "group" : "tree"} className={clsx(depth > 0 && "doc-tree-sub")}>
      {level.items.map((item) => (
        <Item key={item.id} {...props} item={item} depth={depth} href={pageUrl(site, lang, item.path)} />
      ))}
      {more > 0 && (
        <li>
          <button type="button" className="doc-tree-more" style={{ paddingLeft: indent(depth) + 30 }} onClick={() => props.onMore(parent)} disabled={loading.has(key)}>
            {loading.has(key) ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {t("docsSite.more", { count: more })}
          </button>
        </li>
      )}
    </ul>
  );
}

/** Deep trees indent less the deeper they go, so a page twelve levels down still has room for its title. */
const indent = (depth: number) => Math.round(Math.min(depth, 4) * 14 + Math.max(0, depth - 4) * 6);

function Item(props: TreeProps & { item: NavItem; depth: number; href: string }) {
  const { t } = useTranslation();
  const { item, depth, href, expanded, currentId } = props;
  const open = expanded.has(item.id);
  const current = currentId === item.id;
  return (
    <li role="treeitem" aria-expanded={item.children ? open : undefined} aria-selected={current}>
      <div className={clsx("doc-tree-row", current && "is-current")} style={{ paddingLeft: indent(depth) }}>
        {item.children > 0 ? (
          <button
            type="button"
            className="doc-tree-toggle"
            aria-label={t(open ? "docsSite.collapse" : "docsSite.expand", { title: item.title })}
            onClick={() => props.onToggle(item)}
          >
            <ChevronRight className={clsx("size-3.5 transition-transform", open && "rotate-90")} />
          </button>
        ) : (
          <span className="doc-tree-dot" aria-hidden />
        )}
        <a
          href={href}
          aria-current={current ? "page" : undefined}
          onPointerEnter={() => props.onPrefetch(href)}
          onFocus={() => props.onPrefetch(href)}
          onClick={(e) => {
            if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            e.preventDefault();
            props.onNavigate(href);
            // A page with pages below it opens them when it is chosen.
            if (item.children > 0 && !open) props.onToggle(item);
          }}
        >
          {item.title}
        </a>
      </div>
      {open && item.children > 0 && <Level {...props} parent={item.id} depth={depth + 1} />}
    </li>
  );
}

/** The top level in order (#159): the pages of the area shown and those of none, then each group under its heading
 * (the groups of that area and those of none). A group can be closed; the browser keeps that per site. */
function Grouped(props: TreeProps & { items: NavItem[]; more: number }) {
  const { t } = useTranslation();
  const { area, items, site, lang, more, loading } = props;
  const store = `pb-doc-groups:${site.slug}`;
  const [closed, setClosed] = useState<Set<number>>(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(store) ?? "[]") as number[]);
    } catch {
      return new Set();
    }
  });
  const areaIds = new Set((site.areas ?? []).map((a) => a.id));
  const here = (id: number | null | undefined) => !area || id == null || !areaIds.has(id) || id === area.id;
  const groups: NavGroup[] = (site.groups ?? []).filter((g) => here(g.area));
  const groupIds = new Set(groups.map((g) => g.id));
  const shown = items.filter((i) => here(i.area));
  const loose = shown.filter((i) => i.group == null || !groupIds.has(i.group));
  const row = (item: NavItem) => <Item key={item.id} {...props} item={item} depth={0} href={pageUrl(site, lang, item.path)} />;
  const toggle = (id: number) =>
    setClosed((old) => {
      const next = new Set(old);
      if (!next.delete(id)) next.add(id);
      try {
        localStorage.setItem(store, JSON.stringify([...next]));
      } catch {
        // private mode: kept for this visit only
      }
      return next;
    });
  const head = (props.currentPath ?? "").split("/")[0];
  return (
    <div role="tree">
      {loose.length > 0 && <ul role="group">{loose.map(row)}</ul>}
      {groups.map((g) => {
        const pages = shown.filter((i) => i.group === g.id);
        if (!pages.length) return null;
        // A closed group still opens for the page shown in it.
        const shut = closed.has(g.id) && !pages.some((i) => i.path === head);
        const id = `doc-group-${g.id}`;
        return (
          <div key={g.id} className="doc-tree-group">
            <button type="button" className="doc-tree-head" aria-expanded={!shut} aria-controls={id} onClick={() => toggle(g.id)}>
              <span>{g.title}</span>
              <ChevronRight className={clsx("size-3.5 transition-transform", !shut && "rotate-90")} aria-hidden />
            </button>
            {!shut && (
              <ul role="group" id={id} aria-label={g.title}>
                {pages.map(row)}
              </ul>
            )}
          </div>
        );
      })}
      {more > 0 && (
        <button type="button" className="doc-tree-more" style={{ paddingLeft: 30 }} onClick={() => props.onMore(null)} disabled={loading.has(levelKey(lang, null))}>
          {loading.has(levelKey(lang, null)) ? <Loader2 className="size-3.5 animate-spin" /> : null}
          {t("docsSite.more", { count: more })}
        </button>
      )}
    </div>
  );
}
