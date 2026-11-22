import clsx from "clsx";
import {
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from "react";
import { useTranslation } from "react-i18next";
import { useMe, useMentions, usePeople, useReadMentions } from "../api/queries";
import type { Mention, Person } from "../api/types";
import { ShellContext } from "../features/layout/shell-context";
import { Avatar } from "./Avatar";

// As the backend reads them (services/mentions.py): not inside words, e-mail addresses or paths.
export const MENTION = /(?<![\w@./-])@([a-z0-9][a-z0-9._-]{1,58})/gi;

type MdNode = {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
};

/** The person an @word names, if any ("@anna." ends a sentence). */
export function personFor(
  word: string,
  people: Map<string, Person>,
): Person | undefined {
  const name = word.toLowerCase();
  return people.get(name) ?? people.get(name.replace(/[._-]+$/, ""));
}

/** Remark plugin: @username of a person with access becomes a mention; code and links stay as they are. */
export function linkMentions(people: Map<string, Person>) {
  const split = (text: string): MdNode[] => {
    const nodes: MdNode[] = [];
    let last = 0;
    for (const match of text.matchAll(MENTION)) {
      const person = personFor(match[1], people);
      if (!person) continue;
      if (match.index > last)
        nodes.push({ type: "text", value: text.slice(last, match.index) });
      nodes.push({
        type: "link",
        url: `#mention-${person.username}`,
        children: [{ type: "text", value: `@${person.username}` }],
      });
      last = match.index + 1 + person.username.length;
    }
    if (last < text.length)
      nodes.push({ type: "text", value: text.slice(last) });
    return nodes;
  };
  const walk = (node: MdNode) => {
    node.children = node.children?.flatMap((child) => {
      if (child.type === "text") return split(child.value ?? "");
      if (child.type !== "link" && child.type !== "linkReference") walk(child);
      return [child];
    });
  };
  return () => walk;
}

/** The people of the project on screen by user name (none outside a project). */
export function usePeopleMap(): Map<string, Person> {
  const projectId = useContext(ShellContext)?.project.id ?? null;
  const people = usePeople(projectId).data;
  return useMemo(
    () => new Map((people ?? []).map((p) => [p.username, p])),
    [people],
  );
}

/** A mention in a text: "@" and the person's name as running text; the signed-in user's own one is a filled pill. */
export function MentionChip({ person }: { person: Person }) {
  const me = useMe();
  const mine = me?.id === person.id;
  return (
    <span
      data-part="mention"
      title={`@${person.username}`}
      className={clsx(
        "rounded-sm px-[0.3em] py-px font-medium [box-decoration-break:clone]",
        mine ? "bg-you text-on-you" : "bg-you-bg text-you-fg",
      )}
    >
      @{person.name}
    </span>
  );
}

/** The @word being typed right before the caret, if any. */
function typedMention(
  text: string,
  caret: number,
): { start: number; query: string } | null {
  const match = text
    .slice(0, caret)
    .match(/(?:^|[^\w@./-])@([a-z0-9._-]{0,58})$/i);
  return match
    ? { start: caret - match[1].length - 1, query: match[1].toLowerCase() }
    : null;
}

/** A small list of people under a text field while an @word is typed. ``onKeyDown`` answers whether it
 * used the key (then the field must not). */
export function useMentionPicker(
  projectId: number,
  text: string,
  setText: (value: string) => void,
  area: RefObject<HTMLTextAreaElement | null>,
) {
  const people = usePeople(projectId).data ?? [];
  const me = useMe();
  const [caret, setCaret] = useState<number | null>(null);
  const [active, setActive] = useState(0);
  const [closedAt, setClosedAt] = useState<number | null>(null);
  const typed = caret === null ? null : typedMention(text, caret);
  const matches = typed
    ? people
        .filter(
          (p) =>
            p.id !== me?.id &&
            (p.username.startsWith(typed.query) ||
              p.name.toLowerCase().startsWith(typed.query)),
        )
        .slice(0, 6)
    : [];
  const open = !!typed && matches.length > 0 && closedAt !== typed.start;
  const index = Math.min(active, Math.max(matches.length - 1, 0));

  const track = () => {
    setCaret(area.current?.selectionStart ?? null);
    setActive(0);
  };
  const pick = (person: Person) => {
    if (!typed || caret === null) return;
    const insert = `@${person.username} `;
    setText(text.slice(0, typed.start) + insert + text.slice(caret));
    const at = typed.start + insert.length;
    setCaret(at);
    requestAnimationFrame(() => {
      area.current?.focus();
      area.current?.setSelectionRange(at, at);
    });
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!open) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setActive(
        (index + (e.key === "ArrowDown" ? 1 : matches.length - 1)) %
          matches.length,
      );
      return true;
    }
    if (
      (e.key === "Enter" || e.key === "Tab") &&
      !e.shiftKey &&
      !e.nativeEvent.isComposing
    ) {
      e.preventDefault();
      pick(matches[index]);
      return true;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      setClosedAt(typed!.start);
      return true;
    }
    return false;
  };
  const menu = open ? (
    <div
      role="listbox"
      data-part="menu"
      className="absolute bottom-full left-0 z-40 mb-1 w-64 max-w-full rounded-sm bg-bg p-1 shadow-lg ring-1 ring-line-2"
    >
      {matches.map((p, i) => (
        <button
          key={p.id}
          type="button"
          role="option"
          aria-selected={i === index}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => pick(p)}
          className={clsx(
            "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left touch:py-2.5",
            i === index ? "bg-sunk" : "hover:bg-sunk",
          )}
        >
          <Avatar name={p.name} person={p} />
          <span className="min-w-0 flex-1 truncate font-medium">{p.name}</span>
          <span className="text-fg-3">@{p.username}</span>
        </button>
      ))}
    </div>
  ) : null;
  return { menu, onKeyDown, track };
}

// --- notifications for the signed-in user's mentions -------------------------------------------------------------

/** Where a mention leads, kept across switching to its project. */
let pending: Mention | null = null;
export function takePendingMention(projectId: number): Mention | null {
  const found = pending?.project_id === projectId ? pending : null;
  if (found) pending = null;
  return found;
}

const NOTIFY_ASKED = "plainboard.notifyAsked";

/** A browser notification for each new unread mention while a PlainBoard tab is open (asked once). */
function useMentionNotifications(
  items: Mention[] | undefined,
  onOpen: (m: Mention) => void,
) {
  const { t } = useTranslation();
  const seen = useRef<Set<number> | null>(null);
  useEffect(() => {
    if (!items) return;
    const unread = items.filter((m) => !m.read_at);
    if (seen.current === null) {
      seen.current = new Set(unread.map((m) => m.id)); // what was there before this tab opened is no news
      return;
    }
    const fresh = unread.filter((m) => !seen.current!.has(m.id));
    fresh.forEach((m) => seen.current!.add(m.id));
    if (
      !fresh.length ||
      typeof Notification === "undefined" ||
      Notification.permission !== "granted"
    )
      return;
    for (const m of fresh) {
      const note = new Notification(
        t("mentions.notifyTitle", { name: m.author || t("mentions.someone") }),
        { body: m.excerpt.replace(/`/g, ""), tag: `mention-${m.id}` },
      );
      note.onclick = () => {
        window.focus();
        onOpen(m);
        note.close();
      };
    }
  }, [items, onOpen, t]);
}

/** Asks for permission to notify, once per browser (needs a user gesture in some browsers). */
export function askToNotify() {
  if (
    typeof Notification === "undefined" ||
    Notification.permission !== "default" ||
    localStorage.getItem(NOTIFY_ASKED)
  )
    return;
  localStorage.setItem(NOTIFY_ASKED, "1");
  void Notification.requestPermission();
}

/** Always mounted: browser notifications for new mentions (when the user wants them); clicking one opens where it was written. */
export function MentionNotifications({
  projectId,
  onSelectProject,
  openTask,
  openChat,
}: {
  projectId: number;
  onSelectProject: (id: number) => void;
  openTask: (taskId: number) => void;
  openChat: (agentId: number) => void;
}) {
  const show = useMe()?.show_mentions !== false;
  const mentions = useMentions();
  const read = useReadMentions();

  const go = (m: Mention) => {
    if (!m.read_at) read.mutate([m.id]);
    if (m.project_id !== projectId) {
      pending = m;
      onSelectProject(m.project_id);
    } else if (m.task_id !== null && m.kind !== "chat") openTask(m.task_id);
    else if (m.agent_id !== null) openChat(m.agent_id);
    else if (m.task_id !== null) openTask(m.task_id);
  };
  const goRef = useRef(go);
  goRef.current = go;
  const [onOpen] = useState(() => (m: Mention) => goRef.current(m));
  useMentionNotifications(show ? mentions.data?.items : undefined, onOpen);

  // Ask for permission at the first click, once per browser.
  useEffect(() => {
    if (!show) return;
    document.addEventListener("pointerdown", askToNotify, { once: true });
    return () => document.removeEventListener("pointerdown", askToNotify);
  }, [show]);
  return null;
}

/** Marks the user's unread mentions read that are shown on screen now (a ticket or a chat that is open). */
export function useReadMentionsOf(where: {
  taskId?: number | null;
  agentId?: number | null;
}) {
  const mentions = useMentions().data?.items;
  const read = useReadMentions();
  const { taskId, agentId } = where;
  const ids = (mentions ?? [])
    .filter(
      (m) =>
        !m.read_at &&
        (taskId != null
          ? m.task_id === taskId && m.kind !== "chat"
          : agentId != null && m.kind === "chat" && m.agent_id === agentId),
    )
    .map((m) => m.id);
  const key = ids.join(",");
  const { mutate } = read;
  useEffect(() => {
    if (key) mutate(key.split(",").map(Number));
  }, [key, mutate]);
}
