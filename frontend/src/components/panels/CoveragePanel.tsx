"use client";

import { useCallback, useEffect, useState } from "react";
import { Api, type Coverage, type GapReport } from "@/lib/api";
import { JobProgress } from "@/components/JobProgress";
import { ErrorText } from "@/components/ErrorText";

const KIND_STYLE: Record<string, string> = {
  required: "bg-red-100 text-red-800", recommended: "bg-amber-100 text-amber-800",
  optional: "bg-aqua-soft text-teal-ink", info: "bg-white/60 text-muted",
};

export function CoveragePanel({ workspaceId }: { workspaceId: string }) {
  const [gaps, setGaps] = useState<GapReport | null>(null);
  const [coverage, setCoverage] = useState<Coverage[]>([]);
  const [filter, setFilter] = useState("all");
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => {
    Api.gaps(workspaceId).then(setGaps).catch(setError);
    Api.coverage(workspaceId).then(setCoverage).catch(setError);
  }, [workspaceId]);
  useEffect(() => load(), [load]);

  const shown = (gaps?.gaps ?? []).filter((g) => filter === "all" || g.kind === filter);
  return (
    <div className="grid gap-3 lg:grid-cols-[1fr_1fr]">
      <div className="card space-y-2">
        <h3 className="font-semibold">Gaps</h3>
        {gaps && (
          <>
            <p className="text-xs text-muted">{gaps.note}</p>
            <ul className="grid grid-cols-2 gap-1 text-xs">
              {Object.entries(gaps.counts).map(([k, v]) => <li key={k}>{k.replace(/_/g, " ")}: <b>{v}</b></li>)}
            </ul>
            <div className="flex gap-1 text-xs">
              {["all", "required", "recommended", "optional", "info"].map((k) => (
                <button key={k} className={`badge ${filter === k ? "bg-charcoal text-white" : "bg-white/60"}`} onClick={() => setFilter(k)}>{k}</button>
              ))}
            </div>
          </>
        )}
        <JobProgress jobId={jobId} label="Research" onDone={() => { setJobId(null); load(); }} />
        <ErrorText error={error} />
        <ul className="space-y-1 text-sm">
          {shown.map((g, i) => (
            <li key={i} className="rounded border border-black/5 p-2">
              <span className={`badge ${KIND_STYLE[g.kind]}`}>{g.kind}</span>{" "}
              <b>{g.type.replace(/_/g, " ")}</b>: {g.detail}
              {g.suggested_queries.length > 0 && (
                <button className="ml-2 text-xs text-teal-ink hover:underline" disabled={!!jobId} onClick={async () => {
                  try { setJobId((await Api.research(workspaceId, g.suggested_queries.slice(0, 2))).id); } catch (e) { setError(e); }
                }}>research this</button>
              )}
            </li>
          ))}
        </ul>
      </div>
      <div className="card">
        <h3 className="mb-2 font-semibold">Coverage records</h3>
        <p className="mb-2 text-xs text-muted">Stages are recorded events. “completed” is only set when you mark it;
          self-reported understanding is shown separately and is not treated as mastery.</p>
        <table className="w-full text-xs">
          <thead><tr className="text-left text-muted"><th>Concept</th><th>planned</th><th>researched</th><th>explained</th>
            <th>discussed</th><th>completed</th><th>self-report</th></tr></thead>
          <tbody>
            {coverage.map((c) => (
              <tr key={c.concept_key} className="border-t border-black/5">
                <td className="py-1">{c.concept_name}</td>
                {[c.planned_at, c.researched_at, c.explained_at, c.discussed_at, c.completed_at].map((v, i) =>
                  <td key={i}>{v ? "●" : "○"}</td>)}
                <td>{c.self_reported_understanding ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
