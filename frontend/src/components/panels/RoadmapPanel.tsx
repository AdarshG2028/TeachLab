"use client";

import { useCallback, useEffect, useState } from "react";
import { Api, type Roadmap, type RoadmapPhase } from "@/lib/api";
import { JobProgress } from "@/components/JobProgress";
import { ErrorText } from "@/components/ErrorText";

const STAGES = ["planned", "researched", "explained", "discussed", "completed"] as const;

type EditItem = { concept_key?: string | null; title: string; description: string; objectives: string[];
  examples: string[]; included: boolean; optional: boolean; estimated_minutes?: number | null };
type EditPhase = { id?: string | null; title: string; description: string; objectives: string[]; included: boolean;
  estimated_minutes?: number | null; items: EditItem[] };

function toEdit(rm: Roadmap): EditPhase[] {
  return rm.phases.map((p) => ({
    id: p.id, title: p.title, description: p.description, objectives: p.objectives, included: p.included,
    estimated_minutes: p.estimated_minutes, items: p.items.map((i) => ({
      concept_key: i.concept_key, title: i.title, description: i.description, objectives: i.objectives,
      examples: i.examples, included: i.included, optional: i.optional, estimated_minutes: i.estimated_minutes })),
  }));
}

export function RoadmapPanel({ workspaceId, sessionId, level, onStartPhase }: {
  workspaceId: string; sessionId: string; level: number; onStartPhase: () => void;
}) {
  const [rm, setRm] = useState<Roadmap | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [edit, setEdit] = useState<EditPhase[] | null>(null);
  const [instructions, setInstructions] = useState("");
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => { Api.roadmap(workspaceId).then(setRm).catch(setError); }, [workspaceId]);
  useEffect(() => load(), [load]);

  const save = async () => {
    if (!edit || !rm) return;
    try {
      setRm(await Api.saveRoadmap(workspaceId, { expected_version: rm.version, phases: edit }));
      setEdit(null);
    } catch (e) { setError(e); }
  };
  const move = (i: number, d: number) => setEdit((p) => {
    if (!p) return p;
    const n = [...p];
    const j = i + d;
    if (j < 0 || j >= n.length) return p;
    [n[i], n[j]] = [n[j], n[i]];
    return n;
  });

  return (
    <div className="space-y-3">
      <div className="card flex flex-wrap items-end gap-2">
        <label className="flex-1"><span className="label">Instructions for (re)generation</span>
          <input className="input" value={instructions} onChange={(e) => setInstructions(e.target.value)}
            placeholder="e.g. organize chronologically, emphasize applications" /></label>
        <button className="btn" disabled={!!jobId} onClick={async () => {
          try { setJobId((await Api.generateRoadmap(workspaceId, instructions)).id); } catch (e) { setError(e); }
        }}>{rm ? "Regenerate (keeps your edits)" : "Generate roadmap"}</button>
        {rm && !edit && <button className="btn-secondary" onClick={() => setEdit(toEdit(rm))}>Edit</button>}
        {level === 2 && !rm && <span className="text-xs text-muted">Optional in advanced mode.</span>}
      </div>
      <JobProgress jobId={jobId} label="Roadmap" onDone={() => { setJobId(null); load(); }} />
      <ErrorText error={error} />

      {rm && !edit && (
        <>
          <p className="text-sm text-muted">v{rm.version} · {rm.organization} · {rm.origin} — {rm.summary}</p>
          {rm.phases.map((p, i) => <PhaseCard key={p.id} p={p} index={i} workspaceId={workspaceId}
            sessionId={sessionId} level={level} onChanged={load} onJob={setJobId} onStartPhase={onStartPhase} />)}
        </>
      )}

      {edit && (
        <div className="space-y-2">
          {edit.map((p, i) => (
            <div key={p.id ?? i} className="card space-y-2">
              <div className="flex gap-2">
                <input className="input font-semibold" value={p.title}
                  onChange={(e) => setEdit(edit.map((x, k) => k === i ? { ...x, title: e.target.value } : x))} />
                <button className="btn-secondary" onClick={() => move(i, -1)}>↑</button>
                <button className="btn-secondary" onClick={() => move(i, 1)}>↓</button>
                <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={p.included}
                  onChange={(e) => setEdit(edit.map((x, k) => k === i ? { ...x, included: e.target.checked } : x))} />include</label>
              </div>
              {p.items.map((it, j) => (
                <div key={j} className="ml-4 flex gap-2">
                  <input className="input" value={it.title} onChange={(e) => setEdit(edit.map((x, k) => k === i ? {
                    ...x, items: x.items.map((y, m) => m === j ? { ...y, title: e.target.value } : y) } : x))} />
                  <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={it.included}
                    onChange={(e) => setEdit(edit.map((x, k) => k === i ? { ...x, items: x.items.map((y, m) =>
                      m === j ? { ...y, included: e.target.checked } : y) } : x))} />include</label>
                </div>
              ))}
              <button className="btn-secondary ml-4" onClick={() => setEdit(edit.map((x, k) => k === i ? { ...x,
                items: [...x.items, { title: "New concept", description: "", objectives: [], examples: [], included: true,
                  optional: false }] } : x))}>+ Add concept</button>
            </div>
          ))}
          <div className="flex gap-2">
            <button className="btn" onClick={save}>Save as new version</button>
            <button className="btn-secondary" onClick={() => setEdit(null)}>Cancel</button>
            <button className="btn-secondary" onClick={() => setEdit([...edit, { title: "New phase", description: "",
              objectives: [], included: true, items: [] }])}>+ Add phase</button>
          </div>
        </div>
      )}
    </div>
  );
}

function PhaseCard({ p, index, workspaceId, sessionId, level, onChanged, onJob, onStartPhase }: {
  p: RoadmapPhase; index: number; workspaceId: string; sessionId: string; level: number;
  onChanged: () => void; onJob: (id: string) => void; onStartPhase: () => void;
}) {
  const statusColor = { completed: "bg-aqua-soft text-teal-ink", in_progress: "bg-amber-100 text-amber-800",
    skipped: "bg-black/10 text-muted", not_started: "bg-white/60 text-muted" }[p.status] ?? "";
  return (
    <div className={`card ${p.included ? "" : "opacity-50"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">Phase {index + 1}: {p.title} {p.teacher_edited && <span className="badge bg-aqua-soft text-teal-ink">edited</span>}</h3>
        <div className="flex gap-1">
          <span className={`badge ${statusColor}`}>{p.status.replace("_", " ")}</span>
          {sessionId && (
            <button className="btn-secondary" title={level === 0 ? "Recommended in beginner mode" : ""}
              onClick={async () => { await Api.startPhase(workspaceId, p.key, sessionId); onStartPhase(); }}>
              {level === 0 ? "Learn this phase" : "Discuss phase"}</button>
          )}
          <button className="btn-secondary" onClick={async () => { await Api.setPhaseStatus(workspaceId, p.key,
            p.status === "completed" ? "in_progress" : "completed"); onChanged(); }}>
            {p.status === "completed" ? "Reopen" : "Mark complete"}</button>
          <button className="btn-secondary" onClick={async () => { await Api.setPhaseStatus(workspaceId, p.key, "skipped"); onChanged(); }}>Skip</button>
          <button className="btn-secondary" onClick={async () => onJob((await Api.regeneratePhase(workspaceId, p.key)).id)}>Regenerate</button>
        </div>
      </div>
      <p className="text-sm text-muted">{p.description}</p>
      {p.objectives.length > 0 && <ul className="mt-1 list-disc pl-5 text-xs text-muted">{p.objectives.map((o) => <li key={o}>{o}</li>)}</ul>}
      <table className="mt-2 w-full text-xs">
        <thead><tr className="text-left text-muted"><th>Concept</th>{STAGES.map((s) => <th key={s}>{s}</th>)}<th /></tr></thead>
        <tbody>
          {p.items.map((it) => {
            const cov = it.coverage as Record<string, unknown>;
            return (
              <tr key={it.id} className={`border-t border-black/5 ${it.included ? "" : "line-through opacity-50"}`}>
                <td className="py-1">{it.title}{it.optional && " (optional)"}{it.teacher_edited && " ✎"}
                  {cov.self_reported_understanding ? <span className="ml-1 text-amber-700">(self-reported: {String(cov.self_reported_understanding)})</span> : null}</td>
                {STAGES.map((s) => <td key={s}>{cov[s] ? "●" : "○"}</td>)}
                <td><button className="text-teal-ink hover:underline" onClick={async () => {
                  await Api.markCoverage(workspaceId, { concept_key: it.concept_key, concept_name: it.title, stage: "completed",
                    value: !cov.completed });
                  onChanged();
                }}>{cov.completed ? "unmark" : "mark done"}</button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
