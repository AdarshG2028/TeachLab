"use client";

import type { Roadmap } from "@/lib/api";

/** Phase stepper + guided-flow actions shown above the chat. Actions are sent as chat commands. */
export function GuidedBar({ roadmap, currentPhaseKey, busy, onSend }: {
  roadmap: Roadmap | null; currentPhaseKey: string | null; busy: boolean; onSend: (text: string) => void;
}) {
  const phases = (roadmap?.phases ?? []).filter((p) => p.included);
  const idx = phases.findIndex((p) => p.key === currentPhaseKey);
  const finished = phases.length > 0 && phases.every((p) => p.status === "completed" || p.status === "skipped");
  const current = idx >= 0 ? phases[idx] : null;

  return (
    <div className="card-dark mb-3 !rounded-2xl !p-4 text-sm" data-testid="guided-bar">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          {!roadmap && <span>No roadmap yet. Start to research the topic and plan your phases.</span>}
          {roadmap && !current && !finished && <span>Roadmap ready: {phases.length} phases.</span>}
          {current && !finished && (
            <span><b>Phase {idx + 1} of {phases.length}:</b> {current.title}</span>
          )}
          {finished && <span><b>All {phases.length} phases completed.</b></span>}
        </div>
        <div className="flex gap-1">
          {!roadmap && <button className="btn-accent" disabled={busy} onClick={() => onSend("start")}>Start: build my roadmap</button>}
          {roadmap && !finished && (
            <button className="btn-accent" disabled={busy} onClick={() => onSend("next")}>
              {current ? "Next phase →" : "Start phase 1 →"}
            </button>
          )}
          {roadmap && (
            <button className={finished ? "btn-accent" : "btn-secondary"} disabled={busy}
              onClick={() => onSend("generate teaching pack")}>Generate teaching pack</button>
          )}
        </div>
      </div>
      {phases.length > 0 && (
        <ol className="mt-2 flex gap-1" aria-label="phase progress">
          {phases.map((p, i) => (
            <li key={p.key} title={`Phase ${i + 1}: ${p.title} (${p.status.replace("_", " ")})`}
              className={`h-2 flex-1 rounded-full ${p.status === "completed" ? "bg-aqua shadow-[0_0_10px_var(--color-aqua)]"
                : p.key === currentPhaseKey ? "bg-white" : p.status === "skipped" ? "bg-white/20" : "bg-white/10"}`} />
          ))}
        </ol>
      )}
    </div>
  );
}
