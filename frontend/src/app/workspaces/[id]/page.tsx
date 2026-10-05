"use client";

import { use, useCallback, useEffect, useState } from "react";
import { Api, type Overview } from "@/lib/api";
import { ErrorText } from "@/components/ErrorText";
import { LEVELS } from "@/components/WorkspaceForm";
import { ChatPanel } from "@/components/panels/ChatPanel";
import { RoadmapPanel } from "@/components/panels/RoadmapPanel";
import { SourcesPanel } from "@/components/panels/SourcesPanel";
import { CoveragePanel } from "@/components/panels/CoveragePanel";
import { NotesPanel } from "@/components/panels/NotesPanel";
import { AssessmentPanel } from "@/components/panels/AssessmentPanel";
import { ExportsPanel } from "@/components/panels/ExportsPanel";

const TABS = [
  ["chat", "Research chat"], ["roadmap", "Roadmap"], ["sources", "Sources"],
  ["coverage", "Coverage & gaps"], ["notes", "Notes"], ["tests", "Tests"], ["exports", "PDF exports"],
] as const;
type Tab = (typeof TABS)[number][0];

export default function WorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [ov, setOv] = useState<Overview | null>(null);
  const [tab, setTab] = useState<Tab>("chat");
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => {
    Api.overview(id).then(setOv).catch(setError);
  }, [id]);
  useEffect(() => refresh(), [refresh]);
  // Deep links: /workspaces/<id>?tab=roadmap
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    if (requested && TABS.some(([key]) => key === requested)) setTab(requested as Tab);
  }, []);

  if (error) return <ErrorText error={error} />;
  if (!ov) return <p className="text-sm text-slate-500">Loading…</p>;
  const w = ov.workspace;
  const sessionId = ov.latest_session_id ?? "";

  return (
    <div className="space-y-3">
      <div className="card flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{w.title}</h1>
          <p className="text-sm text-slate-500">
            {w.audience || "audience n/a"} · {w.academic_level || "level n/a"}
            {w.time_available_minutes ? ` · ${w.time_available_minutes} min available` : ""}
          </p>
          <p className="mt-1 text-xs text-slate-500">
            {ov.stats.sources as number} sources · {ov.stats.notes as number} notes · {ov.stats.assessments as number} tests
            · roadmap v{ov.roadmap_version ?? "–"}
          </p>
        </div>
        <label className="text-sm">
          <span className="label">Mode (changeable, research is kept)</span>
          <select className="input" value={w.familiarity_level} onChange={async (e) => {
            await Api.updateWorkspace(id, { familiarity_level: Number(e.target.value) as 0 | 1 | 2 });
            refresh();
          }}>
            {LEVELS.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
          </select>
        </label>
      </div>

      <ol className="flex flex-wrap gap-2 text-xs" aria-label="suggested workflow">
        {ov.workflow.map((s, i) => (
          <li key={s.key} className={`badge ${s.status === "done" ? "bg-emerald-100 text-emerald-800"
            : s.status === "recommended" ? "bg-amber-100 text-amber-800" : "bg-slate-100 text-slate-600"}`}>
            {i + 1}. {s.label} — {s.status}
          </li>
        ))}
      </ol>

      <nav className="flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map(([key, label]) => (
          <button key={key} onClick={() => setTab(key)}
            className={`rounded-t px-3 py-1.5 text-sm ${tab === key ? "bg-white font-semibold shadow-sm" : "text-slate-600"}`}>
            {label}
          </button>
        ))}
      </nav>

      <div>
        {tab === "chat" && <ChatPanel workspaceId={id} sessionId={sessionId} onActivity={refresh} />}
        {tab === "roadmap" && <RoadmapPanel workspaceId={id} sessionId={sessionId} level={w.familiarity_level}
          onStartPhase={() => setTab("chat")} />}
        {tab === "sources" && <SourcesPanel workspaceId={id} onActivity={refresh} />}
        {tab === "coverage" && <CoveragePanel workspaceId={id} />}
        {tab === "notes" && <NotesPanel workspaceId={id} />}
        {tab === "tests" && <AssessmentPanel workspaceId={id} defaultLevel={w.academic_level} />}
        {tab === "exports" && <ExportsPanel workspaceId={id} />}
      </div>
    </div>
  );
}
