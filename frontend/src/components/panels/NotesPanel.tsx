"use client";

import { useCallback, useEffect, useState } from "react";
import { Api, type Notes } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import { JobProgress } from "@/components/JobProgress";
import { ErrorText } from "@/components/ErrorText";

export function NotesPanel({ workspaceId }: { workspaceId: string }) {
  const [kind, setKind] = useState<"teacher" | "student">("teacher");
  const [depth, setDepth] = useState<"brief" | "standard" | "detailed">("standard");
  const [extra, setExtra] = useState("");
  const [doc, setDoc] = useState<Notes | null>(null);
  const [editing, setEditing] = useState<{ title: string; sections: { id: string; heading: string; markdown: string }[] } | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(() => {
    Api.notesList(workspaceId, kind).then((d) => setDoc(d[0] ?? null)).catch(setError);
  }, [workspaceId, kind]);
  useEffect(() => load(), [load]);

  return (
    <div className="space-y-3">
      <div className="card flex flex-wrap items-end gap-2">
        <label><span className="label">Notes</span>
          <select className="input" value={kind} onChange={(e) => setKind(e.target.value as "teacher" | "student")}>
            <option value="teacher">Teacher notes</option><option value="student">Student notes</option></select></label>
        <label><span className="label">Depth</span>
          <select className="input" value={depth} onChange={(e) => setDepth(e.target.value as typeof depth)}>
            <option>brief</option><option>standard</option><option>detailed</option></select></label>
        <label className="flex-1"><span className="label">Extra instructions</span>
          <input className="input" value={extra} onChange={(e) => setExtra(e.target.value)} /></label>
        <button className="btn" disabled={!!jobId} onClick={async () => {
          try { setJobId((await Api.generateNotes(workspaceId, { kind, depth, extra_instructions: extra })).id); }
          catch (e) { setError(e); }
        }}>Generate {kind} notes</button>
        {doc && !editing && <button className="btn-secondary" onClick={() => setEditing({ title: doc.title,
          sections: doc.sections.map((s) => ({ id: String(s.id), heading: String(s.heading), markdown: String(s.markdown) })) })}>Edit</button>}
      </div>
      <JobProgress jobId={jobId} label="Notes" onDone={() => { setJobId(null); load(); }} />
      <ErrorText error={error} />
      {!doc && !jobId && <p className="text-sm text-muted">No {kind} notes yet.</p>}
      {doc && !editing && (
        <article className="card space-y-4">
          <h2 className="text-lg font-semibold">{doc.title} <span className="text-xs text-muted">v{doc.version}{doc.edited ? " · edited" : ""}</span></h2>
          {doc.sections.map((s) => (
            <section key={String(s.id)}><h3 className="text-base font-semibold">{String(s.heading)}</h3><Markdown>{String(s.markdown)}</Markdown></section>
          ))}
        </article>
      )}
      {editing && doc && (
        <div className="card space-y-3">
          <input className="input font-semibold" value={editing.title} onChange={(e) => setEditing({ ...editing, title: e.target.value })} />
          {editing.sections.map((s, i) => (
            <div key={s.id} className="grid gap-2 lg:grid-cols-2">
              <div>
                <input className="input mb-1" value={s.heading} onChange={(e) => setEditing({ ...editing,
                  sections: editing.sections.map((x, k) => k === i ? { ...x, heading: e.target.value } : x) })} />
                <textarea className="input font-mono text-xs" rows={12} value={s.markdown} onChange={(e) => setEditing({ ...editing,
                  sections: editing.sections.map((x, k) => k === i ? { ...x, markdown: e.target.value } : x) })} />
              </div>
              <div className="max-h-80 overflow-y-auto rounded border border-black/5 p-2"><Markdown>{s.markdown}</Markdown></div>
            </div>
          ))}
          <div className="flex gap-2">
            <button className="btn" onClick={async () => {
              try { setDoc(await Api.saveNotes(workspaceId, doc.id, editing)); setEditing(null); } catch (e) { setError(e); }
            }}>Save new version</button>
            <button className="btn-secondary" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
