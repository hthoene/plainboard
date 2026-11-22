import clsx from "clsx";
import {
  AlertTriangle,
  AppWindow,
  ArrowUpCircle,
  BarChart3,
  CalendarClock,
  FolderKanban,
  Menu,
  MessageSquare,
  NotebookPen,
  Palette,
  Plug,
  Settings,
  SlidersHorizontal,
  Users,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { useProjectEvents } from "../../api/events";
import {
  useAccounts,
  useAgents,
  useMe,
  useMeta,
  useProjects,
} from "../../api/queries";
import type { Agent, Project } from "../../api/types";
import { Button, EmptyState, IconButton, Spinner } from "../../components/ui";
import { useIsDesktop, useMediaQuery, useStored } from "../../lib/hooks";
import { Logo } from "../auth/Login";
import { Board } from "../board/Board";
import { BrowserView, type BrowserRequest } from "../browser/BrowserView";
import { PreviewsSide, useActivePreviews } from "../browser/Previews";
import { ForYou } from "../board/ForYou";
import {
  MentionNotifications,
  takePendingMention,
} from "../../components/Mentions";
import { TaskModal } from "../board/TaskModal";
import { ChatPanel } from "../chat/ChatPanel";
import { ChatView } from "../chat/ChatView";
import { LookDialog } from "./LookDialog";
import { NotesView } from "../notes/NotesView";
import { ProjectWizard } from "../onboarding/ProjectWizard";
import { ProjectSettings } from "../projects/ProjectSettings";
import { ProjectSwitcher } from "../projects/ProjectSwitcher";
import { SchedulesView } from "../schedules/SchedulesView";
import { ProjectPauseBanner } from "../projects/ProjectPause";
import { PauseBanner } from "../settings/PauseEditor";
import { SettingsDialog } from "../settings/SettingsDialog";
import { StatsView } from "../stats/StatsView";
import { TeamView } from "../team/TeamView";
import { ToolsView } from "../tools/ToolsView";
import { RunningNow } from "./RunningNow";
import { ShellContext, type ShellApi } from "./shell-context";

export type View =
  | "board"
  | "chat"
  | "team"
  | "notes"
  | "schedules"
  | "browser"
  | "tools"
  | "stats"
  | "project";

const VIEWS: { id: View; icon: (cls: string) => ReactNode }[] = [
  { id: "board", icon: (c) => <FolderKanban className={c} /> },
  { id: "chat", icon: (c) => <MessageSquare className={c} /> },
  { id: "team", icon: (c) => <Users className={c} /> },
  // The project's own settings: for its owners. Fixed entry after the core views; on the phone the 4th tab.
  { id: "project", icon: (c) => <SlidersHorizontal className={c} /> },
  { id: "notes", icon: (c) => <NotebookPen className={c} /> },
  { id: "schedules", icon: (c) => <CalendarClock className={c} /> },
  { id: "browser", icon: (c) => <AppWindow className={c} /> },
  { id: "tools", icon: (c) => <Plug className={c} /> },
  { id: "stats", icon: (c) => <BarChart3 className={c} /> },
];
/** The phone's bottom bar holds this many views; the rest sit behind "More". */
const PHONE_TABS = 4;

export function Shell() {
  const { t } = useTranslation();
  const projects = useProjects();
  const [projectId, setProjectId] = useStored<number | null>(
    "plainboard.project",
    null,
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<string>();
  /** The PlainBoard settings, at the section that was open last or at a named one. */
  const openSettings = (section?: string) => {
    setSettingsSection(section);
    setSettingsOpen(true);
  };
  const [newProject, setNewProject] = useState(false);
  const me = useMe();
  const mayCreate = !!me && (me.is_admin || me.may_create_projects);

  const project =
    projects.data?.find((p) => p.id === projectId) ?? projects.data?.[0];
  useEffect(() => {
    if (project && project.id !== projectId) setProjectId(project.id);
  }, [project, projectId, setProjectId]);
  // Nothing to look at yet: whoever may create a project is taken to creating the first one.
  const [offered, setOffered] = useState(false);
  useEffect(() => {
    if (!offered && mayCreate && projects.data?.length === 0) {
      setOffered(true);
      setNewProject(true);
    }
  }, [offered, mayCreate, projects.data]);

  if (projects.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }

  return (
    <>
      {project ? (
        <ProjectWorkspace
          key={project.id}
          projects={projects.data ?? []}
          project={project}
          onSelectProject={setProjectId}
          onNewProject={mayCreate ? () => setNewProject(true) : undefined}
          onProjectDeleted={() => setProjectId(null)}
          onSettings={() => openSettings()}
          onPause={() => openSettings("pause")}
          openSettings={openSettings}
        />
      ) : (
        <div className="flex h-full flex-col pt-[env(safe-area-inset-top)]">
          <header className="flex h-12 shrink-0 items-center gap-2 px-4">
            <Brand />
            <div className="flex-1" />
            <LookButton />
            <IconButton
              label={t("nav.settings")}
              onClick={() => openSettings()}
            >
              <Settings className="size-4" />
            </IconButton>
          </header>
          <PauseBanner onDetails={() => openSettings("pause")} />
          <EmptyState
            icon={<FolderKanban className="size-10" />}
            title={t("projects.none")}
          >
            <p className="mb-4">
              {t(mayCreate ? "projects.noneHint" : "projects.noneSharedHint")}
            </p>
            {mayCreate && (
              <Button variant="primary" onClick={() => setNewProject(true)}>
                {t("projects.create")}
              </Button>
            )}
          </EmptyState>
        </div>
      )}
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        section={settingsSection}
      />
      {newProject && (
        <ProjectWizard
          onClose={() => setNewProject(false)}
          onCreated={(p) => setProjectId(p.id)}
        />
      )}
    </>
  );
}

function Brand({ wide = true }: { wide?: boolean }) {
  return (
    <div className="flex items-center gap-2 font-extrabold">
      <Logo />
      {wide && <span>PlainBoard</span>}
    </div>
  );
}

/** Opens the choice of design and of light or dark. */
function LookButton({ className }: { className?: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <IconButton
        label={t("nav.theme")}
        className={className}
        onClick={() => setOpen(true)}
      >
        <Palette className="size-4" />
      </IconButton>
      <LookDialog open={open} onClose={() => setOpen(false)} />
    </>
  );
}

/** A problem only the user can fix: with its text in the sidebar, as an icon elsewhere. */
function StatusHints({
  onOpenSettings,
  wide,
}: {
  onOpenSettings: () => void;
  wide: boolean;
}) {
  const { t } = useTranslation();
  const meta = useMeta();
  const accounts = useAccounts();
  const admin = !!useMe()?.is_admin;
  const runnerDown = meta.data && !meta.data.runner_ok;
  const noCredentials =
    !runnerDown && accounts.data && !accounts.data.some((a) => a.enabled);
  if (!runnerDown && !noCredentials) return null;
  const text = t(runnerDown ? "status.runnerDown" : "status.noCredentials");
  const body = (
    <>
      <AlertTriangle className="size-4 shrink-0" />
      {wide && <span className="min-w-0">{text}</span>}
    </>
  );
  const cls = clsx(
    "flex shrink-0 items-center gap-2 rounded-sm text-left text-you-fg",
    wide
      ? "mx-2 mb-2 bg-you-bg px-2 py-1.5"
      : "mx-auto mb-1 size-8 justify-center",
  );
  return noCredentials && admin ? (
    <button
      type="button"
      onClick={onOpenSettings}
      className={clsx(cls, "hover:underline")}
      title={text}
    >
      {body}
    </button>
  ) : (
    <div role="status" className={cls} title={text}>
      {body}
    </div>
  );
}

/** A newer PlainBoard exists. Nothing waits for the user here, so it is quiet: a line at the foot of the sidebar. */
function UpdateNotice({ wide }: { wide: boolean }) {
  const { t } = useTranslation();
  const version = useMeta().data?.version;
  if (!version?.behind) return null;
  const how = t("update.how", { count: version.behind });
  const cls = clsx(
    "flex shrink-0 items-center gap-2.5 rounded-sm text-fg-2 hover:bg-sunk hover:text-fg",
    wide ? "mx-2 h-8 px-2" : "mx-auto size-8 justify-center",
  );
  const body = (
    <>
      <ArrowUpCircle className="size-4 shrink-0" />
      {wide && <span className="label-line">{t("update.available")}</span>}
    </>
  );
  return version.url ? (
    <a
      href={version.url}
      target="_blank"
      rel="noreferrer"
      className={cls}
      title={how}
      aria-label={how}
    >
      {body}
    </a>
  ) : (
    <div role="status" className={cls} title={how} aria-label={how}>
      {body}
    </div>
  );
}

const MIN_PANEL = 340;
const NO_AGENTS: Agent[] = [];

function ProjectWorkspace({
  projects,
  project,
  onSelectProject,
  onNewProject,
  onProjectDeleted,
  onSettings,
  onPause,
  openSettings,
}: {
  projects: Project[];
  project: Project;
  onSelectProject: (id: number) => void;
  onNewProject?: () => void;
  onProjectDeleted: () => void;
  onSettings: () => void;
  /** Opens the settings where everything is paused and resumed. */
  onPause: () => void;
  openSettings: (section?: string) => void;
}) {
  const { t } = useTranslation();
  const isDesktop = useIsDesktop();
  const xl = useMediaQuery("(min-width: 1280px)");
  const roomy = useMediaQuery("(min-width: 1440px)");
  const [storedView, setView] = useStored<View>("plainboard.view", "board");
  const owner = project.role === "owner";
  const views = VIEWS.filter((v) => v.id !== "project" || owner);
  const view = views.some((v) => v.id === storedView) ? storedView : "board";
  const [panelOpen, setPanelOpen] = useStored<boolean>(
    "plainboard.chatOpen",
    true,
  );
  const agents = useAgents(project.id);
  const [chatAgentId, setChatAgentId] = useStored<number | null>(
    `plainboard.chatAgent.${project.id}`,
    null,
  );
  const [panelWidth, setPanelWidth] = useStored<number>(
    "plainboard.panelWidth",
    380,
  );
  const [openTaskId, setOpenTaskId] = useState<number | null>(null);
  const [browserRequest, setBrowserRequest] = useState<BrowserRequest>({
    url: "",
    n: 0,
  });
  useProjectEvents(project.id);

  const manager = agents.data?.find((a) => a.is_manager);
  const chatAgent = agents.data?.find((a) => a.id === chatAgentId) ?? manager;
  const activeChatId = chatAgent?.id ?? null;

  const agentById = useCallback(
    (id: number | null | undefined) => agents.data?.find((a) => a.id === id),
    [agents.data],
  );
  const previews = useActivePreviews(project.id, agents.data ?? NO_AGENTS);
  const api: ShellApi = useMemo(
    () => ({
      project,
      canWrite: project.role !== "viewer",
      isOwner: project.role === "owner",
      agents: agents.data ?? NO_AGENTS,
      agentById,
      previews,
      openChat: (id) => {
        setChatAgentId(id);
        if (isDesktop && view !== "chat") setPanelOpen(true);
        else setView("chat");
      },
      panel: {
        available: isDesktop && view !== "chat" && activeChatId !== null,
        open: panelOpen,
        toggle: () => setPanelOpen(!panelOpen),
      },
      openTask: setOpenTaskId,
      openSettings,
      openBrowser: (address) => {
        setBrowserRequest((last) => ({ url: address, n: last.n + 1 }));
        setView("browser");
      },
    }),
    [
      project,
      agents.data,
      agentById,
      previews,
      openSettings,
      setChatAgentId,
      isDesktop,
      view,
      activeChatId,
      panelOpen,
      setPanelOpen,
      setView,
    ],
  );
  // A mention of this project chosen in another one: open where it was written.
  useEffect(() => {
    const m = takePendingMention(project.id);
    if (!m) return;
    if (m.kind !== "chat" && m.task_id !== null) api.openTask(m.task_id);
    else if (m.agent_id !== null) api.openChat(m.agent_id);
    else if (m.task_id !== null) api.openTask(m.task_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  /** A view that has something running behind it gets a mark: the browser while previews run. */
  const marked = (id: View) => id === "browser" && previews.length > 0;
  const viewLabel = (id: View) =>
    marked(id)
      ? `${t(`nav.${id}`)} · ${t("previews.count", { count: previews.length })}`
      : t(`nav.${id}`);

  const startResize = (e: ReactPointerEvent) => {
    e.preventDefault();
    const onMove = (ev: PointerEvent) =>
      setPanelWidth(
        Math.min(
          Math.max(MIN_PANEL, window.innerWidth - ev.clientX),
          Math.round(window.innerWidth * 0.75),
        ),
      );
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.cursor = "";
    };
    document.body.style.cursor = "col-resize";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const showPanel =
    isDesktop && panelOpen && view !== "chat" && activeChatId !== null;
  const width = Math.min(
    Math.max(MIN_PANEL, panelWidth),
    Math.round(window.innerWidth * 0.75),
  );
  // The labelled sidebar needs room next to board and chat; otherwise it is an icon rail.
  const wide = xl && (!showPanel || roomy);
  const switcher = (variant: "side" | "rail" | "bar") => (
    <ProjectSwitcher
      variant={variant}
      projects={projects}
      current={project}
      onSelect={onSelectProject}
      onNew={onNewProject}
      onEdit={owner ? () => setView("project") : undefined}
    />
  );

  return (
    <ShellContext.Provider value={api}>
      <MentionNotifications
        projectId={project.id}
        onSelectProject={onSelectProject}
        openTask={api.openTask}
        openChat={api.openChat}
      />
      <div className="flex h-full">
        {isDesktop && (
          <aside
            data-part="side"
            className={clsx(
              "flex shrink-0 flex-col border-r border-line",
              wide ? "w-46" : "w-[50px]",
            )}
          >
            <div
              className={clsx(
                "flex h-12 shrink-0 items-center",
                wide ? "px-3.5" : "justify-center",
              )}
            >
              <Brand wide={wide} />
            </div>
            <div className="shrink-0 px-2 pb-2.5">
              {switcher(wide ? "side" : "rail")}
            </div>
            <div className="shrink-0 px-2 pb-2.5 empty:hidden">
              <ForYou variant={wide ? "side" : "rail"} />
            </div>
            <StatusHints onOpenSettings={onSettings} wide={wide} />
            <nav
              aria-label={t("nav.views")}
              className="flex shrink-0 flex-col gap-px px-2"
            >
              {views.map((v) => (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => setView(v.id)}
                  title={wide ? undefined : viewLabel(v.id)}
                  aria-label={viewLabel(v.id)}
                  aria-current={view === v.id ? "page" : undefined}
                  className={clsx(
                    "relative flex h-8 items-center gap-2.5 rounded-sm transition-colors",
                    wide ? "px-2" : "justify-center",
                    view === v.id
                      ? "plate"
                      : "text-fg-2 hover:bg-sunk hover:text-fg",
                  )}
                >
                  {v.icon("size-4 shrink-0")}
                  {wide && (
                    <span className="label-line">{t(`nav.${v.id}`)}</span>
                  )}
                  {/* The wide sidebar lists the previews themselves, below. */}
                  {!wide && marked(v.id) && (
                    <span className="mk mk-run absolute top-1 right-1 scale-75 bg-current" />
                  )}
                </button>
              ))}
            </nav>
            {wide ? (
              <div className="mt-5 flex min-h-0 flex-1 flex-col gap-5">
                <PreviewsSide />
                <RunningNow variant="table" />
              </div>
            ) : (
              <div className="flex-1" />
            )}
            <UpdateNotice wide={wide} />
            <div
              className={clsx(
                "flex shrink-0 gap-0.5 p-2",
                wide ? "items-center justify-end" : "flex-col items-center",
              )}
            >
              <LookButton />
              <IconButton label={t("nav.settings")} onClick={onSettings}>
                <Settings className="size-4" />
              </IconButton>
            </div>
          </aside>
        )}

        <div className="flex min-w-0 flex-1 flex-col">
          {!isDesktop && (
            <header
              data-part="topbar"
              className="shrink-0 px-2 pt-[env(safe-area-inset-top)]"
            >
              <div className="flex h-13 items-center gap-2">
                {switcher("bar")}
                <div className="flex-1" />
                <StatusHints onOpenSettings={onSettings} wide={false} />
                <UpdateNotice wide={false} />
                <ForYou />
              </div>
            </header>
          )}
          <PauseBanner onDetails={onPause} />
          <ProjectPauseBanner
            project={project}
            isOwner={project.role === "owner"}
          />
          {!isDesktop && view === "board" && <RunningNow variant="strip" />}
          {project.role === "viewer" && (
            <p
              role="status"
              className="shrink-0 border-b border-line px-4 py-1.5 text-fg-2"
            >
              {t("members.readOnly")}
            </p>
          )}

          <div className="flex min-h-0 flex-1">
            <main data-part="main" className="min-w-0 flex-1 overflow-hidden">
              {view === "board" && <Board />}
              {view === "chat" && activeChatId !== null && (
                <ChatView
                  agentId={activeChatId}
                  onSelectAgent={setChatAgentId}
                />
              )}
              {view === "team" && <TeamView />}
              {view === "notes" && <NotesView />}
              {view === "schedules" && <SchedulesView />}
              {view === "browser" && <BrowserView request={browserRequest} />}
              {view === "tools" && <ToolsView />}
              {view === "stats" && <StatsView />}
              {view === "project" && (
                <ProjectSettings
                  onOpenTeam={() => setView("team")}
                  onDeleted={onProjectDeleted}
                />
              )}
            </main>
            {showPanel && (
              <>
                <div
                  role="separator"
                  aria-orientation="vertical"
                  onPointerDown={startResize}
                  className="group relative w-px shrink-0 cursor-col-resize bg-line"
                >
                  <span className="absolute inset-y-0 -left-1.5 z-10 w-3 group-hover:bg-fg/15" />
                </div>
                <aside
                  data-part="panel"
                  style={{ width }}
                  className="flex shrink-0 flex-col"
                >
                  <ChatPanel
                    agentId={activeChatId!}
                    onSelectAgent={setChatAgentId}
                    onClose={() => setPanelOpen(false)}
                    onExpand={() => setView("chat")}
                  />
                </aside>
              </>
            )}
          </div>

          {!isDesktop && (
            <PhoneTabs
              views={views}
              view={view}
              setView={setView}
              onSettings={onSettings}
              previews={previews.length}
            />
          )}
        </div>
      </div>
      {openTaskId !== null && (
        <TaskModal taskId={openTaskId} onClose={() => setOpenTaskId(null)} />
      )}
    </ShellContext.Provider>
  );
}

/** Bottom navigation on phones: the main views, and a sheet with the rest. */
function PhoneTabs({
  views,
  view,
  setView,
  onSettings,
  previews,
}: {
  views: typeof VIEWS;
  view: View;
  setView: (v: View) => void;
  onSettings: () => void;
  previews: number;
}) {
  const { t } = useTranslation();
  const [more, setMore] = useState(false);
  const [lookOpen, setLookOpen] = useState(false);
  const tabs = views.slice(0, PHONE_TABS);
  const rest = views.slice(PHONE_TABS);
  const marked = (id: View) => id === "browser" && previews > 0;
  const tab = (active: boolean) =>
    clsx(
      "relative flex min-h-[58px] min-w-0 flex-1 flex-col items-center justify-center gap-0.5",
      active
        ? "font-semibold text-fg before:absolute before:inset-x-[22%] before:-top-px before:h-0.5 before:bg-fg"
        : "text-fg-3",
    );
  const item = "flex h-12 w-full items-center gap-3 rounded-sm px-3 text-left";
  const go = (v: View) => {
    setView(v);
    setMore(false);
  };

  return (
    <>
      {more && (
        <div
          className="fixed inset-0 z-40 bg-black/40"
          onClick={() => setMore(false)}
        >
          <div
            role="menu"
            className="absolute inset-x-0 bottom-[calc(58px+env(safe-area-inset-bottom))] rule-t bg-bg p-2"
            onClick={(e) => e.stopPropagation()}
          >
            {rest.map((v) => (
              <button
                key={v.id}
                type="button"
                role="menuitem"
                onClick={() => go(v.id)}
                className={clsx(item, view === v.id ? "plate" : "text-fg")}
              >
                {v.icon("size-5 shrink-0")}
                {t(`nav.${v.id}`)}
                {marked(v.id) && (
                  <span className="ml-auto opacity-80">
                    {t("previews.count", { count: previews })}
                  </span>
                )}
              </button>
            ))}
            <div className="my-2 border-t border-line" />
            <button
              type="button"
              role="menuitem"
              className={item}
              onClick={() => {
                setMore(false);
                onSettings();
              }}
            >
              <Settings className="size-5 shrink-0" />
              {t("nav.settings")}
            </button>
            <button
              type="button"
              role="menuitem"
              className={item}
              onClick={() => {
                setMore(false);
                setLookOpen(true);
              }}
            >
              <Palette className="size-5 shrink-0" />
              {t("nav.theme")}
            </button>
          </div>
        </div>
      )}
      <LookDialog open={lookOpen} onClose={() => setLookOpen(false)} />
      <nav
        data-part="tabs"
        aria-label={t("nav.views")}
        className="relative z-40 flex shrink-0 border-t border-line-2 bg-bg pb-[env(safe-area-inset-bottom)]"
      >
        {tabs.map((v) => (
          <button
            key={v.id}
            type="button"
            onClick={() => go(v.id)}
            aria-label={t(`nav.${v.id}`)}
            aria-current={view === v.id && !more ? "page" : undefined}
            className={tab(view === v.id && !more)}
          >
            <span className="relative">
              {v.icon("size-5")}
              {marked(v.id) && (
                <span className="mk mk-run absolute -top-0.5 -right-2 scale-75 bg-current" />
              )}
            </span>
            <span className="max-w-full truncate">
              {v.id === "project" ? t("nav.projectShort") : t(`nav.${v.id}`)}
            </span>
          </button>
        ))}
        <button
          type="button"
          aria-haspopup="menu"
          aria-expanded={more}
          onClick={() => setMore(!more)}
          className={tab(more || rest.some((v) => v.id === view))}
        >
          <span className="relative">
            <Menu className="size-5" />
            {rest.some((v) => marked(v.id)) && (
              <span className="mk mk-run absolute -top-0.5 -right-2 scale-75 bg-current" />
            )}
          </span>
          <span className="max-w-full truncate">{t("nav.more")}</span>
        </button>
      </nav>
    </>
  );
}
