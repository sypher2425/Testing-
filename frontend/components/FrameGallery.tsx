"use client";

import { useEffect, useState } from "react";
import { frameUrl, listFrames } from "@/lib/api";
import type { FrameMeta } from "@/lib/types";
import Lightbox from "./Lightbox";

const PAGE_SIZE = 60;

export default function FrameGallery({ jobId }: { jobId: string }) {
  const [frames, setFrames] = useState<FrameMeta[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    listFrames(jobId, 1, PAGE_SIZE)
      .then((res) => {
        if (cancelled) return;
        setFrames(res.frames);
        setTotal(res.total);
        setPage(1);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load frames");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  async function loadMore() {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await listFrames(jobId, page + 1, PAGE_SIZE);
      setFrames((prev) => [...prev, ...res.frames]);
      setTotal(res.total);
      setPage((p) => p + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load more frames");
    } finally {
      setLoadingMore(false);
    }
  }

  if (loading) {
    return (
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
        {Array.from({ length: 12 }).map((_, i) => (
          <div key={i} className="aspect-video animate-pulse rounded-lg bg-surface-border/60" />
        ))}
      </div>
    );
  }

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (frames.length === 0) return <p className="text-sm text-slate-500">No frames extracted.</p>;

  const remaining = total !== null ? total - frames.length : 0;

  return (
    <div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
        {frames.map((frame, i) => (
          <button
            key={frame.image}
            onClick={() => setLightboxIndex(i)}
            className="group relative aspect-video overflow-hidden rounded-lg border border-surface-border bg-black/40 transition-colors hover:border-indigo-400/60"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={frameUrl(jobId, frame.image)}
              alt={`Frame at ${frame.timestamp}s`}
              loading="lazy"
              className="h-full w-full object-cover transition-transform duration-200 group-hover:scale-105"
            />
            <span className="pointer-events-none absolute inset-0 bg-black/0 transition-colors group-hover:bg-black/10" />
            <span className="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-white">
              {frame.timestamp.toFixed(2)}s
            </span>
          </button>
        ))}
      </div>

      {remaining > 0 && (
        <button className="btn-secondary mt-4" onClick={loadMore} disabled={loadingMore}>
          {loadingMore ? "Loading…" : `Load more (${remaining} remaining)`}
        </button>
      )}

      {lightboxIndex !== null && (
        <Lightbox
          frames={frames}
          index={lightboxIndex}
          imageUrl={(f) => frameUrl(jobId, f.image)}
          onClose={() => setLightboxIndex(null)}
          onNavigate={setLightboxIndex}
        />
      )}
    </div>
  );
}
