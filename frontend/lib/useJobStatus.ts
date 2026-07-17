"use client";

import { useEffect, useRef, useState } from "react";
import { eventsUrl, getJob } from "./api";
import { TERMINAL_STATES, type JobStatusResponse } from "./types";

export function useJobStatus(jobId: string) {
  const [job, setJob] = useState<JobStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;
    let source: EventSource | null = null;

    const stopPolling = () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };

    const startPolling = () => {
      if (pollRef.current) return;
      pollRef.current = setInterval(async () => {
        try {
          const latest = await getJob(jobId);
          if (!cancelled) setJob(latest);
          if (TERMINAL_STATES.includes(latest.status)) stopPolling();
        } catch (err) {
          if (!cancelled) setError(err instanceof Error ? err.message : "Failed to poll job status");
        }
      }, 2000);
    };

    // Fetch once immediately so the UI has data before SSE/poll ticks in.
    getJob(jobId)
      .then((initial) => {
        if (!cancelled) setJob(initial);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load job");
      });

    try {
      source = new EventSource(eventsUrl(jobId));
      source.onmessage = (event) => {
        if (cancelled) return;
        try {
          const parsed = JSON.parse(event.data) as JobStatusResponse;
          setJob(parsed);
          setError(null);
        } catch {
          // ignore malformed frames
        }
      };
      source.onerror = () => {
        source?.close();
        source = null;
        if (!cancelled) startPolling();
      };
    } catch {
      startPolling();
    }

    return () => {
      cancelled = true;
      source?.close();
      stopPolling();
    };
  }, [jobId]);

  return { job, error };
}
