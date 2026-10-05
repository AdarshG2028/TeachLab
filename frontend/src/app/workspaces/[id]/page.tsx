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
    <div className="flex flex-col gap-2">
      {/* one compact row: brand/back, title, tabs, stats, workflow, mode */}
      <header className="card-dark fade-in !rounded-2xl !px-3 !py-2">
        <div className="relative z-10 flex flex-wrap items-center gap-x-3 gap-y-2">
          <a href="/" className="tile h-8 w-8 shrink-0 text-xs" title="All workspaces">TL</a>
          <div className="min-w-0 max-w-[16rem] shrink">
            <h1 className="gradient-text truncate text-base font-bold leading-tight">{w.title}</h1>
            <p className="muted truncate text-[11px]"><span className="dot mr-1" />{level} · {w.academic_level || w.audience || ""}</p>
          </div>
          <nav className="flex min-w-0 flex-1 gap-0.5 overflow-x-auto rounded-full bg-white/5 p-0.5 ring-1 ring-white/10">
            {TABS.map(([key, label]) => (
              <button key={key} onClick={() => setTab(key)}
                className={`whitespace-nowrap rounded-full px-3 py-1 text-[13px] font-medium transition ${
                  tab === key ? "bg-aqua text-[#06323a]" : "text-white/70 hover:bg-white/10 hover:text-white"}`}>
                {label}
              </button>
            ))}
          </nav>
          <div className="hidden items-center gap-1 2xl:flex">
            {stats.map(([k, v]) => (
              <span key={k} className="rounded-full bg-white/5 px-2.5 py-0.5 text-[11px] ring-1 ring-white/10">
                <b className="text-white">{v}</b> <span className="muted">{k}</span>
              </span>
            ))}
          </div>
          <details className="relative">
            <summary className="btn-secondary cursor-pointer list-none !px-3 !py-1 !text-xs">Workflow ▾</summary>
            <ol className="absolute right-0 z-30 mt-2 w-72 space-y-1 rounded-2xl bg-charcoal-2 p-3 text-xs shadow-2xl ring-1 ring-white/10"
              aria-label="suggested workflow">
              <li className="muted mb-1">
                {stats.map(([k, v]) => `${v} ${k.toLowerCase()}`).join(" · ")}
              </li>
              {ov.workflow.map((s, i) => (
                <li key={s.key} className={s.status === "done" ? "text-aqua" : s.status === "recommended" ? "text-white" : "text-white/50"}>
                  {s.status === "done" ? "✓" : `${i + 1}.`} {s.label}
                </li>
              ))}
            </ol>
          </details>
          <select className="input !w-auto !py-1 !text-xs" aria-label="Mode" title="Mode (changeable, research is kept)"
            value={w.familiarity_level} onChange={async (e) => {
              await Api.updateWorkspace(id, { familiarity_level: Number(e.target.value) as 0 | 1 | 2 });
              refresh();
            }}>
            {LEVELS.map((l) => <option key={l.value} value={l.value}>{l.label.split(" — ")[0]}</option>)}
          </select>
        </div>
      </header>

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
