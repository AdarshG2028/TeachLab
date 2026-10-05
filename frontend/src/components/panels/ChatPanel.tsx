"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Api, type Message, type Roadmap, type Session } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import { JobProgress } from "@/components/JobProgress";
import { ErrorText } from "@/components/ErrorText";
import { GuidedBar } from "@/components/GuidedBar";
import { PackStatus } from "@/components/PackStatus";

type Citation = { marker: string; title: string; url: string | null; snippet: string; source_id: string };

export function ChatPanel({ workspaceId, sessionId: initial, onActivity }: {
  workspaceId: string; sessionId: string; onActivity?: () => void;
}) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState(initial);
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [roadmap, setRoadmap] = useState<Roadmap | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    if (!sessionId) return;
    const [msgs, sess, rm] = await Promise.all([Api.messages(workspaceId, sessionId), Api.session(workspaceId, sessionId),
      Api.roadmap(workspaceId)]);
    setMessages(msgs);
    setSession(sess);
    setRoadmap(rm);
    setTimeout(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), 50);
  }, [workspaceId, sessionId]);

  useEffect(() => { Api.sessions(workspaceId).then(setSessions).catch(setError); }, [workspaceId]);
  useEffect(() => { load().catch(setError); }, [load]);

  const loadOlder = async () => {
    const older = await Api.messages(workspaceId, sessionId, messages[0]?.seq);
    setMessages((m) => [...older, ...m]);
  };

  const sendText = async (content: string) => {
    if (!content.trim() || jobId) return;
    try {
      const turn = await Api.sendMessage(workspaceId, sessionId, content);
      setMessages((m) => [...m, turn.message]);
      setText("");
      setJobId(turn.job.id);
    } catch (err) { setError(err); }
  };
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    await sendText(text);
  };

  const state = (session?.state ?? {}) as { unresolved_questions?: string[]; current_concept?: string;
    current_phase_id?: string | null };

  const [details, setDetails] = useState(false);

  return (
    <div className="card relative flex h-[calc(100dvh-5.25rem)] min-h-[360px] flex-col !p-0">
      {/* single toolbar row: session, phase progress + actions, details */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-black/5 px-3 py-1.5 text-sm">
        <select className="input !w-auto !py-0.5 !text-xs" value={sessionId} onChange={(e) => setSessionId(e.target.value)}
          aria-label="Session">
          {sessions.map((s) => <option key={s.id} value={s.id}>{s.title} ({s.message_count})</option>)}
        </select>
        <button className="btn-secondary !px-2.5 !py-0.5 !text-xs" onClick={async () => {
          const s = await Api.createSession(workspaceId, `Session ${sessions.length + 1}`);
          setSessions((x) => [s, ...x]);
          setSessionId(s.id);
        }}>+ New</button>
        <GuidedBar roadmap={roadmap} currentPhaseKey={state.current_phase_id ?? null} busy={!!jobId} onSend={sendText} />
        {messages.length >= 50 && <button className="btn-secondary !px-2.5 !py-0.5 !text-xs" onClick={loadOlder}>Older</button>}
        <button className="btn-secondary !px-2.5 !py-0.5 !text-xs" onClick={() => setDetails((d) => !d)} aria-expanded={details}>
          Details
        </button>
      </div>

      {/* conversation: centered reading column */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4">
        <div className="mx-auto max-w-4xl space-y-4 py-4">
          {messages.length === 0 && (
            <p className="muted py-10 text-center text-sm">Ask a question, or press <b>Start</b> above to build your roadmap.</p>
          )}
          {messages.map((m) => {
            const citations = (m.citations ?? []) as Citation[];
            const research = (m.meta as { research?: { performed?: boolean; queries?: string[]; new_sources?: number;
              reused_sources?: number } })?.research;
            const limitations = (m.meta as { evidence_limitations?: string })?.evidence_limitations;
            const guided = (m.meta as { guided?: { phase_index?: number; phase_count?: number; lesson?: unknown;
              pack_job_id?: string } | null })?.guided;
            return (
              <div key={m.id} className={`fade-in ${m.role === "user" ? "bubble-user" : "bubble-assistant"}`}>
                {m.role === "assistant" && (
                  <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">
                    Assistant{guided?.lesson ? ` · lesson · phase ${guided.phase_index} of ${guided.phase_count}` : ""}
                  </div>
                )}
                {m.role === "assistant" ? <Markdown>{m.content}</Markdown> : <p className="whitespace-pre-wrap text-sm">{m.content}</p>}
                {research?.performed && (
                  <p className="mt-1 text-xs text-muted">Researched: {research.queries?.join("; ")} — {research.new_sources} new,{" "}
                    {research.reused_sources} reused sources</p>
                )}
                {guided?.pack_job_id && <PackStatus workspaceId={workspaceId} jobId={guided.pack_job_id} />}
                {limitations && <p className="mt-1 text-xs text-amber-700">Evidence note: {limitations}</p>}
                {citations.length > 0 && (
                  <details className="mt-2 border-t border-black/5 pt-2 text-xs">
                    <summary className="cursor-pointer text-muted">Sources ({citations.length})</summary>
                    <ul className="mt-1 space-y-1">
                      {citations.map((c) => (
                        <li key={c.marker} title={c.snippet}>
                          <b className="tile !h-5 !w-auto !rounded-md px-1.5 text-[10px]">{c.marker}</b>{" "}
                          {c.url ? <a className="text-teal-ink hover:underline" href={c.url} target="_blank" rel="noreferrer">{c.title}</a> : c.title}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            );
          })}
          <div ref={bottom} />
        </div>
      </div>

      {/* composer pinned at the bottom */}
      <div className="border-t border-black/5 px-4 py-3">
        <div className="mx-auto max-w-4xl">
          <JobProgress jobId={jobId} label="Assistant" onDone={() => { setJobId(null); load(); onActivity?.(); }} />
          <ErrorText error={error} />
          <form className="flex items-end gap-2" onSubmit={send}>
            <textarea className="input resize-none" rows={2} value={text} onChange={(e) => setText(e.target.value)}
              placeholder="Ask a question, request research, or say 'next'…  (Enter to send, Shift+Enter for a new line)"
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); } }} />
            <button className="btn-accent !h-[3.25rem]" disabled={!!jobId || !text.trim()}>Send</button>
          </form>
        </div>
      </div>

      {/* session details slide-over (hidden by default) */}
      {details && (
        <aside className="absolute right-3 top-12 z-20 w-80 max-w-[90%] space-y-2 rounded-2xl bg-white p-4 text-sm shadow-2xl ring-1 ring-black/10">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold">Session details</h3>
            <button className="muted" onClick={() => setDetails(false)} aria-label="Close">✕</button>
          </div>
          <p><span className="label">Current concept</span>{state.current_concept ?? "–"}</p>
          <div><span className="label">Open questions</span>
            <ul className="list-disc pl-4 text-xs">{(state.unresolved_questions ?? []).map((q) => <li key={q}>{q}</li>)}</ul>
          </div>
          {session?.rolling_summary && (
            <details open><summary className="cursor-pointer text-xs text-muted">Conversation summary</summary>
              <p className="mt-1 text-xs">{session.rolling_summary}</p></details>
          )}
        </aside>
      )}
    </div>
  );
}
