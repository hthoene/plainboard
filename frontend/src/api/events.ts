import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect, useSyncExternalStore } from "react";
import { qk } from "./queries";
import type { Agent, Conversation, Message } from "./types";

// --- streaming text store (live deltas per agent, not persisted) -----------------------------

type StreamState = Record<number, string>;
let streams: StreamState = {};
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

const streamStore = {
  append(agentId: number, text: string) {
    streams = { ...streams, [agentId]: (streams[agentId] ?? "") + text };
    emit();
  },
  clear(agentId: number) {
    if (!(agentId in streams)) return;
    const { [agentId]: _, ...rest } = streams;
    streams = rest;
    emit();
  },
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

export function useStreamingText(agentId: number | null): string {
  return useSyncExternalStore(streamStore.subscribe, () => (agentId === null ? "" : (streams[agentId] ?? "")));
}

// --- websocket event handling ----------------------------------------------------------------

interface ServerEvent {
  type: string;
  data: any;
}

function upsertMessage(qc: QueryClient, agentId: number | undefined, conversationId: number, msg: Message) {
  const caches = qc.getQueriesData<Conversation>({ queryKey: ["conversation"] });
  for (const [key, conv] of caches) {
    if (!conv || conv.id !== conversationId) continue;
    const exists = conv.messages.some((m) => m.id === msg.id);
    const messages = exists ? conv.messages.map((m) => (m.id === msg.id ? msg : m)) : [...conv.messages, msg];
    qc.setQueryData<Conversation>(key, { ...conv, messages });
    agentId = agentId ?? conv.agent_id;
  }
  if (agentId !== undefined && msg.role === "assistant" && msg.kind === "text") streamStore.clear(agentId);
}

function handle(qc: QueryClient, pid: number, event: ServerEvent) {
  const { type, data } = event;
  const [entity] = type.split(".");
  switch (entity) {
    case "task":
    case "comment":
    case "attachment":
    case "decision":
      qc.invalidateQueries({ queryKey: qk.tasks(pid) });
      qc.invalidateQueries({ queryKey: ["task"] });
      break;
    case "assignment":
      qc.invalidateQueries({ queryKey: qk.assignments(pid) });
      qc.invalidateQueries({ queryKey: ["task"] });
      break;
    case "agent":
      if (type === "agent.upserted") {
        qc.setQueryData<Agent[]>(qk.agents(pid), (old) => {
          if (!old) return old;
          const exists = old.some((a) => a.id === data.id);
          return exists ? old.map((a) => (a.id === data.id ? data : a)) : [...old, data];
        });
      } else qc.invalidateQueries({ queryKey: qk.agents(pid) });
      break;
    case "run":
      qc.invalidateQueries({ queryKey: qk.runs(pid) });
      if (data.status !== "running" && data.status !== "queued") streamStore.clear(data.agent_id);
      break;
    case "message":
      if (type === "message.delta") streamStore.append(data.agent_id, data.text);
      else upsertMessage(qc, undefined, data.conversation_id, data as Message);
      break;
    case "conversation":
      qc.setQueryData<Conversation>(qk.conversation(data.agent_id), (old) => (old ? { ...old, context_tokens: data.context_tokens } : old));
      qc.setQueryData<Agent[]>(qk.agents(pid), (old) =>
        old?.map((a) => (a.id === data.agent_id ? { ...a, context_tokens: data.context_tokens } : a)),
      );
      break;
    case "note":
      qc.invalidateQueries({ queryKey: qk.notes(pid) });
      break;
    case "schedule":
      qc.invalidateQueries({ queryKey: qk.schedules(pid) });
      break;
    case "mcp":
      qc.invalidateQueries({ queryKey: qk.mcp(pid) });
      break;
    case "repository":
      qc.invalidateQueries({ queryKey: qk.repos(pid) });
      qc.invalidateQueries({ queryKey: qk.agents(pid) });
      break;
    case "project":
      qc.invalidateQueries({ queryKey: qk.projects });
      break;
  }
}

/** Keeps the query cache in sync with server events for one project. Reconnects with backoff. */
export function useProjectEvents(pid: number | null) {
  const qc = useQueryClient();
  useEffect(() => {
    if (pid === null) return;
    let socket: WebSocket | null = null;
    let closed = false;
    let retry = 0;
    let timer: number | undefined;
    let ping: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://${location.host}/api/projects/${pid}/ws`);
      socket.onopen = () => {
        if (retry > 0) qc.invalidateQueries(); // resync everything after a reconnect
        retry = 0;
        ping = window.setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 25_000);
      };
      socket.onmessage = (msg) => handle(qc, pid, JSON.parse(msg.data));
      socket.onclose = () => {
        window.clearInterval(ping);
        if (closed) return;
        retry += 1;
        timer = window.setTimeout(connect, Math.min(10_000, 500 * 2 ** retry));
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      window.clearInterval(ping);
      socket?.close();
    };
  }, [pid, qc]);
}
