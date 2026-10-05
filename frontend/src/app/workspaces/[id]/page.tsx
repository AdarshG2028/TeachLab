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
  if (!ov) return <p className="text-sm text-muted">Loading…</p>;
  const w = ov.workspace;
  const sessionId = ov.latest_session_id ?? "";

  return (
    <div className="space-y-4">
      <div className="card-dark fade-in flex flex-wrap items-start justify-between gap-4">
        <div className="relative z-10 min-w-0 flex-1">
          <p className="label"><span className="dot mr-1" />{LEVELS[w.familiarity_level]?.label.split(" — ")[0]}</p>
          <h1 className="gradient-text text-3xl font-bold leading-tight">{w.title}</h1>
          <p className="muted mt-1 text-sm">
            {w.audience || "audience n/a"} · {w.academic_level || "level n/a"}
            {w.time_available_minutes ? ` · ${w.time_available_minutes} min available` : ""}
          </p>
          <div className="mt-4 flex flex-wrap gap-3">
            {[["Sources", ov.stats.sources as number], ["Notes", ov.stats.notes as number],
              ["Tests", ov.stats.assessments as number], ["Roadmap", ov.roadmap_version ? `v${ov.roadmap_version}` : "–"]]
              .map(([k, v]) => (
                <div key={String(k)} className="rounded-2xl bg-white/5 px-4 py-2 ring-1 ring-white/10">
                  <div className="stat text-white">{v}</div>
                  <div className="label !mb-0">{k}</div>
                </div>
              ))}
          </div>
        </div>
        <label className="relative z-10 text-sm text-ink">
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
          <li key={s.key} className={`badge ring-1 ring-black/5 ${s.status === "done" ? "bg-aqua-soft text-teal-ink"
            : s.status === "recommended" ? "bg-white text-ink shadow-sm" : "bg-white/40 text-muted"}`}>
            {s.status === "done" ? "✓" : `${i + 1}.`} {s.label}
          </li>
        ))}
      </ol>

      <nav className="flex flex-wrap gap-1 rounded-full bg-white/40 p-1 ring-1 ring-white/60 backdrop-blur">
        {TABS.map(([key, label]) => (
          <button key={key} onClick={() => setTab(key)}
            className={`tab ${tab === key ? "tab-active" : ""}`}>
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
