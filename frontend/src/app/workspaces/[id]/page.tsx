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

  const level = LEVELS[w.familiarity_level]?.label.split(" — ")[0];
  const stats: [string, string | number][] = [["Sources", ov.stats.sources as number], ["Notes", ov.stats.notes as number],
    ["Tests", ov.stats.assessments as number], ["Roadmap", ov.roadmap_version ? `v${ov.roadmap_version}` : "–"]];

  return (
    <div className="flex flex-col gap-3">
      {/* slim header: one row */}
      <header className="card-dark fade-in !rounded-2xl !px-4 !py-3">
        <div className="relative z-10 flex flex-wrap items-center gap-x-4 gap-y-2">
          <a href="/" className="muted text-sm hover:text-white" title="All workspaces">←</a>
          <div className="min-w-0 flex-1">
            <h1 className="gradient-text truncate text-xl font-bold leading-tight">{w.title}</h1>
            <p className="muted truncate text-xs">
              <span className="dot mr-1" />{level} · {w.audience || "audience n/a"} · {w.academic_level || "level n/a"}
            </p>
          </div>
          <div className="hidden items-center gap-1.5 md:flex">
            {stats.map(([k, v]) => (
              <span key={k} className="rounded-full bg-white/5 px-3 py-1 text-xs ring-1 ring-white/10">
                <b className="text-white">{v}</b> <span className="muted">{k}</span>
              </span>
            ))}
          </div>
          <details className="relative">
            <summary className="btn-secondary cursor-pointer list-none">Workflow ▾</summary>
            <ol className="absolute right-0 z-30 mt-2 w-72 space-y-1 rounded-2xl bg-charcoal-2 p-3 text-xs shadow-2xl ring-1 ring-white/10"
              aria-label="suggested workflow">
              {ov.workflow.map((s, i) => (
                <li key={s.key} className={s.status === "done" ? "text-aqua" : s.status === "recommended" ? "text-white" : "text-white/50"}>
                  {s.status === "done" ? "✓" : `${i + 1}.`} {s.label}
                </li>
              ))}
            </ol>
          </details>
          <select className="input !w-auto !py-1.5" aria-label="Mode" title="Mode (changeable, research is kept)"
            value={w.familiarity_level} onChange={async (e) => {
              await Api.updateWorkspace(id, { familiarity_level: Number(e.target.value) as 0 | 1 | 2 });
              refresh();
            }}>
            {LEVELS.map((l) => <option key={l.value} value={l.value}>{l.label.split(" — ")[0]}</option>)}
          </select>
        </div>
      </header>

      <nav className="flex flex-wrap gap-1 self-start rounded-full bg-white/40 p-1 ring-1 ring-white/60 backdrop-blur">
        {TABS.map(([key, label]) => (
          <button key={key} onClick={() => setTab(key)} className={`tab ${tab === key ? "tab-active" : ""}`}>
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
