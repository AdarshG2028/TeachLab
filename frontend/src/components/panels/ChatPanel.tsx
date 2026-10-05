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

  return (
    <div className="grid gap-3 lg:grid-cols-[1fr_260px]">
      <div className="card flex h-[70vh] flex-col">
        <div className="mb-2 flex items-center gap-2 text-sm">
          <select className="input w-auto" value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
            {sessions.map((s) => <option key={s.id} value={s.id}>{s.title} ({s.message_count})</option>)}
          </select>
          <button className="btn-secondary" onClick={async () => {
            const s = await Api.createSession(workspaceId, `Session ${sessions.length + 1}`);
            setSessions((x) => [s, ...x]);
            setSessionId(s.id);
          }}>New session</button>
          {messages.length >= 50 && <button className="btn-secondary" onClick={loadOlder}>Load older</button>}
        </div>
        <GuidedBar roadmap={roadmap} currentPhaseKey={state.current_phase_id ?? null} busy={!!jobId} onSend={sendText} />
        <div className="flex-1 space-y-3 overflow-y-auto pr-1">
          {messages.map((m) => {
            const citations = (m.citations ?? []) as Citation[];
            const research = (m.meta as { research?: { performed?: boolean; queries?: string[]; new_sources?: number;
              reused_sources?: number } })?.research;
            const limitations = (m.meta as { evidence_limitations?: string })?.evidence_limitations;
            const guided = (m.meta as { guided?: { phase_index?: number; phase_count?: number; lesson?: unknown;
              pack_job_id?: string } | null })?.guided;
            return (
              <div key={m.id} className={m.role === "user" ? "ml-12 rounded-lg bg-slate-100 p-3" : "mr-6 rounded-lg border border-slate-200 p-3"}>
                <div className="mb-1 text-xs font-semibold uppercase text-slate-400">
                  {m.role}{guided?.lesson ? ` · lesson · phase ${guided.phase_index} of ${guided.phase_count}` : ""}
                </div>
                {m.role === "assistant" ? <Markdown>{m.content}</Markdown> : <p className="whitespace-pre-wrap text-sm">{m.content}</p>}
                {research?.performed && (
                  <p className="mt-1 text-xs text-slate-500">Researched: {research.queries?.join("; ")} — {research.new_sources} new,{" "}
                    {research.reused_sources} reused sources</p>
                )}
                {guided?.pack_job_id && <PackStatus workspaceId={workspaceId} jobId={guided.pack_job_id} />}
                {limitations && <p className="mt-1 text-xs text-amber-700">Evidence note: {limitations}</p>}
                {citations.length > 0 && (
                  <ul className="mt-2 space-y-1 border-t border-slate-100 pt-2 text-xs">
                    {citations.map((c) => (
                      <li key={c.marker} title={c.snippet}>
                        <b>[{c.marker}]</b>{" "}
                        {c.url ? <a className="text-blue-700 hover:underline" href={c.url} target="_blank" rel="noreferrer">{c.title}</a> : c.title}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
          <div ref={bottom} />
        </div>
        <JobProgress jobId={jobId} label="Assistant" onDone={() => { setJobId(null); load(); onActivity?.(); }} />
        <ErrorText error={error} />
        <form className="mt-2 flex gap-2" onSubmit={send}>
          <textarea className="input" rows={2} value={text} onChange={(e) => setText(e.target.value)}
            placeholder="Ask a question, request research, or say 'next phase'…"
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); } }} />
          <button className="btn" disabled={!!jobId || !text.trim()}>Send</button>
        </form>
      </div>
      <aside className="card space-y-2 text-sm">
        <h3 className="font-semibold">Session state</h3>
        <p><span className="label">Current concept</span>{state.current_concept ?? "–"}</p>
        <div><span className="label">Open questions</span>
          <ul className="list-disc pl-4 text-xs">{(state.unresolved_questions ?? []).map((q) => <li key={q}>{q}</li>)}</ul>
        </div>
        {session?.rolling_summary && (
          <details><summary className="cursor-pointer text-xs text-slate-500">Conversation summary</summary>
            <p className="mt-1 text-xs">{session.rolling_summary}</p></details>
        )}
      </aside>
    </div>
  );
}
