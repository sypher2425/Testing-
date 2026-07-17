"use client";

import { useEffect } from "react";
import type { FrameMeta } from "@/lib/types";

interface Props {
  frames: FrameMeta[];
  index: number;
  imageUrl: (frame: FrameMeta) => string;
  onClose: () => void;
  onNavigate: (index: number) => void;
}

export default function Lightbox({ frames, index, imageUrl, onClose, onNavigate }: Props) {
  const frame = frames[index];

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowRight") onNavigate(Math.min(index + 1, frames.length - 1));
      if (e.key === "ArrowLeft") onNavigate(Math.max(index - 1, 0));
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [index, frames.length, onClose, onNavigate]);

  if (!frame) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4"
      onClick={onClose}
    >
      <button
        className="absolute right-4 top-4 text-2xl text-white/70 hover:text-white"
        onClick={onClose}
        aria-label="Close"
      >
        ✕
      </button>
      <button
        className="absolute left-4 text-3xl text-white/50 hover:text-white disabled:opacity-20"
        onClick={(e) => {
          e.stopPropagation();
          onNavigate(Math.max(index - 1, 0));
        }}
        disabled={index === 0}
        aria-label="Previous"
      >
        ‹
      </button>
      <button
        className="absolute right-4 text-3xl text-white/50 hover:text-white disabled:opacity-20"
        onClick={(e) => {
          e.stopPropagation();
          onNavigate(Math.min(index + 1, frames.length - 1));
        }}
        disabled={index === frames.length - 1}
        aria-label="Next"
      >
        ›
      </button>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={imageUrl(frame)}
        alt={`Frame ${frame.frame}`}
        className="max-h-[85vh] max-w-[90vw] rounded-lg object-contain"
        onClick={(e) => e.stopPropagation()}
      />
      <div className="absolute bottom-6 rounded-full bg-black/60 px-4 py-1.5 text-sm text-white">
        Frame {frame.frame} · t={frame.timestamp.toFixed(3)}s
        {frame.scene_id !== null && frame.scene_id !== undefined ? ` · scene ${frame.scene_id}` : ""}
      </div>
    </div>
  );
}
