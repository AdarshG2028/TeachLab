"use client";

import { useEffect, useRef, useState } from "react";
import { Api, type Job } from "./api";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

/**
 * Follows a background job via Server-Sent Events, falling back to polling if SSE fails.
 * Calls `onDone` once with the final job.
 */
export function useJob(jobId: string | null, onDone?: (job: Job) => void): Job | null {
  const [job, setJob] = useState<Job | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    let poll: ReturnType<typeof setInterval> | null = null;
    let finished = false;

    const handle = (j: Job) => {
      if (stopped) return;
      setJob(j);
      if (TERMINAL.has(j.status) && !finished) {
        finished = true;
        es?.close();
        if (poll) clearInterval(poll);
        doneRef.current?.(j);
      }
    };

    const startPolling = () => {
      if (poll) return;
      poll = setInterval(() => Api.job(jobId).then(handle).catch(() => undefined), 1500);
    };

    let es: EventSource | null = null;
    if (typeof EventSource !== "undefined") {
      es = new EventSource(Api.jobEventsUrl(jobId));
      es.addEventListener("job", (ev) => handle(JSON.parse((ev as MessageEvent).data)));
      es.onerror = () => {
        es?.close();
        if (!finished) startPolling();
      };
    } else {
      startPolling();
    }
    return () => {
      stopped = true;
      es?.close();
      if (poll) clearInterval(poll);
    };
  }, [jobId]);

  return job;
}
