"use client";

import type { Job } from "@/lib/api";
import { useJob } from "@/lib/useJob";

/** Progress bar for a background job; calls onDone when it reaches a terminal state. */
export function JobProgress({ jobId, label, onDone }: { jobId: string | null; label?: string; onDone?: (j: Job) => void }) {
  const job = useJob(jobId, onDone);
  if (!jobId) return null;
  const pct = Number((job?.progress as { percent?: number })?.percent ?? 0);
  const current = (job?.progress as { current?: string })?.current ?? "queued";
  const failed = job?.status === "failed";
  return (
    <div className="my-2 rounded-md border border-black/5 bg-white p-2 text-xs" data-testid="job-progress">
      <div className="flex justify-between">
        <span>{label ?? job?.kind ?? "job"}: <b>{job?.status ?? "queued"}</b> — {current}</span>
        <span>{pct}%</span>
      </div>
      <div className="mt-1 h-1.5 rounded bg-white/60">
        <div className={`h-1.5 rounded ${failed ? "bg-red-500" : "bg-aqua"}`} style={{ width: `${pct}%` }} />
      </div>
      {failed && <p className="mt-1 text-red-600">{String((job?.error as { message?: string })?.message ?? "Failed")}</p>}
    </div>
  );
}
