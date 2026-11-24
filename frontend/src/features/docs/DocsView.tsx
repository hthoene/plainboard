import clsx from "clsx";
import {
  BookOpen,
  Bot,
  ChevronLeft,
  CircleAlert,
  Copy,
  ExternalLink,
  FileText,
  Globe,
  Languages,
  Lock,
  MessageSquare,
  Plus,
  ScrollText,
  Tag,
  Trash2,
  Upload,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import type { DocPageItem, DocSite } from "../../api/types";
import { Button, ChatPanelToggle, EmptyState, ErrorText, Field, IconButton, Input, Modal, Spinner, Textarea } from "../../components/ui";
import { formatBytes, timeAgo } from "../../lib/format";
import { useStored } from "../../lib/hooks";
import { useShell } from "../layout/shell-context";
import { DocMarkdown } from "./DocMarkdown";
import { languageLabel, PageEditor } from "./PageEditor";
import { PageTree } from "./PageTree";
import { base, rel, ReleaseContext, releaseHref, releaseOf, siteHref, useDocFiles, useDocPage, useDocSites, useDocsRefresh, useRelease } from "./queries";
import { ReleaseBadges, ReleasePicker, ReleasesTab } from "./Releases";
import { SiteSettings, Visibility } from "./SiteSettings";

type Tab = "pages" | "rules" | "files" | "versions" | "settings";

/** The project's doc sites (#118): a list of them, and per site its pages, its doc rules, its files and its
 * settings. Its doc agent writes it; people in the project can write too, viewers read. `focus`: a page to show
 * (found in the search). */
export function DocsView({ focus }: { focus?: { pageId: number; n: number } }) {
  const { project, canWrite } = useShell();
  const sites = useDocSites(project.id);
  const [siteId, setSiteId] = useStored<number | null>(`plainboard.docSite.${project.id}`, null);
  const [pageId, setPageId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("pages");
  const [creating, setCreating] = useState(false);

  // A page found in the search: its site, that page.
  useEffect(() => {
    if (!focus) return;
    api
      .get<DocPageItem>(`${base(project.id)}/pages/${focus.pageId}`)
      .then((p) => {
        setSiteId(p.site_id);
        setPageId(p.id);
        setTab("pages");
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.n]);

  if (sites.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }
  const site = sites.data?.find((s) => s.id === siteId);
  return (
    <div className="flex h-full flex-col">
      {site ? (
        <SiteWorkspace
          key={site.id}
          site={site}
          tab={tab}
          setTab={setTab}
          pageId={pageId}
          setPageId={setPageId}
          onBack={() => {
            setSiteId(null);
            setPageId(null);
          }}
        />
      ) : (
        <SiteList
          sites={sites.data ?? []}
          error={sites.error}
          canWrite={canWrite}
          onOpen={(id) => {
            setSiteId(id);
            setPageId(null);
            setTab("pages");
          }}
          onNew={() => setCreating(true)}
        />
      )}
      {creating && (
        <NewSiteDialog
          onClose={() => setCreating(false)}
          onCreated={(s) => {
            setCreating(false);
            setSiteId(s.id);
            setPageId(null);
            setTab("rules");
          }}
        />
      )}
    </div>
  );
}

function Status({ site }: { site: DocSite }) {
  const { t } = useTranslation();
  const state = !site.published ? "draft" : site.public ? "public" : "private";
  return (
    <span data-part="badge" className={clsx("inline-flex h-6 shrink-0 items-center gap-1 rounded-sm px-1.5 whitespace-nowrap ring-1 ring-inset", state === "public" ? "text-ok ring-line-2" : "text-fg-2 ring-line-2")}>
      {state === "public" ? <Globe className="size-3.5" /> : state === "private" ? <Lock className="size-3.5" /> : <FileText className="size-3.5" />}
      {t(`docs.visibility.${state}`)}
    </span>
  );
}

// --- the list -------------------------------------------------------------------------------------------------

function SiteList({ sites, error, canWrite, onOpen, onNew }: { sites: DocSite[]; error: unknown; canWrite: boolean; onOpen: (id: number) => void; onNew: () => void }) {
  const { t } = useTranslation();
  const { agents, openChat } = useShell();
  const manager = agents.find((a) => a.is_manager);
  return (
    <>
      <header data-part="page-head" className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
        <h1 className="min-w-0 flex-1 truncate font-extrabold">{t("docs.title")}</h1>
        {canWrite && sites.length > 0 && (
          <Button variant="primary" icon={<Plus className="size-4" />} onClick={onNew}>
            {t("docs.new")}
          </Button>
        )}
        <ChatPanelToggle where="view" divider />
      </header>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        <ErrorText error={error} />
        {sites.length === 0 ? (
          <EmptyState icon={<BookOpen className="size-10" />} title={t("docs.empty")}>
            <p className="mb-4">{t("docs.emptyHint")}</p>
            {canWrite && (
              <div className="flex flex-wrap justify-center gap-2">
                {manager && (
                  <Button icon={<MessageSquare className="size-4" />} onClick={() => openChat(manager.id)}>
                    {t("docs.askManager")}
                  </Button>
                )}
                <Button variant="primary" icon={<Plus className="size-4" />} onClick={onNew}>
                  {t("docs.new")}
                </Button>
              </div>
            )}
          </EmptyState>
        ) : (
          <ul className="mx-auto max-w-4xl">
            <li className="px-4 pt-4 pb-3 text-fg-2">{t("docs.intro")}</li>
            {sites.map((s) => (
              <li key={s.id} className="border-b border-line first-of-type:border-t">
                <button type="button" onClick={() => onOpen(s.id)} className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-sunk touch:py-4">
                  <BookOpen className="mt-0.5 size-4 shrink-0 text-fg-3" />
                  <span className="min-w-0 flex-1 space-y-0.5">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold">{s.name}</span>
                      <Status site={s} />
                    </span>
                    {s.description && <span className="block text-fg-2">{s.description}</span>}
                    <span className="flex flex-wrap gap-x-3 gap-y-0.5 text-fg-3">
                      <span>{t("docs.pagesCount", { count: s.pages })}</span>
                      {s.stale_pages > 0 && (
                        <span className="inline-flex items-center gap-1 text-fg-2">
                          <CircleAlert className="size-3.5" />
                          {t("docs.staleCount", { count: s.stale_pages })}
                        </span>
                      )}
                      <span className="uppercase">{[s.language, ...s.languages].join(" · ")}</span>
                      <span className="truncate">{s.domain && s.domain_verified_at ? s.domain : s.url}</span>
                      <span>{t("docs.updated", { when: timeAgo(s.updated_at) })}</span>
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

function NewSiteDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (s: DocSite) => void }) {
  const { t } = useTranslation();
  const { project } = useShell();
  const refresh = useDocsRefresh(project.id);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [spec, setSpec] = useState("");
  const [published, setPublished] = useState(false);
  const [isPublic, setPublic] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const site = await api.post<DocSite>(`${base(project.id)}/sites`, { name: name.trim(), description: description.trim(), spec, published, public: isPublic });
      await refresh();
      onCreated(site);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      onClose={onClose}
      title={t("docs.new")}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="primary" onClick={create} loading={busy} disabled={!name.trim()}>
            {t("docs.create")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="text-fg-2">{t("docs.newHint")}</p>
        <Field label={t("docs.settings.name")} hint={t("docs.settings.nameHint")}>
          <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder={t("docs.namePlaceholder")} />
        </Field>
        <Field label={t("docs.settings.description")} hint={t("docs.settings.descriptionHint")}>
          <Input value={description} maxLength={300} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Visibility published={published} isPublic={isPublic} onPublished={setPublished} onPublic={setPublic} />
        <Field label={t("docs.rules.title")} hint={t("docs.rules.newHint")}>
          <Textarea rows={6} value={spec} onChange={(e) => setSpec(e.target.value)} placeholder={t("docs.rules.placeholder")} />
        </Field>
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}

// --- one site -------------------------------------------------------------------------------------------------

function SiteWorkspace({
  site,
  tab,
  setTab,
  pageId,
  setPageId,
  onBack,
}: {
  site: DocSite;
  tab: Tab;
  setTab: (t: Tab) => void;
  pageId: number | null;
  setPageId: (id: number | null) => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const { project, canWrite, isOwner } = useShell();
  const [section, setSection] = useState("general");
  // The version being edited (#126): null is the working one; a version deleted meanwhile falls back to it.
  const [stored, setRelease] = useStored<number | null>(`plainboard.docRelease.${site.id}`, null);
  const release = stored !== null && site.releases.some((r) => r.id === stored && !r.working) ? stored : null;
  const hasReleases = site.releases.some((r) => !r.working);
  const tabs: { id: Tab; label: string; icon: ReactNode }[] = [
    { id: "pages", label: t("docs.tabs.pages"), icon: <FileText className="size-4" /> },
    { id: "rules", label: t("docs.tabs.rules"), icon: <ScrollText className="size-4" /> },
    { id: "files", label: t("docs.tabs.files"), icon: <Upload className="size-4" /> },
    { id: "versions", label: t("docs.tabs.versions"), icon: <Tag className="size-4" /> },
    ...(canWrite ? [{ id: "settings" as Tab, label: t("docs.tabs.settings"), icon: <Languages className="size-4" /> }] : []),
  ];
  return (
    <>
      <header data-part="page-head" className="shrink-0 border-b border-line">
        <div className="flex h-12 items-center gap-1.5 px-2 sm:px-3">
          <IconButton label={t("docs.allSites")} onClick={onBack}>
            <ChevronLeft className="size-4" />
          </IconButton>
          <h1 className="min-w-0 truncate font-extrabold">{site.name}</h1>
          <Status site={site} />
          <nav aria-label={t("docs.tabsLabel")} className="mx-2 hidden h-full items-stretch gap-1 md:flex">
            {tabs.map((x) => (
              <TabButton key={x.id} active={tab === x.id} onClick={() => setTab(x.id)}>
                {x.label}
              </TabButton>
            ))}
          </nav>
          <div className="flex-1" />
          {site.published && (
            <a href={releaseHref(site, releaseOf(site, release))} target="_blank" rel="noreferrer" className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-sm px-2.5 font-medium text-fg-2 hover:bg-sunk hover:text-fg touch:h-11">
              <ExternalLink className="size-4" />
              <span className="max-sm:hidden">{t("docs.openSite")}</span>
            </a>
          )}
          <ChatPanelToggle where="view" divider />
        </div>
        <nav aria-label={t("docs.tabsLabel")} className="flex h-11 items-stretch gap-1 overflow-x-auto px-2 md:hidden">
          {tabs.map((x) => (
            <TabButton key={x.id} active={tab === x.id} onClick={() => setTab(x.id)}>
              {x.label}
            </TabButton>
          ))}
        </nav>
      </header>
      <ReleaseContext.Provider value={release}>
      <div className="flex min-h-0 flex-1">
        {tab === "pages" && (
          <PagesTab
            site={site}
            pageId={pageId}
            setPageId={setPageId}
            onRules={() => setTab("rules")}
            picker={hasReleases ? <ReleasePicker site={site} release={release} onRelease={setRelease} onManage={() => setTab("versions")} /> : null}
          />
        )}
        {tab === "rules" && <RulesTab site={site} />}
        {tab === "files" && <FilesTab site={site} />}
        {tab === "versions" && (
          <ReleasesTab
            site={site}
            canWrite={canWrite}
            onEdit={(id) => {
              setRelease(id);
              setTab("pages");
            }}
          />
        )}
        {tab === "settings" && canWrite && (
          <SiteSettings pid={project.id} site={site} isOwner={isOwner} section={section} onSection={setSection} onDeleted={onBack} />
        )}
      </div>
      </ReleaseContext.Provider>
    </>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={clsx(
        "relative flex shrink-0 items-center px-2.5 whitespace-nowrap",
        active ? "font-semibold text-fg after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:bg-fg" : "text-fg-2 hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}

// --- pages ----------------------------------------------------------------------------------------------------

function PagesTab({ site, pageId, setPageId, onRules, picker }: { site: DocSite; pageId: number | null; setPageId: (id: number | null) => void; onRules: () => void; picker: ReactNode }) {
  const { project, canWrite } = useShell();
  const [newIn, setNewIn] = useState<DocPageItem | null | undefined>(undefined);
  const release = useRelease();
  const page = useDocPage(project.id, pageId);
  const opened = page.data ? [...page.data.ancestors.map((a) => a.id)] : [];
  return (
    <div className="flex min-h-0 min-w-0 flex-1">
      <aside className={clsx("flex w-full shrink-0 flex-col border-line md:w-72 md:border-r xl:w-80", pageId !== null && "max-md:hidden")}>
        {picker}
        <PageTree key={release ?? "w"} pid={project.id} site={site} selected={pageId} opened={opened} canWrite={canWrite} onSelect={setPageId} onNew={(p) => setNewIn(p)} />
      </aside>
      <div className={clsx("flex min-w-0 flex-1", pageId === null && "max-md:hidden")}>
        {pageId !== null ? (
          <PageEditor key={`${pageId}-${release}`} pid={project.id} site={site} pageId={pageId} canWrite={canWrite} onBack={() => setPageId(null)} onOpenPage={setPageId} />
        ) : (
          <Overview site={site} onRules={onRules} />
        )}
      </div>
      {newIn !== undefined && (
        <NewPageDialog
          site={site}
          parent={newIn}
          onClose={() => setNewIn(undefined)}
          onCreated={(id) => {
            setNewIn(undefined);
            setPageId(id);
          }}
        />
      )}
    </div>
  );
}

function NewPageDialog({ site, parent, onClose, onCreated }: { site: DocSite; parent: DocPageItem | null; onClose: () => void; onCreated: (id: number) => void }) {
  const { t } = useTranslation();
  const { project } = useShell();
  const refresh = useDocsRefresh(project.id);
  const release = useRelease();
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const create = async () => {
    setBusy(true);
    try {
      const page = await api.post<DocPageItem>(rel(`${base(project.id)}/sites/${site.id}/pages`, release), { title: title.trim(), body: "", parent_id: parent?.id ?? null });
      await refresh();
      onCreated(page.id);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-md"
      title={parent ? t("docs.newPageBelow", { title: parent.title }) : t("docs.newPage")}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="primary" loading={busy} disabled={!title.trim()} onClick={create}>
            {t("docs.create")}
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (title.trim()) void create();
        }}
        className="space-y-3"
      >
        <Field label={t("docs.page.title")} hint={t("docs.newPageHint")}>
          <Input autoFocus value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <ErrorText error={error} />
      </form>
    </Modal>
  );
}

/** What a site is at a glance, beside its tree while no page is open. */
function Overview({ site, onRules }: { site: DocSite; onRules: () => void }) {
  const { t } = useTranslation();
  const release = useRelease();
  const fact = (icon: ReactNode, label: string, value: ReactNode) => (
    <div className="flex items-start gap-3 border-b border-line py-2.5">
      <span className="mt-0.5 text-fg-3">{icon}</span>
      <span className="w-36 shrink-0 text-fg-2">{label}</span>
      <span className="min-w-0 flex-1">{value}</span>
    </div>
  );
  return (
    <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-8 px-4 py-6 sm:px-8">
        <section>
          <h2 className="caps rule-b pb-1.5">{t("docs.overview.title")}</h2>
          {fact(<FileText className="size-4" />, t("docs.overview.pages"), t("docs.pagesCount", { count: site.pages }))}
          {fact(<CircleAlert className="size-4" />, t("docs.overview.stale"), site.stale_pages ? t("docs.staleCount", { count: site.stale_pages }) : <span className="text-ok">{t("docs.overview.allCurrent")}</span>)}
          {fact(
            <Languages className="size-4" />,
            t("docs.overview.languages"),
            <>
              {[site.language, ...site.languages].map(languageLabel).join(", ")}
              {site.languages.length > 0 && <span className="text-fg-3"> · {t(`docs.translator.${site.translator || "none"}`)}</span>}
            </>,
          )}
          {(site.versioned || site.releases.some((r) => !r.working)) &&
            fact(
              <Tag className="size-4" />,
              t("docs.overview.version"),
              (() => {
                const r = releaseOf(site, release);
                return r ? (
                  <span className="flex flex-wrap items-center gap-1.5">
                    {r.name} <ReleaseBadges r={r} versioned={site.versioned} />
                  </span>
                ) : (
                  "–"
                );
              })(),
            )}
          {fact(
            <Globe className="size-4" />,
            t("docs.overview.address"),
            site.published ? (
              <a href={siteHref(site)} target="_blank" rel="noreferrer" className="break-all underline decoration-line-2 underline-offset-3 hover:decoration-fg">
                {siteHref(site)}
              </a>
            ) : (
              <span className="text-fg-3">{t("docs.visibility.draftHint")}</span>
            ),
          )}
          {fact(
            <Bot className="size-4" />,
            t("docs.overview.agent"),
            site.agent ? (
              <>
                {site.agent.name} <span className="text-fg-3">· {site.agent.model || t("team.defaultModel")}</span>
              </>
            ) : (
              "–"
            ),
          )}
        </section>
        <section className="space-y-3">
          <div className="flex items-center gap-2 rule-b pb-1.5">
            <h2 className="caps flex-1">{t("docs.rules.title")}</h2>
            <Button size="sm" onClick={onRules}>
              {t("docs.rules.open")}
            </Button>
          </div>
          {site.spec.trim() ? (
            <div className="relative max-h-72 overflow-hidden">
              <DocMarkdown text={site.spec} />
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-linear-to-t from-bg" />
            </div>
          ) : (
            <p className="text-fg-3">{t("docs.rules.none")}</p>
          )}
        </section>
      </div>
    </div>
  );
}

// --- doc rules ------------------------------------------------------------------------------------------------

/** The doc rules (the user's own word for the site's spec): what goes in, what not, how it is written. The doc
 * agent follows them with every change; the manager writes them with the user. */
function RulesTab({ site }: { site: DocSite }) {
  const { t } = useTranslation();
  const { project, canWrite, agents, openChat } = useShell();
  const refresh = useDocsRefresh(project.id);
  const manager = agents.find((a) => a.is_manager);
  const [text, setText] = useState(site.spec);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (!editing) setText(site.spec);
  }, [site.spec, editing]);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`${base(project.id)}/sites/${site.id}`, { spec: text });
      await refresh();
      setEditing(false);
      setSaved(true);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-5 px-4 py-6 sm:px-8">
        <header className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 rule-b pb-1.5">
            <ScrollText className="size-4" />
            <h2 className="caps flex-1">{t("docs.rules.title")}</h2>
            {saved && !editing && <span className="text-ok">{t("settingsPage.saved")}</span>}
            {canWrite && !editing && (
              <Button size="sm" variant="primary" onClick={() => (setEditing(true), setSaved(false))}>
                {t("common.edit")}
              </Button>
            )}
          </div>
          <p className="text-fg-2">{t("docs.rules.hint", { agent: site.agent?.name ?? t("docs.rules.agent") })}</p>
          {canWrite && manager && (
            <button type="button" onClick={() => openChat(manager.id)} className="inline-flex items-center gap-1.5 text-fg-2 underline decoration-line-2 underline-offset-3 hover:text-fg">
              <MessageSquare className="size-4" /> {t("docs.rules.withManager")}
            </button>
          )}
        </header>
        {editing ? (
          <div className="space-y-3">
            <Textarea autoFocus value={text} onChange={(e) => setText(e.target.value)} rows={20} className="font-mono [field-sizing:content] min-h-80" placeholder={t("docs.rules.placeholder")} aria-label={t("docs.rules.title")} />
            <p className="text-fg-3">{t("docs.rules.length", { count: text.length, max: 20000 })}</p>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" loading={busy} onClick={save} disabled={text === site.spec}>
                {t("common.save")}
              </Button>
              <Button variant="ghost" onClick={() => (setEditing(false), setText(site.spec))}>
                {t("common.cancel")}
              </Button>
              <ErrorText error={error} />
            </div>
          </div>
        ) : site.spec.trim() ? (
          <DocMarkdown text={site.spec} />
        ) : (
          <EmptyState icon={<ScrollText className="size-8" />} title={t("docs.rules.none")}>
            {t("docs.rules.noneHint")}
          </EmptyState>
        )}
      </div>
    </div>
  );
}

// --- files ----------------------------------------------------------------------------------------------------

function FilesTab({ site }: { site: DocSite }) {
  const { t } = useTranslation();
  const { project, canWrite } = useShell();
  const files = useDocFiles(project.id, site.id);
  const refresh = useDocsRefresh(project.id);
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState<number | null>(null);
  const upload = async (list: File[]) => {
    if (!list.length) return;
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      for (const f of list) form.append("files", f);
      await api.post(`${base(project.id)}/sites/${site.id}/files`, form);
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      className="scroll-thin min-h-0 flex-1 overflow-y-auto"
      onDragOver={(e) => canWrite && e.preventDefault()}
      onDrop={(e) => {
        if (!canWrite) return;
        e.preventDefault();
        void upload(Array.from(e.dataTransfer.files));
      }}
    >
      <div className="mx-auto max-w-4xl space-y-4 px-4 py-6 sm:px-8">
        <div className="flex flex-wrap items-center gap-2 rule-b pb-1.5">
          <h2 className="caps flex-1">{t("docs.files.title")}</h2>
          {canWrite && (
            <Button size="sm" variant="primary" icon={<Upload className="size-3.5" />} loading={busy} onClick={() => input.current?.click()}>
              {t("docs.files.upload")}
            </Button>
          )}
          <input ref={input} type="file" multiple hidden onChange={(e) => (void upload(Array.from(e.target.files ?? [])), (e.target.value = ""))} />
        </div>
        <p className="text-fg-2">{t("docs.files.hint")}</p>
        {error && <ErrorText error={error} />}
        {files.isLoading ? (
          <Spinner />
        ) : !files.data?.length ? (
          <p className="py-8 text-center text-fg-3">{t("docs.files.none")}</p>
        ) : (
          <ul className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
            {files.data.map((f) => {
              const image = f.content_type.startsWith("image/");
              const ref = `${image ? "!" : ""}[${f.filename}](doc-file:${f.id})`;
              return (
                <li key={f.id} className="group/file flex flex-col overflow-hidden rounded-sm ring-1 ring-line ring-inset">
                  <a href={`/api/attachments/${f.id}`} target="_blank" rel="noreferrer" className="flex aspect-[4/3] items-center justify-center bg-sunk">
                    {image ? <img src={`/api/attachments/${f.id}`} alt={f.filename} loading="lazy" className="size-full object-cover" /> : <FileText className="size-8 text-fg-3" />}
                  </a>
                  <div className="flex items-center gap-1 px-2 py-1.5">
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium" title={f.filename}>
                        {f.filename}
                      </span>
                      <span className="block text-fg-3">{formatBytes(f.size)}</span>
                    </span>
                    <IconButton
                      label={copied === f.id ? t("common.copied") : t("docs.files.copyRef")}
                      onClick={async () => {
                        await navigator.clipboard?.writeText(ref).catch(() => undefined);
                        setCopied(f.id);
                        window.setTimeout(() => setCopied(null), 1500);
                      }}
                    >
                      <Copy className={clsx("size-4", copied === f.id && "text-ok")} />
                    </IconButton>
                    {canWrite && (
                      <IconButton
                        label={t("common.delete")}
                        onClick={async () => {
                          if (!window.confirm(t("docs.files.deleteConfirm", { name: f.filename }))) return;
                          await api.del(`/attachments/${f.id}`).catch((e: unknown) => setError(errorMessage(e)));
                          await refresh();
                        }}
                      >
                        <Trash2 className="size-4" />
                      </IconButton>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
