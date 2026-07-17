"use client";

import { useEffect, useState } from "react";
import { frameUrl, listFrames } from "@/lib/api";
import type { FrameMeta } from "@/lib/types";
import Lightbox from "./Lightbox";

export default function FrameGallery({ jobId }: { jobId: string }) {
  const [frames, setFrames] = useState<FrameMeta[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  const [visibleCount, setVisibleCount] = useState(24);

  useEffect(() => {
    let cancelled = false;
    listFrames(jobId, 1, 500)
      .then((res) => {
        if (!cancelled) setFrames(res.frames);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load frames");
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (frames.length === 0) return <p className="text-sm text-slate-500">No frames extracted.</p>;

  const visible = frames.slice(0, visibleCount);

  return (
    <div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
        {visible.map((frame, i) => (
          <button
            key={frame.image}
            onClick={() => setLightboxIndex(i)}
            className="group relative aspect-video overflow-hidden rounded-lg border border-surface-border bg-black/40"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={frameUrl(jobId, frame.image)}
              alt={`Frame at ${frame.timestamp}s`}
              loading="lazy"
              className="h-full w-full object-cover transition-transform group-hover:scale-105"
            />
            <span className="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-white">
              {frame.timestamp.toFixed(2)}s
            </span>
          </button>
        ))}
      </div>
      {visibleCount < frames.length && (
        <button className="btn-secondary mt-4" onClick={() => setVisibleCount((c) => c + 24)}>
          Load more ({frames.length - visibleCount} remaining)
        </button>
      )}

      {lightboxIndex !== null && (
        <Lightbox
          frames={visible}
          index={lightboxIndex}
          imageUrl={(f) => frameUrl(jobId, f.image)}
          onClose={() => setLightboxIndex(null)}
          onNavigate={setLightboxIndex}
        />
      )}
    </div>
  );
}
