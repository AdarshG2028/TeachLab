"use client";

import { useState } from "react";
import { Api, type Job } from "@/lib/api";
import { JobProgress } from "@/components/JobProgress";

type PackResult = { artifacts?: { kind: string; artifact_id: string; title: string }[]; warnings?: string[] };

const LABELS: Record<string, string> = {
  teacher_notes: "Teacher notes", student_notes: "Student notes", question_paper: "Question paper",
  answer_key: "Answer key & marking scheme", student_pack: "Student pack",
};

/** Live progress of a teaching-pack job, then download links for every PDF it produced. */
export function PackStatus({ workspaceId, jobId }: { workspaceId: string; jobId: string }) {
  const [done, setDone] = useState<Job | null>(null);
  const result = (done?.result ?? {}) as PackResult;
  return (
    <div className="mt-2" data-testid="pack-status">
      {!done && <JobProgress jobId={jobId} label="Teaching pack" onDone={setDone} />}
      {done?.status === "succeeded" && (
        <ul className="mt-1 flex flex-wrap gap-2 text-xs">
          {(result.artifacts ?? []).map((a) => (
            <li key={a.artifact_id}>
              <a className="btn-secondary" href={Api.downloadUrl(workspaceId, a.artifact_id)}>⬇ {LABELS[a.kind] ?? a.title}</a>
            </li>
          ))}
        </ul>
      )}
      {(result.warnings ?? []).map((w) => <p key={w} className="mt-1 text-xs text-muted">{w}</p>)}
      {done?.status === "failed" && (
        <p className="mt-1 text-xs text-red-700">{String((done.error as { message?: string }).message ?? "Failed")}</p>
      )}
    </div>
  );
}
