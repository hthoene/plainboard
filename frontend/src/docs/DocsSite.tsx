import clsx from "clsx";
import { ArrowLeft, ArrowRight, ArrowUpRight, ChevronDown, ChevronRight, Globe, Info, ListTree, LogOut, Menu, Moon, Search, Sun, Tag, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useDismiss } from "../lib/hooks";
import { setAppearance, useLook } from "../lib/theme";
import { Assistant } from "./Assistant";
import { customSlug, inVersion, knownCustom, knownPage, langBase, loadCustom, loadNav, loadPage, NotFoundError, pageUrl, parseUrl, prefetch, versionBase } from "./api";
import { Content } from "./Content";
import { SearchDialog } from "./Search";
import { AreaCards, areaOf, AreaSwitcher, areaStart, shownAreas } from "./NavParts";
import { levelKey, Tree } from "./Sidebar";
import type { CustomView, Nav, NavArea, NavItem, PageView, SiteView, VersionItem } from "./types";

/** The name of a language in that language ("Deutsch", "Français"), as a language switcher shows it. */
export function languageName(code: string): string {
  try {
    const name = new Intl.DisplayNames([code], { type: "language" }).of(code) ?? code;
    return name.charAt(0).toLocaleUpperCase(code) + name.slice(1);
  } catch {
    return code;
  }
}

function dateIn(lang: string, iso: string): string {
  try {
    return new Intl.DateTimeFormat(lang, { dateStyle: "long" }).format(new Date(iso));
  } catch {
    return iso.slice(0, 10);
  }
}

/** The levels of the tree a page view brings: the top, the children of each page on the way down, its own. */
function levelsOf(page: PageView, lang: string): [string, Nav][] {
  const out: [string, Nav][] = [];
  page.sidebar.forEach((level, k) => {
    if (k === 0) out.push([levelKey(lang, null), level]);
    else {
      const crumb = page.breadcrumbs[k - 1];
      const parent = page.sidebar[k - 1].items.find((i) => i.path === crumb?.path);
      if (parent) out.push([levelKey(lang, parent.id), level]);
    }
  });
  out.push([levelKey(lang, page.id), page.children]);
  return out;
}

/** The ids of the pages on the way down to a page (to open them in the tree). */
function openedFor(page: PageView): number[] {
  const ids: number[] = [];
  page.sidebar.forEach((level, k) => {
    const crumb = page.breadcrumbs[k];
    const found = crumb && level.items.find((i) => i.path === crumb.path);
    if (found) ids.push(found.id);
  });
  if (page.children.total > 0) ids.push(page.id);
  return ids;
}

interface Where {
  lang: string;
  path: string;
}

export function DocsSite({ site, initial, initialCustom = null }: { site: SiteView; initial: PageView | null; initialCustom?: CustomView | null }) {
  const { t } = useTranslation();
  const start = useMemo(() => parseUrl(site, new URL(location.href)) ?? { lang: site.lang, path: "", hash: "" }, [site]);
  const [where, setWhere] = useState<Where>({ lang: site.lang, path: start.path });
  const [page, setPage] = useState<PageView | null>(initial);
  // A page of the site's own (imprint, privacy …: #121) instead of a docs page.
  const [custom, setCustom] = useState<CustomView | null>(initialCustom);
  const [missing, setMissing] = useState(!initial && !initialCustom && site.nav.items.length > 0);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [levels, setLevels] = useState<Map<string, Nav>>(() => {
    const map = new Map<string, Nav>([[levelKey(site.lang, null), site.nav]]);
    if (initial) for (const [k, v] of levelsOf(initial, site.lang)) map.set(k, v);
    return map;
  });
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set(initial ? openedFor(initial) : []));
  const [levelsLoading, setLevelsLoading] = useState<Set<string>>(new Set());
  const [drawer, setDrawer] = useState(false);
  const [searching, setSearching] = useState(false);
  const counter = useRef(0);
  const look = useLook();

  useEffect(() => {
    if (initial) knownPage(initial, start.path);
    if (initialCustom) knownCustom(initialCustom);
    history.scrollRestoration = "manual";
    if (start.hash) requestAnimationFrame(() => document.getElementById(start.hash)?.scrollIntoView());
  }, [initial, initialCustom, start]);

  const showPage = useCallback((found: PageView, lang: string) => {
    setPage(found);
    setCustom(null);
    setMissing(false);
    setLevels((old) => {
      const next = new Map(old);
      for (const [k, v] of levelsOf(found, lang)) {
        const had = next.get(k);
        if (!had || had.items.length <= v.items.length) next.set(k, v);
      }
      return next;
    });
    setExpanded((old) => new Set([...old, ...openedFor(found)]));
  }, []);

  /** Opens a page of the site without loading the document again; anything else is left to the browser. */
  const go = useCallback(
    (href: string, how: { pop?: boolean; scroll?: number } = {}) => {
      const url = new URL(href, location.href);
      const target = parseUrl(site, url);
      if (!target) {
        location.href = url.href;
        return;
      }
      setDrawer(false);
      const same = target.lang === where.lang && target.path === where.path && !missing;
      if (same && !how.pop) {
        if (target.hash) {
          history.pushState({ scroll: 0 }, "", url.pathname + url.hash);
          document.getElementById(target.hash)?.scrollIntoView({ behavior: "smooth" });
        } else if (url.href !== location.href) {
          history.pushState({ scroll: 0 }, "", url.pathname);
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
        return;
      }
      const n = ++counter.current;
      const slow = window.setTimeout(() => n === counter.current && setLoading(true), 120);
      if (!how.pop) history.replaceState({ ...history.state, scroll: window.scrollY }, "");
      const slug = customSlug(site, target.path);
      (slug ? loadCustom(site, target.lang, slug) : loadPage(site, target.lang, target.path))
        .then((found) => {
          if (n !== counter.current) return;
          if ("breadcrumbs" in found) showPage(found, target.lang);
          else {
            setCustom(found);
            setPage(null);
            setMissing(false);
          }
          setFailed(false);
        })
        .catch((e: unknown) => {
          if (n !== counter.current) return;
          if (e instanceof NotFoundError) {
            setPage(null);
            setCustom(null);
            setMissing(true);
          } else setFailed(true);
        })
        .finally(() => {
          window.clearTimeout(slow);
          if (n !== counter.current) return;
          setLoading(false);
          setWhere({ lang: target.lang, path: target.path });
          if (!how.pop) history.pushState({ scroll: 0 }, "", url.pathname + url.hash);
          requestAnimationFrame(() => {
            const anchor = target.hash && document.getElementById(target.hash);
            if (anchor) anchor.scrollIntoView();
            else window.scrollTo(0, how.scroll ?? 0);
          });
        });
    },
    [site, where, missing, showPage],
  );

  useEffect(() => {
    const onPop = (e: PopStateEvent) => go(location.href, { pop: true, scroll: (e.state as { scroll?: number } | null)?.scroll ?? 0 });
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [go]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && (e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName));
      if (((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") || (e.key === "/" && !typing)) {
        e.preventDefault();
        setSearching((s) => !s);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Title, description and language of the document follow the page, as the server wrote them for the first.
  useEffect(() => {
    const shown = custom ?? page;
    document.title = shown ? `${shown.title} – ${site.name}` : missing ? `${t("docsSite.notFound")} – ${site.name}` : site.name;
    document.documentElement.lang = where.lang;
    document.querySelector('meta[name="description"]')?.setAttribute("content", shown?.description || site.description);
  }, [page, custom, missing, site, where.lang, t]);

  useEffect(() => {
    document.body.style.overflow = drawer ? "hidden" : "";
  }, [drawer]);

  const lang = where.lang;
  const onPrefetch = useCallback((href: string) => prefetch(site, href), [site]);
  const onNavigate = useCallback((href: string) => go(href), [go]);

  const fetchLevel = (parent: number | null, offset: number) => {
    const key = levelKey(lang, parent);
    setLevelsLoading((s) => new Set(s).add(key));
    loadNav(site, lang, parent, offset)
      .then((nav) =>
        setLevels((old) => {
          const next = new Map(old);
          const had = next.get(key);
          const seen = new Set(offset && had ? had.items.map((i) => i.id) : []);
          next.set(key, offset && had ? { ...nav, items: [...had.items, ...nav.items.filter((i) => !seen.has(i.id))] } : nav);
          return next;
        }),
      )
      .catch(() => undefined)
      .finally(() =>
        setLevelsLoading((s) => {
          const next = new Set(s);
          next.delete(key);
          return next;
        }),
      );
  };
  const onToggle = (item: NavItem) => {
    const open = expanded.has(item.id);
    setExpanded((old) => {
      const next = new Set(old);
      if (open) next.delete(item.id);
      else next.add(item.id);
      return next;
    });
    if (!open && !levels.has(levelKey(lang, item.id))) fetchLevel(item.id, 0);
  };
  const onMore = (parent: number | null) => fetchLevel(parent, levels.get(levelKey(lang, parent))?.items.length ?? 0);
  // Another language: its top level, if no page brought it yet.
  useEffect(() => {
    if (!levels.has(levelKey(lang, null)) && !levelsLoading.has(levelKey(lang, null))) fetchLevel(null, 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lang]);

  const home = `${langBase(site, lang)}/`;
  const theme = site.theme ?? null;
  const logo = theme ? (look.dark ? theme.logo_dark : theme.logo) : null;
  const links = theme?.links ?? [];
  const footer = site.footer ?? [];
  // Areas (#159): the one of the page shown, the others in the switcher and as cards on the home page.
  const top = levels.get(levelKey(lang, null))?.items ?? site.nav.items;
  const areas = shownAreas(site, top);
  const area = areaOf(areas, top, page?.path ?? where.path);
  const areaProps = { areas, hrefOf: (a: NavArea) => pageUrl(site, lang, areaStart(top, a)), onGo: go };
  const tree = (
    <>
      {areas.length > 1 && area && <AreaSwitcher {...areaProps} current={area} />}
      <Tree
      area={area}
      currentPath={page?.path ?? where.path}
      site={site}
      lang={lang}
      levels={levels}
      expanded={expanded}
      loading={levelsLoading}
      currentId={page?.id ?? null}
      onToggle={onToggle}
      onMore={onMore}
      onNavigate={onNavigate}
      onPrefetch={onPrefetch}
    />
    </>
  );
  // Another version (#126): a document of its own, at the same page when it is there.
  const [switching, setSwitching] = useState(false);
  const toVersion = (v: VersionItem) => {
    setSwitching(true);
    setLoading(true);
    void inVersion(site, v, lang, where.path).then((href) => location.assign(href));
  };
  useEffect(() => {
    // Back from the other version (the page kept by the browser): ready again.
    const onShow = (e: PageTransitionEvent) => e.persisted && (setSwitching(false), setLoading(false));
    window.addEventListener("pageshow", onShow);
    return () => window.removeEventListener("pageshow", onShow);
  }, []);
  const versions = site.versioned && site.version && (site.versions?.length ?? 0) > 1 && (
    <VersionMenu versions={site.versions!} current={site.version.slug} busy={switching} onChoose={toVersion} />
  );
  const languages = site.languages.length > 1 && (
    <LanguageMenu
      languages={site.languages}
      current={lang}
      onChoose={(code) => {
        // An address for every language: the same page there (its own language at the site's base).
        go(pageUrl(site, code, where.path));
      }}
    />
  );

  return (
    <div className={clsx("doc-shell", site.assistant && "has-ai")}>
      <div className={clsx("doc-progress", loading && "is-on")} aria-hidden />
      <header className="doc-header">
        <div className="doc-header-in">
          <button type="button" className="doc-icon-btn lg:hidden" onClick={() => setDrawer(true)} aria-label={t("docsSite.menu")} aria-expanded={drawer}>
            <Menu className="size-5" />
          </button>
          <a
            href={home}
            className="doc-brand"
            onClick={(e) => {
              if (e.button !== 0 || e.metaKey || e.ctrlKey) return;
              e.preventDefault();
              go(home);
            }}
          >
            {logo ? (
              <img src={logo} alt={theme?.show_name ? "" : site.name} className="doc-logo" />
            ) : (
              <span className="doc-mark" aria-hidden>
                {site.name.charAt(0).toUpperCase()}
              </span>
            )}
            {(!logo || theme?.show_name) && <span className="doc-brand-name">{site.name}</span>}
          </a>
          <div className="flex-1" />
          {links.length > 0 && (
            <nav className="doc-head-links hidden lg:flex" aria-label={t("docsSite.links")}>
              {links.map((l) => (
                <a key={l.url + l.label} href={l.url}>
                  {l.label}
                </a>
              ))}
            </nav>
          )}
          <button type="button" className="doc-search-btn" onClick={() => setSearching(true)} aria-haspopup="dialog" aria-label={t("docsSite.search")}>
            <Search className="size-4 shrink-0" />
            <span className="doc-search-btn-label">{t("docsSite.searchShort")}</span>
            <kbd className="doc-search-btn-kbd">{/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘K" : "Ctrl K"}</kbd>
          </button>
          <div className="hidden lg:contents">
            {versions}
            {languages}
          </div>
          <button
            type="button"
            className="doc-icon-btn"
            onClick={() => setAppearance(look.dark ? "light" : "dark")}
            aria-label={t(look.dark ? "docsSite.light" : "docsSite.dark")}
            title={t(look.dark ? "docsSite.light" : "docsSite.dark")}
          >
            {look.dark ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />}
          </button>
        </div>
      </header>

      <div className="doc-layout">
        <aside className="doc-side" data-scroll>
          {tree}
        </aside>
        {drawer && (
          <div className="doc-drawer-scrim lg:hidden" onMouseDown={() => setDrawer(false)}>
            <aside className="doc-drawer" onMouseDown={(e) => e.stopPropagation()} aria-label={t("docsSite.pages")}>
              <div className="doc-drawer-head">
                <span className="doc-brand-name">{site.name}</span>
                <button type="button" className="doc-icon-btn" onClick={() => setDrawer(false)} aria-label={t("docsSite.close")}>
                  <X className="size-5" />
                </button>
              </div>
              {(versions || languages) && (
                <div className="doc-drawer-langs">
                  {versions}
                  {languages}
                </div>
              )}
              {links.length > 0 && (
                <nav className="doc-drawer-links" aria-label={t("docsSite.links")}>
                  {links.map((l) => (
                    <a key={l.url + l.label} href={l.url}>
                      {l.label}
                      <ArrowUpRight className="size-3.5" aria-hidden />
                    </a>
                  ))}
                </nav>
              )}
              <div className="doc-drawer-tree" data-scroll>
                {tree}
              </div>
            </aside>
          </div>
        )}

        <main className="doc-main">
          <VersionBanner site={site} lang={lang} path={where.path} onGo={toVersion} />
          {failed ? (
            <Message title={t("docsSite.failed")} text={t("docsSite.failedHint")} action={<button type="button" className="doc-btn" onClick={() => go(location.href, { pop: true })}>{t("docsSite.retry")}</button>} />
          ) : missing ? (
            <Message
              title={t("docsSite.notFound")}
              text={t("docsSite.notFoundHint")}
              action={
                <>
                  <a href={home} className="doc-btn" onClick={(e) => (e.preventDefault(), go(home))}>
                    {t("docsSite.home")}
                  </a>
                  <button type="button" className="doc-btn is-ghost" onClick={() => setSearching(true)}>
                    {t("docsSite.search")}
                  </button>
                </>
              }
            />
          ) : custom ? (
            <CustomArticle site={site} page={custom} lang={lang} home={home} onNavigate={onNavigate} onPrefetch={onPrefetch} />
          ) : !page ? (
            <Message title={site.name} text={site.description || t("docsSite.empty")} />
          ) : (
            <>
            {areas.length > 1 && where.path === "" && <AreaCards {...areaProps} site={site} top={top} />}
            <Article site={site} page={page} lang={lang} home={home} onNavigate={onNavigate} onPrefetch={onPrefetch} />
            </>
          )}
          <footer className="doc-foot">
            {footer.length > 0 && (
              <nav className="doc-foot-links" aria-label={t("docsSite.legal")}>
                {footer.map((f) => {
                  const href = pageUrl(site, lang, f.path);
                  return (
                    <a
                      key={f.slug}
                      href={href}
                      aria-current={custom?.slug === f.slug ? "page" : undefined}
                      onPointerEnter={() => onPrefetch(href)}
                      onClick={(e) => {
                        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
                        e.preventDefault();
                        go(href);
                      }}
                    >
                      {f.titles[lang] ?? f.title}
                    </a>
                  );
                })}
              </nav>
            )}
            <span>
              {site.project ? `${site.project} · ` : ""}
              {t("docsSite.madeWith")}{" "}
              <a href="https://github.com/hthoene/plainboard" target="_blank" rel="noreferrer">
                PlainBoard
              </a>
            </span>
            {!site.public && (
              <button
                type="button"
                className="doc-link-btn"
                onClick={() => void fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }).finally(() => location.reload())}
              >
                <LogOut className="size-3.5" /> {t("docsSite.signOut")}
              </button>
            )}
          </footer>
        </main>

        <aside className="doc-toc">{(custom ?? page) && <Toc page={(custom ?? page)!} />}</aside>
      </div>
      {/* The place for the site's assistant (#119): bottom right, kept free by everything else. */}
      <div id="pb-docs-assistant" data-part="docs-assistant" className="doc-assistant-slot">
        {site.assistant && <Assistant site={site} lang={lang} page={page} onNavigate={onNavigate} />}
      </div>
      <SearchDialog site={site} lang={lang} open={searching} onClose={() => setSearching(false)} onOpen={go} />
    </div>
  );
}

function Article({ site, page, lang, home, onNavigate, onPrefetch }: { site: SiteView; page: PageView; lang: string; home: string; onNavigate: (href: string) => void; onPrefetch: (href: string) => void }) {
  const { t } = useTranslation();
  const link = (path: string) => pageUrl(site, lang, path);
  const go = (href: string) => (e: MouseEvent) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    onNavigate(href);
  };
  const sub = page.children.items;
  return (
    <article key={`${page.id}-${page.lang}`} className="doc-article">
      <nav aria-label={t("docsSite.breadcrumbs")} className="doc-crumbs">
        <a href={home} onClick={go(home)}>
          {site.name}
        </a>
        {page.breadcrumbs.map((c) => (
          <span key={c.path} className="contents">
            <ChevronRight className="size-3.5 shrink-0" aria-hidden />
            <a href={link(c.path)} onClick={go(link(c.path))} onPointerEnter={() => onPrefetch(link(c.path))}>
              {c.title}
            </a>
          </span>
        ))}
      </nav>
      <h1 className="doc-title">{page.title}</h1>
      {page.fallback && (
        <p className="doc-fallback" role="note">
          <Globe className="size-4 shrink-0" />
          {t("docsSite.fallback", { language: languageName(lang), original: languageName(site.language) })}
        </p>
      )}
      {page.toc.filter((h) => h.level <= 3).length > 2 && <MobileToc page={page} />}
      <div className="doc-prose">
        <Content html={page.html} filesBase={`${site.api}/files`} onNavigate={onNavigate} onPrefetch={onPrefetch} />
      </div>
      {sub.length > 0 && (
        <section className="doc-children" aria-label={t("docsSite.inSection")}>
          <h2>{t("docsSite.inSection")}</h2>
          <ul>
            {sub.map((c) => (
              <li key={c.id}>
                <a href={link(c.path)} onClick={go(link(c.path))} onPointerEnter={() => onPrefetch(link(c.path))}>
                  <span>{c.title}</span>
                  {c.children > 0 && <span className="doc-children-count">{t("docsSite.pagesCount", { count: c.children })}</span>}
                  <ArrowRight className="size-4 shrink-0" />
                </a>
              </li>
            ))}
            {page.children.total > sub.length && <li className="doc-children-more">{t("docsSite.more", { count: page.children.total - sub.length })}</li>}
          </ul>
        </section>
      )}
      <p className="doc-updated">{t("docsSite.updated", { date: dateIn(lang, page.updated_at) })}</p>
      {(page.prev || page.next) && (
        <nav className="doc-pager" aria-label={t("docsSite.pager")}>
          {page.prev ? (
            <a href={link(page.prev.path)} onClick={go(link(page.prev.path))} onPointerEnter={() => onPrefetch(link(page.prev!.path))} className="is-prev">
              <span className="doc-pager-label">
                <ArrowLeft className="size-3.5" /> {t("docsSite.previous")}
              </span>
              <span className="doc-pager-title">{page.prev.title}</span>
            </a>
          ) : (
            <span />
          )}
          {page.next && (
            <a href={link(page.next.path)} onClick={go(link(page.next.path))} onPointerEnter={() => onPrefetch(link(page.next!.path))} className="is-next">
              <span className="doc-pager-label">
                {t("docsSite.next")} <ArrowRight className="size-3.5" />
              </span>
              <span className="doc-pager-title">{page.next.title}</span>
            </a>
          )}
        </nav>
      )}
    </article>
  );
}

/** A page of the site's own (imprint, privacy …): its text, without the docs' tree around it. */
function CustomArticle({ site, page, lang, home, onNavigate, onPrefetch }: { site: SiteView; page: CustomView; lang: string; home: string; onNavigate: (href: string) => void; onPrefetch: (href: string) => void }) {
  const { t } = useTranslation();
  return (
    <article key={`${page.id}-${page.lang}`} className="doc-article">
      <nav aria-label={t("docsSite.breadcrumbs")} className="doc-crumbs">
        <a
          href={home}
          onClick={(e) => {
            if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
            e.preventDefault();
            onNavigate(home);
          }}
        >
          {site.name}
        </a>
      </nav>
      <h1 className="doc-title">{page.title}</h1>
      {page.fallback && (
        <p className="doc-fallback" role="note">
          <Globe className="size-4 shrink-0" />
          {t("docsSite.fallback", { language: languageName(lang), original: languageName(site.language) })}
        </p>
      )}
      {page.toc.filter((h) => h.level <= 3).length > 2 && <MobileToc page={page} />}
      <div className="doc-prose">
        <Content html={page.html} filesBase={`${site.api}/files`} onNavigate={onNavigate} onPrefetch={onPrefetch} />
      </div>
      <p className="doc-updated">{t("docsSite.updated", { date: dateIn(lang, page.updated_at) })}</p>
    </article>
  );
}

type Headings = Pick<PageView, "toc">;

/** "On this page": the headings of the page, the one being read marked. */
function Toc({ page }: { page: Headings }) {
  const { t } = useTranslation();
  const heads = page.toc.filter((h) => h.level >= 2 && h.level <= 3);
  const active = useActiveHeading(heads.map((h) => h.id));
  if (heads.length < 2) return null;
  return (
    <nav aria-label={t("docsSite.onThisPage")} className="doc-toc-in">
      <p className="doc-toc-head">
        <ListTree className="size-4" /> {t("docsSite.onThisPage")}
      </p>
      <ul>
        {heads.map((h) => (
          <li key={h.id} className={clsx(h.level === 3 && "is-sub")}>
            <a
              href={`#${h.id}`}
              className={clsx(active === h.id && "is-active")}
              onClick={(e) => {
                e.preventDefault();
                history.replaceState(history.state, "", `#${h.id}`);
                document.getElementById(h.id)?.scrollIntoView({ behavior: "smooth" });
              }}
            >
              {h.text}
            </a>
          </li>
        ))}
      </ul>
      <button type="button" className="doc-link-btn" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
        {t("docsSite.top")}
      </button>
    </nav>
  );
}

function MobileToc({ page }: { page: Headings }) {
  const { t } = useTranslation();
  const heads = page.toc.filter((h) => h.level >= 2 && h.level <= 3);
  return (
    <details className="doc-toc-mobile">
      <summary>
        <ListTree className="size-4" /> {t("docsSite.onThisPage")}
        <ChevronDown className="ml-auto size-4 doc-toc-chev" />
      </summary>
      <ul>
        {heads.map((h) => (
          <li key={h.id} className={clsx(h.level === 3 && "is-sub")}>
            <a href={`#${h.id}`}>{h.text}</a>
          </li>
        ))}
      </ul>
    </details>
  );
}

function useActiveHeading(ids: string[]): string | null {
  const [active, setActive] = useState<string | null>(null);
  const key = ids.join(" ");
  useEffect(() => {
    let frame = 0;
    const read = () => {
      frame = 0;
      let found: string | null = ids[0] ?? null;
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top < 120) found = id;
      }
      // At the very end of the page the last heading counts, however short its part is.
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) found = ids[ids.length - 1] ?? found;
      setActive(found);
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(read);
    };
    read();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      cancelAnimationFrame(frame);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return active;
}

function LanguageMenu({ languages, current, onChoose }: { languages: string[]; current: string; onChoose: (code: string) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, open, () => setOpen(false));
  return (
    <div ref={ref} className="doc-lang">
      <button type="button" className="doc-lang-btn" onClick={() => setOpen((o) => !o)} aria-haspopup="menu" aria-expanded={open} aria-label={t("docsSite.language")}>
        <Globe className="size-4" />
        <span>{languageName(current)}</span>
        <ChevronDown className="size-3.5" />
      </button>
      {open && (
        <div role="menu" className="doc-menu">
          {languages.map((code) => (
            <button
              key={code}
              type="button"
              role="menuitemradio"
              aria-checked={code === current}
              onClick={() => {
                setOpen(false);
                if (code !== current) onChoose(code);
              }}
            >
              <span>{languageName(code)}</span>
              <span className="doc-menu-code">{code}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Which version is read, and the others (#126): the one in development first, then the newest released. */
function VersionMenu({ versions, current, busy, onChoose }: { versions: VersionItem[]; current: string; busy: boolean; onChoose: (v: VersionItem) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, open, () => setOpen(false));
  const shown = versions.find((v) => v.slug === current);
  const note = (v: VersionItem) => (v.working ? t("docsSite.versionWorking") : v.deprecated ? t("docsSite.versionDeprecated") : v.default ? t("docsSite.versionLatest") : "");
  return (
    <div ref={ref} className="doc-lang doc-version">
      <button
        type="button"
        className="doc-lang-btn doc-version-btn"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`${t("docsSite.version")}: ${shown?.name ?? current}`}
        title={t("docsSite.version")}
        disabled={busy}
      >
        <Tag className="size-4" />
        <span className="doc-version-name">{shown?.name ?? current}</span>
        <ChevronDown className="size-3.5" />
      </button>
      {open && (
        <div role="menu" aria-label={t("docsSite.versions")} className="doc-menu">
          {versions.map((v) => (
            <button
              key={v.slug}
              type="button"
              role="menuitemradio"
              aria-checked={v.slug === current}
              onClick={() => {
                setOpen(false);
                if (v.slug !== current) onChoose(v);
              }}
            >
              <span>{v.name}</span>
              {note(v) && <span className={clsx("doc-menu-code doc-version-note", v.working && "is-working")}>{note(v)}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Above the page when it is not the version most readers want: the one in development, a deprecated or an older one. */
function VersionBanner({ site, lang, path, onGo }: { site: SiteView; lang: string; path: string; onGo: (v: VersionItem) => void }) {
  const { t } = useTranslation();
  const v = site.version;
  const list = site.versions ?? [];
  const current = list.find((x) => x.default);
  if (!site.versioned || !v || v.default || !current) return null;
  const here = list.findIndex((x) => x.slug === v.slug);
  const older = here > list.indexOf(current);
  if (!v.working && !v.deprecated && !older) return null;
  const href = `${versionBase(site, current)}${lang === site.language ? "" : `/${lang}`}/${path}`;
  return (
    <div className={clsx("doc-version-banner", v.working && "is-working")} role="note">
      <Info className="size-4 shrink-0" aria-hidden />
      <p>
        {v.working ? t("docsSite.bannerWorking") : v.deprecated ? t("docsSite.bannerDeprecated", { name: v.name }) : t("docsSite.bannerOld", { name: v.name })}{" "}
        <a
          href={href}
          onClick={(e) => {
            if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
            e.preventDefault();
            onGo(current);
          }}
        >
          {t("docsSite.bannerToCurrent", { name: current.name })}
        </a>
      </p>
    </div>
  );
}

function Message({ title, text, action }: { title: string; text: string; action?: ReactNode }) {
  return (
    <div className="doc-message">
      <h1 className="doc-title">{title}</h1>
      <p>{text}</p>
      {action && <div className="doc-message-actions">{action}</div>}
    </div>
  );
}
