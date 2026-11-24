import type { CustomView, Nav, PageView, SearchHit, SiteView, VersionItem } from "./types";

export class NotFoundError extends Error {}

/** `&release=<slug>` for the requests of a version other than the default one (#126); nothing otherwise. */
export const releaseParam = (site: SiteView) => (site.version && !site.version.default ? `&release=${encodeURIComponent(site.version.slug)}` : "");

/** The address of a version (the site's own for the default one). */
export const versionBase = (site: SiteView, v: VersionItem) => (v.default ? (site.site_base ?? site.base) : `${site.site_base ?? site.base}/${v.slug}`);

/** The same page in another version, or its start when the page is not there (a site's own page is in every one). */
export async function inVersion(site: SiteView, v: VersionItem, lang: string, path: string): Promise<string> {
  const root = `${versionBase(site, v)}${lang === site.language ? "" : `/${lang}`}`;
  if (path && !customSlug(site, path)) {
    const resp = await fetch(`${site.api}/page?path=${encodeURIComponent(path)}&lang=${encodeURIComponent(lang)}&release=${encodeURIComponent(v.slug)}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).catch(() => null);
    if (!resp?.ok) return `${root}/`;
  }
  return `${root}/${path}`;
}

/** The base of the site in a language: its own language at the site's base, every other one below it. */
export const langBase = (site: SiteView, lang: string) => (lang === site.language ? site.base : `${site.base}/${lang}`);

/** The address of a page ("" is the home page). */
export const pageUrl = (site: SiteView, lang: string, path: string) => `${langBase(site, lang)}/${path}`;

/** Which page of the site an address is, or null when it is not one of its pages (a file, another site). */
export function parseUrl(site: SiteView, url: URL): { lang: string; path: string; hash: string } | null {
  if (url.origin !== location.origin) return null;
  let rest = url.pathname;
  if (site.base) {
    if (rest !== site.base && !rest.startsWith(site.base + "/")) return null;
    rest = rest.slice(site.base.length);
  }
  if (rest.startsWith("/api/") || rest.startsWith("/assets/")) return null;
  const parts = rest.split("/").filter(Boolean).map(decodeURIComponent);
  // Another version of the site (#126): a document of its own.
  if (parts.length && site.versions?.some((v) => v.slug === parts[0])) return null;
  let lang = site.language;
  if (parts.length && parts[0] !== site.language && site.languages.includes(parts[0])) lang = parts.shift()!;
  const path = parts.join("/");
  if (path === "sitemap.xml" || path === "robots.txt") return null;
  return { lang, path, hash: decodeURIComponent(url.hash.slice(1)) };
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (resp.status === 404) throw new NotFoundError();
  if (resp.status === 401) {
    location.reload(); // signed out meanwhile: the server shows the sign-in
    throw new Error("Sign in");
  }
  if (!resp.ok) throw new Error(resp.status === 429 ? "429" : resp.statusText);
  return (await resp.json()) as T;
}

// A page fetched once (or while it is being fetched) is not fetched again: a link that was pointed at is
// ready when it is clicked. The site changes rarely; a few hundred pages are kept.
const pages = new Map<string, Promise<PageView>>();
const PAGES_KEPT = 300;

export function loadPage(site: SiteView, lang: string, path: string): Promise<PageView> {
  const key = `${lang}\n${path}`;
  let found = pages.get(key);
  if (!found) {
    found = getJson<PageView>(`${site.api}/page?path=${encodeURIComponent(path)}&lang=${encodeURIComponent(lang)}${releaseParam(site)}`);
    found.catch(() => pages.delete(key));
    pages.set(key, found);
    if (pages.size > PAGES_KEPT) pages.delete(pages.keys().next().value!);
  }
  return found;
}

/** Seeds the cache with the page the server rendered. */
export function knownPage(page: PageView, path: string) {
  pages.set(`${page.lang}\n${path}`, Promise.resolve(page));
}

export function prefetch(site: SiteView, href: string) {
  const target = parseUrl(site, new URL(href, location.href));
  if (!target) return;
  const slug = customSlug(site, target.path);
  (slug ? loadCustom(site, target.lang, slug) : loadPage(site, target.lang, target.path)).catch(() => undefined);
}

export const loadNav = (site: SiteView, lang: string, parent: number | null, offset = 0) =>
  getJson<Nav>(`${site.api}/nav?lang=${encodeURIComponent(lang)}&offset=${offset}${parent === null ? "" : `&parent=${parent}`}${releaseParam(site)}`);

const searches = new Map<string, Promise<SearchHit[]>>();

export function search(site: SiteView, lang: string, query: string): Promise<SearchHit[]> {
  const key = `${lang}\n${query}`;
  let found = searches.get(key);
  if (!found) {
    found = getJson<SearchHit[]>(`${site.api}/search?q=${encodeURIComponent(query)}&lang=${encodeURIComponent(lang)}&limit=12${releaseParam(site)}`);
    found.catch(() => searches.delete(key));
    searches.set(key, found);
  }
  return found;
}

/** The slug of the site's own page at this address (p/<slug>), or null for a docs page. */
export function customSlug(site: SiteView, path: string): string | null {
  return site.footer?.find((f) => f.path === path)?.slug ?? null;
}

const customs = new Map<string, Promise<CustomView>>();

export function loadCustom(site: SiteView, lang: string, slug: string): Promise<CustomView> {
  const key = `${lang}\n${slug}`;
  let found = customs.get(key);
  if (!found) {
    found = getJson<CustomView>(`${site.api}/custom/${encodeURIComponent(slug)}?lang=${encodeURIComponent(lang)}${releaseParam(site)}`);
    found.catch(() => customs.delete(key));
    customs.set(key, found);
  }
  return found;
}

export function knownCustom(page: CustomView) {
  customs.set(`${page.lang}\n${page.slug}`, Promise.resolve(page));
}
