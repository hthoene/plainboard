/** What the server hands a served doc site (app/plainboard/services/docs_public.py). */

export interface NavItem {
  id: number;
  title: string;
  path: string;
  /** How many pages are directly below it. */
  children: number;
  /** Pages at the top: their area and group (#159). */
  area?: number | null;
  group?: number | null;
}

/** An area of the navigation (#159): a switcher at the top of it, the site's parts. */
export interface NavArea {
  id: number;
  title: string;
  description: string;
  /** A lucide icon name (areaIcons.ts). */
  icon: string;
}

/** A heading over pages at the top, in an area or (none) in every one. */
export interface NavGroup {
  id: number;
  area: number | null;
  title: string;
}

export interface Nav {
  items: NavItem[];
  total: number;
  offset: number;
}

export interface Near {
  title: string;
  path: string;
}

export interface SiteView {
  name: string;
  slug: string;
  description: string;
  public: boolean;
  /** The site's address: "" at its own domain, /docs/<slug> here. */
  base: string;
  /** Its JSON: /api/public/docs/<slug> */
  api: string;
  language: string;
  /** Its own language first, then the further ones. */
  languages: string[];
  lang: string;
  lang_base: string;
  project: string;
  updated_at: string;
  home: string | null;
  nav: Nav;
  /** Areas and groups of the navigation (#159); none: the tree as it is. */
  areas?: NavArea[];
  groups?: NavGroup[];
  /** Its AI assistant is on (#119): POST {api}/assistant. */
  assistant?: boolean;
  /** Its own look (#121): null for the look every site has (the colours and font come with the page's head). */
  theme?: SiteTheme | null;
  /** Its own pages (imprint, privacy …) for the footer, in their order. */
  footer?: FooterLink[];
  /** It uses versions (#126): then the version shown, the ones to switch to, and its address without a version. */
  versioned?: boolean;
  version?: VersionInfo | null;
  versions?: VersionItem[];
  site_base?: string;
}

/** A version a visitor may switch to: the one in development first, then the newest released. */
export interface VersionItem {
  slug: string;
  name: string;
  /** In development (the next one). */
  working: boolean;
  deprecated: boolean;
  /** What the site's address without a version shows. */
  default: boolean;
  released_on: string | null;
}

export interface VersionInfo extends VersionItem {
  notes: string;
}

export interface SiteTheme {
  logo: string | null;
  logo_dark: string | null;
  /** The site's name beside the logo. */
  show_name: boolean;
  links: { label: string; url: string }[];
}

export interface FooterLink {
  slug: string;
  title: string;
  /** Its title in each of the site's languages. */
  titles: Record<string, string>;
  /** p/<slug> */
  path: string;
}

/** A page of the site's own, outside the docs tree (services/docs_custom.py). */
export interface CustomView {
  id: number;
  slug: string;
  path: string;
  title: string;
  lang: string;
  fallback: boolean;
  html: string;
  toc: { level: number; id: string; text: string }[];
  description: string;
  updated_at: string;
}

export interface PageView {
  id: number;
  title: string;
  path: string;
  lang: string;
  /** Not translated yet: the original is shown. */
  fallback: boolean;
  html: string;
  toc: { level: number; id: string; text: string }[];
  markdown: string;
  description: string;
  updated_at: string;
  breadcrumbs: Near[];
  prev: Near | null;
  next: Near | null;
  /** The top level, then the children of each page on the way down to this one. */
  sidebar: Nav[];
  children: Nav;
}

export interface SearchHit {
  title: string;
  path: string;
  url: string;
  snippet: string;
}

export interface DocsState {
  site: SiteView;
  page: PageView | null;
  /** The page is one of the site's own (p/<slug>). */
  custom?: CustomView | null;
  /** A private site and nobody signed in: the sign-in. */
  sign_in?: boolean;
}

declare global {
  interface Window {
    __PB_DOCS__?: DocsState;
  }
}
