import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext } from "react";
import { api } from "../../api/client";
import type { Attachment, DocCustomPage, DocDomain, DocPage, DocPageItem, DocPages, DocRelease, DocSite, DocTranslation, DocVersion } from "../../api/types";

/** The version of the site being edited (#126): its id, null for the working version. Every page route takes it. */
export const ReleaseContext = createContext<number | null>(null);
export const useRelease = () => useContext(ReleaseContext);

/** A page route's address in that version: `?release=<id>` added (nothing for the working version). */
export const rel = (url: string, release: number | null) => (release === null ? url : `${url}${url.includes("?") ? "&" : "?"}release=${release}`);

/** The version a release id stands for (the working one for null). */
export const releaseOf = (site: DocSite, release: number | null): DocRelease | undefined =>
  site.releases.find((r) => (release === null ? r.working : r.id === release));

/** Every key starts with ["docs", project]: one live event of a site or page refreshes what is shown. */
export const dk = {
  all: (pid: number) => ["docs", pid] as const,
  sites: (pid: number) => ["docs", pid, "sites"] as const,
  children: (pid: number, site: number, parent: number | null, release: number | null = null) => ["docs", pid, "children", site, parent ?? "top", release ?? "w"] as const,
  page: (pid: number, id: number, release: number | null = null) => ["docs", pid, "page", id, release ?? "w"] as const,
  versions: (pid: number, id: number) => ["docs", pid, "versions", id] as const,
  version: (pid: number, page: number, id: number) => ["docs", pid, "version", page, id] as const,
  translation: (pid: number, page: number, lang: string, release: number | null = null) => ["docs", pid, "translation", page, lang, release ?? "w"] as const,
  files: (pid: number, site: number) => ["docs", pid, "files", site] as const,
  domain: (pid: number, site: number) => ["docs", pid, "domain", site] as const,
  find: (pid: number, site: number, q: string, release: number | null = null) => ["docs", pid, "find", site, q, release ?? "w"] as const,
  custom: (pid: number, site: number) => ["docs", pid, "custom", site] as const,
};

export const base = (pid: number) => `/projects/${pid}/docs`;

export const useDocSites = (pid: number) => useQuery({ queryKey: dk.sites(pid), queryFn: () => api.get<DocSite[]>(`${base(pid)}/sites`) });

/** The children of a page (null: the top), the first 200; more with `offset`. */
export function useDocChildren(pid: number, site: number, parent: number | null, enabled = true) {
  const release = useRelease();
  return useQuery({
    queryKey: dk.children(pid, site, parent, release),
    queryFn: () => api.get<DocPages>(rel(`${base(pid)}/sites/${site}/pages?limit=200${parent === null ? "" : `&parent=${parent}`}`, release)),
    enabled,
    staleTime: 10_000,
  });
}

export function useDocPage(pid: number, id: number | null) {
  const release = useRelease();
  return useQuery({ queryKey: dk.page(pid, id ?? 0, release), queryFn: () => api.get<DocPage>(rel(`${base(pid)}/pages/${id}`, release)), enabled: id !== null });
}

export function useDocVersions(pid: number, page: number, enabled: boolean) {
  const release = useRelease();
  return useQuery({ queryKey: dk.versions(pid, page), queryFn: () => api.get<DocVersion[]>(rel(`${base(pid)}/pages/${page}/versions`, release)), enabled });
}

export function useDocVersion(pid: number, page: number, id: number | null) {
  const release = useRelease();
  return useQuery({
    queryKey: dk.version(pid, page, id ?? 0),
    queryFn: () => api.get<DocVersion>(rel(`${base(pid)}/pages/${page}/versions/${id}`, release)),
    enabled: id !== null,
    staleTime: Infinity, // a version never changes
  });
}

export function useDocTranslation(pid: number, page: number, lang: string | null) {
  const release = useRelease();
  return useQuery({
    queryKey: dk.translation(pid, page, lang ?? "", release),
    queryFn: () => api.get<DocTranslation>(rel(`${base(pid)}/pages/${page}/translations/${lang}`, release)),
    enabled: !!lang,
  });
}

export const useDocFiles = (pid: number, site: number, enabled = true) =>
  useQuery({ queryKey: dk.files(pid, site), queryFn: () => api.get<Attachment[]>(`${base(pid)}/sites/${site}/files?limit=500`), enabled });

export const useDocDomain = (pid: number, site: number, enabled: boolean) =>
  useQuery({ queryKey: dk.domain(pid, site), queryFn: () => api.get<DocDomain | null>(`${base(pid)}/sites/${site}/domain`), enabled });

export function useFindPages(pid: number, site: number, q: string) {
  const release = useRelease();
  return useQuery({
    queryKey: dk.find(pid, site, q, release),
    queryFn: () => api.get<DocPageItem[]>(rel(`${base(pid)}/sites/${site}/pages/find?q=${encodeURIComponent(q)}&limit=50`, release)),
    enabled: q.length > 0,
    placeholderData: (prev) => prev,
  });
}

export const useDocCustomPages = (pid: number, site: number) =>
  useQuery({ queryKey: dk.custom(pid, site), queryFn: () => api.get<DocCustomPage[]>(`${base(pid)}/sites/${site}/custom-pages`) });

/** After a change: everything of the project's docs is fetched again (it is small: only what is open). */
export function useDocsRefresh(pid: number) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: dk.all(pid) });
}

/** The site's address in the browser: its own domain once verified, else here. */
export function siteHref(site: DocSite): string {
  return site.domain && site.domain_verified_at ? `https://${site.domain}/` : site.url;
}

/** A version's address in the browser (the site's for its default one, or while the site uses no versions). */
export function releaseHref(site: DocSite, release: DocRelease | undefined): string {
  if (!release || !site.versioned || release.default) return siteHref(site);
  return site.domain && site.domain_verified_at ? `https://${site.domain}/${release.slug}/` : release.url;
}
