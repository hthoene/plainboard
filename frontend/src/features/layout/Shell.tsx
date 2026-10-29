import clsx from "clsx";
import { AlertTriangle, CalendarClock, FolderKanban, MessageSquare, NotebookPen, PanelRight, Plug, Settings, Users } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useProjectEvents } from "../../api/events";
import { useAgents, useMeta, useProjects, useSettings } from "../../api/queries";
import { Button, EmptyState, IconButton, Spinner } from "../../components/ui";
import { useIsDesktop, useStored } from "../../lib/hooks";
import { Logo } from "../auth/Login";
import { Board } from "../board/Board";
import { TaskModal } from "../board/TaskModal";
import { ChatPanel } from "../chat/ChatPanel";
import { ChatView } from "../chat/ChatView";
import { NotesView } from "../notes/NotesView";
import { ProjectDialog } from "../projects/ProjectDialog";
import { ProjectSwitcher } from "../projects/ProjectSwitcher";
import { SchedulesView } from "../schedules/SchedulesView";
import { SettingsDialog } from "../settings/SettingsDialog";
import { TeamView } from "../team/TeamView";
import { ToolsView } from "../tools/ToolsView";
import { ShellContext, type ShellApi } from "./shell-context";

export type View = "board" | "chat" | "team" | "notes" | "schedules" | "tools";

const VIEWS: { id: View; icon: (cls: string) => ReactNode }[] = [
  { id: "board", icon: (c) => <FolderKanban className={c} /> },
  { id: "chat", icon: (c) => <MessageSquare className={c} /> },
  { id: "team", icon: (c) => <Users className={c} /> },
  { id: "notes", icon: (c) => <NotebookPen className={c} /> },
  { id: "schedules", icon: (c) => <CalendarClock className={c} /> },
  { id: "tools", icon: (c) => <Plug className={c} /> },
];

export function Shell() {
  const { t } = useTranslation();
  const projects = useProjects();
  const isDesktop = useIsDesktop();
  const [projectId, setProjectId] = useStored<number | null>("plainboard.project", null);
  const [view, setView] = useStored<View>("plainboard.view", "board");
  const [panelOpen, setPanelOpen] = useStored<boolean>("plainboard.chatOpen", true);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [projectDialog, setProjectDialog] = useState<"new" | "edit" | null>(null);

  const project = projects.data?.find((p) => p.id === projectId) ?? projects.data?.[0];
  useEffect(() => {
    if (project && project.id !== projectId) setProjectId(project.id);
  }, [project, projectId, setProjectId]);

  if (projects.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-zinc-200 bg-white px-2 sm:gap-3 sm:px-3 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex items-center gap-2 pl-1 font-semibold tracking-tight">
          <Logo />
          <span className="hidden xl:inline">PlainBoard</span>
        </div>
        {project && (
          <ProjectSwitcher
            projects={projects.data ?? []}
            current={project}
            onSelect={setProjectId}
            onNew={() => setProjectDialog("new")}
            onEdit={() => setProjectDialog("edit")}
          />
        )}
        {project && isDesktop && (
          <nav className="flex items-center gap-0.5">
            {VIEWS.map((v) => (
              <button
                key={v.id}
                onClick={() => setView(v.id)}
                className={clsx(
                  "flex h-8 items-center gap-1.5 rounded-md px-2.5 text-sm transition-colors",
                  view === v.id
                    ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50"
                    : "text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100",
                )}
              >
                {v.icon("size-4")}
                {t(`nav.${v.id}`)}
              </button>
            ))}
          </nav>
        )}
        <div className="flex-1" />
        <StatusHints onOpenSettings={() => setSettingsOpen(true)} />
        <IconButton label={t("nav.settings")} onClick={() => setSettingsOpen(true)}>
          <Settings className="size-4" />
        </IconButton>
        {project && isDesktop && view !== "chat" && (
          <IconButton
            label={t("nav.togglePanel")}
            onClick={() => setPanelOpen(!panelOpen)}
            className={clsx(panelOpen && "bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50")}
          >
            <PanelRight className="size-4" />
          </IconButton>
        )}
      </header>

      {project ? (
        <ProjectWorkspace key={project.id} projectId={project.id} view={view} setView={setView} panelOpen={panelOpen} setPanelOpen={setPanelOpen} isDesktop={isDesktop} />
      ) : (
        <EmptyState icon={<FolderKanban className="size-10" />} title={t("projects.none")}>
          <p className="mb-4">{t("projects.noneHint")}</p>
          <Button variant="primary" onClick={() => setProjectDialog("new")}>
            {t("projects.create")}
          </Button>
        </EmptyState>
      )}

      {project && !isDesktop && (
        <nav className="flex shrink-0 justify-around border-t border-zinc-200 bg-white pb-[env(safe-area-inset-bottom)] dark:border-zinc-800 dark:bg-zinc-900">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              onClick={() => setView(v.id)}
              className={clsx(
                "flex min-w-0 flex-1 flex-col items-center gap-0.5 py-2 text-[10px] font-medium",
                view === v.id ? "text-indigo-600 dark:text-indigo-400" : "text-zinc-500",
              )}
            >
              {v.icon("size-5")}
              <span className="truncate">{t(`nav.${v.id}`)}</span>
            </button>
          ))}
        </nav>
      )}

      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      {projectDialog && (
        <ProjectDialog
          project={projectDialog === "edit" ? project : undefined}
          onClose={() => setProjectDialog(null)}
          onSaved={(p) => setProjectId(p.id)}
          onDeleted={() => setProjectId(null)}
        />
      )}
    </div>
  );
}

function StatusHints({ onOpenSettings }: { onOpenSettings: () => void }) {
  const { t } = useTranslation();
  const meta = useMeta();
  const settings = useSettings();
  if (meta.data && !meta.data.runner_ok) {
    return (
      <span className="flex items-center gap-1.5 text-xs text-amber-600" title={t("status.runnerDown")}>
        <AlertTriangle className="size-3.5" /> <span className="hidden sm:inline">{t("status.runnerDown")}</span>
      </span>
    );
  }
  const values = settings.data?.values;
  const missing = values && values.claude_auth_mode_effective === "runner_login" && !values.claude_auth_mode;
  if (missing) {
    return (
      <button onClick={onOpenSettings} className="flex items-center gap-1.5 text-xs text-amber-600 hover:underline" title={t("status.noCredentials")}>
        <AlertTriangle className="size-3.5" /> <span className="hidden sm:inline">{t("status.noCredentials")}</span>
      </button>
    );
  }
  return null;
}

const MIN_PANEL = 360;

function ProjectWorkspace({
  projectId,
  view,
  setView,
  panelOpen,
  setPanelOpen,
  isDesktop,
}: {
  projectId: number;
  view: View;
  setView: (v: View) => void;
  panelOpen: boolean;
  setPanelOpen: (v: boolean) => void;
  isDesktop: boolean;
}) {
  const projects = useProjects();
  const project = projects.data!.find((p) => p.id === projectId)!;
  const agents = useAgents(projectId);
  const [chatAgentId, setChatAgentId] = useStored<number | null>(`plainboard.chatAgent.${projectId}`, null);
  const [panelWidth, setPanelWidth] = useStored<number>("plainboard.panelWidth", Math.round(window.innerWidth * 0.42));
  const [openTaskId, setOpenTaskId] = useState<number | null>(null);
  useProjectEvents(projectId);

  const manager = agents.data?.find((a) => a.is_manager);
  const chatAgent = agents.data?.find((a) => a.id === chatAgentId) ?? manager;
  const activeChatId = chatAgent?.id ?? null;

  const agentById = useCallback((id: number | null | undefined) => agents.data?.find((a) => a.id === id), [agents.data]);
  const api: ShellApi = useMemo(
    () => ({
      project,
      agents: agents.data ?? [],
      agentById,
      openChat: (id) => {
        setChatAgentId(id);
        if (isDesktop && view !== "chat") setPanelOpen(true);
        else setView("chat");
      },
      openTask: setOpenTaskId,
    }),
    [project, agents.data, agentById, setChatAgentId, isDesktop, view, setPanelOpen, setView],
  );

  const startResize = (e: ReactPointerEvent) => {
    e.preventDefault();
    const onMove = (ev: PointerEvent) => setPanelWidth(Math.min(Math.max(MIN_PANEL, window.innerWidth - ev.clientX), Math.round(window.innerWidth * 0.75)));
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.cursor = "";
    };
    document.body.style.cursor = "col-resize";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const showPanel = isDesktop && panelOpen && view !== "chat" && activeChatId !== null;
  const width = Math.min(Math.max(MIN_PANEL, panelWidth), Math.round(window.innerWidth * 0.75));

  return (
    <ShellContext.Provider value={api}>
      <div className="flex min-h-0 flex-1">
        <main className="min-w-0 flex-1 overflow-hidden">
          {view === "board" && <Board />}
          {view === "chat" && activeChatId !== null && <ChatView agentId={activeChatId} onSelectAgent={setChatAgentId} />}
          {view === "team" && <TeamView />}
          {view === "notes" && <NotesView />}
          {view === "schedules" && <SchedulesView />}
          {view === "tools" && <ToolsView />}
        </main>
        {showPanel && (
          <>
            <div
              role="separator"
              aria-orientation="vertical"
              onPointerDown={startResize}
              className="group relative w-px shrink-0 cursor-col-resize bg-zinc-200 dark:bg-zinc-800"
            >
              <span className="absolute inset-y-0 -left-1.5 w-3 group-hover:bg-indigo-500/20" />
            </div>
            <aside style={{ width }} className="flex shrink-0 flex-col bg-white dark:bg-zinc-900">
              <ChatPanel agentId={activeChatId!} onSelectAgent={setChatAgentId} onClose={() => setPanelOpen(false)} onExpand={() => setView("chat")} />
            </aside>
          </>
        )}
      </div>
      {openTaskId !== null && <TaskModal taskId={openTaskId} onClose={() => setOpenTaskId(null)} />}
    </ShellContext.Provider>
  );
}
