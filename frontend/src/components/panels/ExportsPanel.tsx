"use client";

import { useCallback, useEffect, useState } from "react";
import { Api, type Artifact, type AssessmentSummary, type ExportRequest } from "@/lib/api";
import { JobProgress } from "@/components/JobProgress";
import { ErrorText } from "@/components/ErrorText";

const KINDS: { kind: ExportRequest["kind"]; label: string; needsTest: boolean }[] = [
  { kind: "student_notes", label: "Student notes", needsTest: false },
  { kind: "teacher_notes", label: "Teacher notes", needsTest: false },
  { kind: "question_paper", label: "Question paper (no answers)", needsTest: true },
  { kind: "answer_key", label: "Answer key & marking scheme", needsTest: true },
  { kind: "student_pack", label: "Student pack (notes + practice test)", needsTest: false },
];

export function ExportsPanel({ workspaceId }: { workspaceId: string }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [tests, setTests] = useState<AssessmentSummary[]>([]);
  const [meta, setMeta] = useState({ teacher_name: "", institution: "", class_name: "", date: "", assessment_id: "" });
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => {
    Api.artifacts(workspaceId).then(setArtifacts).catch(setError);
    Api.assessments(workspaceId).then((t) => setTests(t.filter((x) => x.status === "ready"))).catch(setError);
  }, [workspaceId]);
  useEffect(() => load(), [load]);

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <div className="card space-y-2 text-sm">
        <h3 className="font-semibold">Create PDF</h3>
        <div className="grid grid-cols-2 gap-2">
          {(["teacher_name", "institution", "class_name", "date"] as const).map((k) => (
            <label key={k}><span className="label">{k.replace("_", " ")}</span>
              <input className="input" value={meta[k]} onChange={(e) => setMeta({ ...meta, [k]: e.target.value })} /></label>
          ))}
        </div>
        <label className="block"><span className="label">Test (for papers / keys / pack)</span>
          <select className="input" value={meta.assessment_id} onChange={(e) => setMeta({ ...meta, assessment_id: e.target.value })}>
            <option value="">—</option>{tests.map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}</select></label>
        <div className="flex flex-wrap gap-2">
          {KINDS.map((k) => (
            <button key={k.kind} className="btn-secondary" disabled={!!jobId || (k.needsTest && !meta.assessment_id)}
              onClick={async () => {
                setError(null);
                try {
                  setJobId((await Api.exportPdf(workspaceId, { kind: k.kind, teacher_name: meta.teacher_name,
                    institution: meta.institution, class_name: meta.class_name, date: meta.date,
                    assessment_id: meta.assessment_id || null })).id);
                } catch (e) { setError(e); }
              }}>{k.label}</button>
          ))}
        </div>
        <button className="btn" disabled={!!jobId} onClick={async () => {
          setError(null);
          try {
            setJobId((await Api.teachingPack(workspaceId, { teacher_name: meta.teacher_name,
              institution: meta.institution, class_name: meta.class_name })).id);
          } catch (e) { setError(e); }
        }}>Generate full teaching pack (notes + test + answer key)</button>
        <JobProgress jobId={jobId} label="PDF" onDone={(j) => { setJobId(null); load(); if (j.status === "failed") setError(new Error(String((j.error as { message?: string }).message))); }} />
        <ErrorText error={error} />
      </div>
      <div className="card text-sm">
        <h3 className="mb-2 font-semibold">Downloads</h3>
        <ul className="divide-y divide-slate-100">
          {artifacts.map((a) => (
            <li key={a.id} className="flex items-center justify-between py-1">
              <span>{a.title} <span className="text-xs text-slate-500">· {a.kind} · {(a.size_bytes / 1024).toFixed(0)} KB</span></span>
              <a className="btn-secondary" href={Api.downloadUrl(workspaceId, a.id)}>Download</a>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
