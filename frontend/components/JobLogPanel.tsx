"use client";

import { useEffect, useRef, useState } from "react";
import { getLogs } from "@/lib/api";
import type { LogLine } from "@/lib/types";

interface Props {
  jobId: string;
  /** Keep polling for new lines every 2s (for an in-progress job). */
  live: boolean;
  /** Collapsed behind a toggle by default — used on the Results view so a
   * completed job's page isn't dominated by log output. */
  collapsedByDefault?: boolean;
}

export default function JobLogPanel({ jobId, live, collapsedByDefault = false }: Props) {
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [expanded, setExpanded] = useState(!collapsedByDefault);
  const sinceIdRef = useRef(0);
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await getLogs(jobId, sinceIdRef.current);
        if (cancelled || res.logs.length === 0) return;
        sinceIdRef.current = res.logs[res.logs.length - 1]!.id;
        setLogs((prev) => [...prev, ...res.logs].slice(-300));
      } catch {
        // transient log fetch failure; next tick (if live) will retry
      }
    };
    poll();
    const id = live ? setInterval(poll, 2000) : null;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [jobId, live]);

  useEffect(() => {
    if (live && expanded) logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs, live, expanded]);

  return (
    <div className="card p-4">
      {collapsedByDefault ? (
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="flex w-full items-center justify-between text-sm font-medium text-slate-300"
        >
          <span>Processing log{logs.length > 0 ? ` (${logs.length} lines)` : ""}</span>
          <span className="text-slate-500">{expanded ? "▲" : "▼"}</span>
        </button>
      ) : (
        <h3 className="mb-2 text-sm font-medium text-slate-300">Log</h3>
      )}

      {expanded && (
        <div className="mt-2 max-h-64 overflow-y-auto rounded-lg bg-black/30 p-3 font-mono text-xs leading-relaxed">
          {logs.length === 0 && <p className="text-slate-600">No log output.</p>}
          {logs.map((line) => (
            <div
              key={line.id}
              className={
                line.level === "error"
                  ? "text-red-400"
                  : line.level === "warning"
                    ? "text-amber-400"
                    : "text-slate-400"
              }
            >
              <span className="text-slate-600">{new Date(line.timestamp).toLocaleTimeString()}</span> {line.message}
            </div>
          ))}
          <div ref={logEndRef} />
        </div>
      )}
    </div>
  );
}
