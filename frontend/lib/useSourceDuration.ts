"use client";

import { useEffect, useState } from "react";
import type { JobSource } from "./types";

/** Metadata is read locally without uploading or playing the user's file. */
export function useSourceDuration(sources: JobSource[]): { duration: number | null; reading: boolean } {
  const source = sources[0];
  const file = sources.length === 1 && source?.kind === "file" ? source.file : null;
  const [result, setResult] = useState<{ file: File | null; duration: number | null; reading: boolean }>({ file: null, duration: null, reading: false });

  useEffect(() => {
    if (!file) {
      setResult({ file: null, duration: null, reading: false });
      return;
    }
    let finished = false;
    const media = document.createElement("video");
    const url = URL.createObjectURL(file);
    setResult({ file, duration: null, reading: true });
    const finish = (duration: number | null) => {
      if (finished) return;
      finished = true;
      setResult({ file, duration, reading: false });
      URL.revokeObjectURL(url);
    };
    media.preload = "metadata";
    media.onloadedmetadata = () => finish(Number.isFinite(media.duration) && media.duration > 0 ? media.duration : null);
    media.onerror = () => finish(null);
    const timeout = window.setTimeout(() => finish(null), 8000);
    media.src = url;
    return () => {
      finished = true;
      window.clearTimeout(timeout);
      media.onloadedmetadata = null;
      media.onerror = null;
      media.removeAttribute("src");
      media.load();
      URL.revokeObjectURL(url);
    };
  }, [file]);

  return result.file === file ? result : { duration: null, reading: Boolean(file) };
}
