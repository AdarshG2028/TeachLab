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
    <div className="min-w-[280px] flex-1" data-testid="guided-bar">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <div className="min-w-0 truncate">
          {!roadmap && <span className="text-muted">No roadmap yet.</span>}
          {roadmap && !current && !finished && <span>Roadmap ready: {phases.length} phases.</span>}
          {current && !finished && (
            <span><b>Phase {idx + 1} of {phases.length}:</b> {current.title}</span>
          )}
          {finished && <span><b>All {phases.length} phases completed.</b></span>}
        </div>
        <div className="flex gap-1.5">
          {!roadmap && <button className="btn-accent !px-3 !py-1 !text-xs" disabled={busy} onClick={() => onSend("start")}>Start: build my roadmap</button>}
          {roadmap && !finished && (
            <button className="btn-accent !px-3 !py-1 !text-xs" disabled={busy} onClick={() => onSend("next")}>
              {current ? "Next phase →" : "Start phase 1 →"}
            </button>
          )}
          {roadmap && (
            <button className={`${finished ? "btn-accent" : "btn-secondary"} !px-3 !py-1 !text-xs`} disabled={busy}
              onClick={() => onSend("generate teaching pack")}>Generate teaching pack</button>
          )}
        </div>
      </div>
      {phases.length > 0 && (
        <ol className="mt-1 flex gap-1" aria-label="phase progress">
          {phases.map((p, i) => (
            <li key={p.key} title={`Phase ${i + 1}: ${p.title} (${p.status.replace("_", " ")})`}
              className={`h-1 flex-1 rounded-full ${p.status === "completed" ? "bg-aqua shadow-[0_0_8px_var(--color-aqua)]"
                : p.key === currentPhaseKey ? "bg-ink/60" : p.status === "skipped" ? "bg-black/10" : "bg-black/10"}`} />
          ))}
        </ol>
      )}
    </div>
  );
}
