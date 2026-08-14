"use client";

import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import type { TranscriptJSON } from "@/lib/types";

type VisualTheme = "midnight" | "paper" | "violet";
type VisualFormat = "portrait" | "square" | "story" | "landscape";
type VisualFont = "monster" | "clean";

interface VisualPage {
  id: string;
  text: string;
  highlights: number[];
}

const THEME_OPTIONS: Record<VisualTheme, { label: string; background: string; text: string; muted: string }> = {
  midnight: { label: "Midnight", background: "#080b09", text: "#f5f6f1", muted: "#8b998e" },
  paper: { label: "Paper", background: "#f1eadc", text: "#25211d", muted: "#766d62" },
  violet: { label: "Violet", background: "#181326", text: "#fbf8ff", muted: "#aaa0bd" },
};

const FORMAT_OPTIONS: Record<VisualFormat, { label: string; width: number; height: number }> = {
  portrait: { label: "Portrait 4:5", width: 1080, height: 1350 },
  square: { label: "Square 1:1", width: 1080, height: 1080 },
  story: { label: "Story 9:16", width: 1080, height: 1920 },
  landscape: { label: "Landscape 16:9", width: 1600, height: 900 },
};

const ACCENTS = ["#C8FF62", "#FFD45F", "#FF7CAD", "#75D9FF"];
const LINES_PER_CARD = 18;
const STOP_WORDS = new Set([
  "ABOUT", "AFTER", "AGAIN", "ALSO", "AND", "ARE", "BECAUSE", "BEEN", "BEFORE", "BUT",
  "CAN", "COULD", "DOES", "FOR", "FROM", "HAVE", "INTO", "ITS", "JUST", "LIKE", "MORE",
  "NOT", "NOW", "ONLY", "OUR", "THAN", "THAT", "THE", "THEIR", "THEN", "THESE", "THEY",
  "THIS", "THOSE", "THROUGH", "VERY", "WANT", "WERE", "WHAT", "WHEN", "WHERE", "WHICH",
  "WHILE", "WITH", "WOULD", "YOU", "YOUR",
]);

const FONT_STACKS: Record<VisualFont, string> = {
  monster: 'Impact, Haettenschweiler, "Arial Black", sans-serif',
  clean: '"Arial Black", Arial, Helvetica, sans-serif',
};

function wordsFrom(text: string): string[] {
  return text.trim() ? text.trim().split(/\s+/) : [];
}

function linesFrom(text: string): string[][] {
  return text
    .split(/\r?\n/)
    .map((line) => wordsFrom(line))
    .filter((line) => line.length > 0);
}

function suggestedHighlights(words: string[]): number[] {
  const desired = Math.max(2, Math.min(12, Math.round(words.length * 0.18)));
  return words
    .map((word, index) => {
      const clean = word.replace(/[^\p{L}\p{N}]/gu, "").toLocaleUpperCase();
      const score = STOP_WORDS.has(clean) ? -1 : clean.length + (/[!?]$/.test(word) ? 3 : 0);
      return { index, score };
    })
    .filter((item) => item.score >= 5)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, desired)
    .map((item) => item.index)
    .sort((a, b) => a - b);
}

function transcriptPages(transcript: TranscriptJSON): VisualPage[] {
  const transcriptLines = transcript.segments
    .map((segment) => segment.text.replace(/\s+/g, " ").trim())
    .filter(Boolean);
  if (!transcriptLines.length) {
    return [{ id: "card-1", text: "TYPE YOUR MESSAGE HERE", highlights: [1] }];
  }
  const pages: VisualPage[] = [];
  for (let index = 0; index < transcriptLines.length; index += LINES_PER_CARD) {
    const lines = transcriptLines.slice(index, index + LINES_PER_CARD);
    const text = lines.join("\n");
    pages.push({
      id: `card-${pages.length + 1}`,
      text,
      highlights: suggestedHighlights(wordsFrom(text)),
    });
  }
  return pages;
}

function safeFilename(title: string): string {
  const cleaned = title.replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "").slice(0, 60);
  return cleaned || "transcript-visual";
}

export default function TranscriptVisualEditor({
  transcript,
  title,
  onClose,
}: {
  transcript: TranscriptJSON;
  title: string;
  onClose: () => void;
}) {
  const [pages, setPages] = useState<VisualPage[]>(() => transcriptPages(transcript));
  const [pageIndex, setPageIndex] = useState(0);
  const [dockSlots, setDockSlots] = useState<[string | null, string | null]>(() => [
    pages[0]?.id ?? null,
    pages[1]?.id ?? null,
  ]);
  const [activeSlot, setActiveSlot] = useState<0 | 1>(0);
  const [theme, setTheme] = useState<VisualTheme>("midnight");
  const [format, setFormat] = useState<VisualFormat>("portrait");
  const [font, setFont] = useState<VisualFont>("monster");
  const [accent, setAccent] = useState<string>(ACCENTS[0]!);
  const [fontSize, setFontSize] = useState(54);
  const [exporting, setExporting] = useState(false);

  const currentPage = pages[pageIndex]!;
  const words = useMemo(() => wordsFrom(currentPage.text), [currentPage.text]);
  const lines = useMemo(() => linesFrom(currentPage.text), [currentPage.text]);
  const highlightSet = useMemo(() => new Set(currentPage.highlights), [currentPage.highlights]);
  const selectedTheme = THEME_OPTIONS[theme];
  const selectedFormat = FORMAT_OPTIONS[format];

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKey);
    };
  }, [onClose]);

  function updateCurrent(patch: Partial<VisualPage>) {
    setPages((current) =>
      current.map((page, index) => index === pageIndex ? { ...page, ...patch } : page)
    );
  }

  function updatePage(pageId: string, patch: Partial<VisualPage>) {
    setPages((current) =>
      current.map((page) => page.id === pageId ? { ...page, ...patch } : page)
    );
  }

  function changeText(text: string) {
    const wordCount = wordsFrom(text).length;
    updateCurrent({ text, highlights: currentPage.highlights.filter((index) => index < wordCount) });
  }

  function togglePageHighlight(page: VisualPage, index: number) {
    const next = new Set(page.highlights);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    updatePage(page.id, { highlights: Array.from(next).sort((a, b) => a - b) });
    const nextIndex = pages.findIndex((item) => item.id === page.id);
    if (nextIndex >= 0) setPageIndex(nextIndex);
  }

  function dockPage(pageId: string, slot: 0 | 1) {
    setDockSlots((current) => {
      const next: [string | null, string | null] = [...current];
      const sourceSlot = current.indexOf(pageId);
      if (sourceSlot >= 0 && sourceSlot !== slot) {
        next[sourceSlot] = current[slot];
      }
      next[slot] = pageId;
      return next;
    });
    const nextIndex = pages.findIndex((page) => page.id === pageId);
    if (nextIndex >= 0) setPageIndex(nextIndex);
    setActiveSlot(slot);
  }

  function focusPage(index: number) {
    const boundedIndex = Math.max(0, Math.min(pages.length - 1, index));
    const page = pages[boundedIndex];
    if (!page) return;
    const visibleSlot = dockSlots.indexOf(page.id);
    if (visibleSlot >= 0) {
      setPageIndex(boundedIndex);
      setActiveSlot(visibleSlot as 0 | 1);
      return;
    }
    dockPage(page.id, activeSlot);
  }

  function addCard() {
    const nextPage: VisualPage = {
      id: `card-${Date.now()}`,
      text: "TYPE YOUR NEW MESSAGE HERE",
      highlights: [2],
    };
    setPages((current) => [...current, nextPage]);
    setPageIndex(pages.length);
    setDockSlots([currentPage.id, nextPage.id]);
    setActiveSlot(1);
  }

  function deleteCard() {
    if (pages.length === 1) return;
    const deletedId = currentPage.id;
    const remaining = pages.filter((page) => page.id !== deletedId);
    const otherSlot = activeSlot === 0 ? 1 : 0;
    const otherId = dockSlots[otherSlot];
    const otherPage = remaining.find((page) => page.id === otherId) ?? null;
    const replacement = remaining.find((page) => page.id !== otherId) ?? null;
    setPages(remaining);
    if (replacement) {
      setPageIndex(remaining.findIndex((page) => page.id === replacement.id));
      setDockSlots((current) => {
        const next: [string | null, string | null] = [...current];
        next[activeSlot] = replacement.id;
        if (next[otherSlot] === deletedId) next[otherSlot] = otherPage?.id ?? null;
        return next;
      });
    } else {
      setPageIndex(0);
      setActiveSlot(otherSlot as 0 | 1);
      setDockSlots((current) => {
        const next: [string | null, string | null] = [...current];
        next[activeSlot] = null;
        next[otherSlot] = otherPage?.id ?? remaining[0]?.id ?? null;
        return next;
      });
    }
  }

  async function exportPng() {
    setExporting(true);
    try {
      const { width, height } = selectedFormat;
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Canvas export is not available in this browser.");

      context.fillStyle = selectedTheme.background;
      context.fillRect(0, 0, width, height);

      const padding = Math.round(width * 0.055);
      context.textBaseline = "top";
      const upperLines = lines.map((line) => line.map((word) => word.toLocaleUpperCase()));
      let renderSize = Math.round(fontSize * (width / 1080));
      const top = padding;
      const bottomLimit = height - padding;
      let placements: { word: string; index: number; x: number; y: number; width: number }[] = [];

      while (renderSize >= Math.round(30 * (width / 1080))) {
        context.font = `900 ${renderSize}px ${FONT_STACKS[font]}`;
        const lineHeight = renderSize * 1.04;
        const transcriptLineGap = renderSize * 0.28;
        const space = context.measureText(" ").width;
        let x = padding;
        let y = top;
        let wordIndex = 0;
        placements = [];
        for (const line of upperLines) {
          for (const word of line) {
            const measured = context.measureText(word).width;
            if (x > padding && x + measured > width - padding) {
              x = padding;
              y += lineHeight;
            }
            placements.push({ word, index: wordIndex, x, y, width: measured });
            x += measured + space;
            wordIndex += 1;
          }
          x = padding;
          y += lineHeight + transcriptLineGap;
        }
        const contentBottom = placements.length ? placements[placements.length - 1]!.y + lineHeight : top;
        if (contentBottom <= bottomLimit) break;
        renderSize -= Math.max(2, Math.round(width / 540));
      }

      context.font = `900 ${renderSize}px ${FONT_STACKS[font]}`;
      for (const placement of placements) {
        if (highlightSet.has(placement.index)) {
          const padX = renderSize * 0.12;
          const padY = renderSize * 0.07;
          context.fillStyle = accent;
          context.fillRect(
            placement.x - padX,
            placement.y - padY,
            placement.width + padX * 2,
            renderSize + padY * 2
          );
          context.fillStyle = "#10120f";
        } else {
          context.fillStyle = selectedTheme.text;
        }
        context.fillText(placement.word, placement.x, placement.y);
      }

      const blob = await new Promise<Blob>((resolve, reject) =>
        canvas.toBlob((value) => value ? resolve(value) : reject(new Error("PNG export failed.")), "image/png")
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${safeFilename(title)}-card-${pageIndex + 1}.png`;
      anchor.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  }

  const previewStyle = {
    "--visual-background": selectedTheme.background,
    "--visual-text": selectedTheme.text,
    "--visual-muted": selectedTheme.muted,
    "--visual-accent": accent,
    "--visual-font-ratio": String(fontSize / 10.8),
    "--visual-font": FONT_STACKS[font],
    aspectRatio: `${selectedFormat.width} / ${selectedFormat.height}`,
  } as CSSProperties;

  return (
    <div className="visual-editor-overlay" role="dialog" aria-modal="true" aria-label="Transcript visual editor">
      <header className="visual-editor-header">
        <div>
          <span>Transcript visual editor</span>
          <strong>{title}</strong>
        </div>
        <div className="visual-editor-header-actions">
          <button type="button" className="btn-primary" onClick={exportPng} disabled={exporting || !words.length}>
            {exporting ? "Rendering…" : "Export PNG"}
          </button>
          <button type="button" className="visual-close" onClick={onClose} aria-label="Close visual editor">×</button>
        </div>
      </header>

      <div className="visual-editor-body">
        <aside className="visual-editor-tools">
          <div className="visual-card-nav">
            <button type="button" onClick={() => focusPage(pageIndex - 1)} disabled={pageIndex === 0}>←</button>
            <strong>Card {pageIndex + 1} / {pages.length}</strong>
            <button type="button" onClick={() => focusPage(pageIndex + 1)} disabled={pageIndex === pages.length - 1}>→</button>
          </div>

          <div className="visual-card-actions">
            <button type="button" onClick={addCard}>+ New card</button>
            <button type="button" onClick={deleteCard} disabled={pages.length === 1}>Delete</button>
          </div>

          <div className="visual-control-block">
            <span>Workspace cards</span>
            <div className="visual-card-presets">
              {pages.map((page, index) => (
                <button
                  key={page.id}
                  type="button"
                  draggable
                  onDragStart={(event) => event.dataTransfer.setData("text/plain", page.id)}
                  onClick={() => focusPage(index)}
                  className={page.id === currentPage.id ? "selected" : ""}
                >
                  <b>{index + 1}</b>
                  <span>{page.text.split(/\r?\n/)[0] || "Empty card"}</span>
                  {dockSlots.includes(page.id) && <i>Docked</i>}
                </button>
              ))}
            </div>
          </div>

          <label className="visual-control-block">
            <span>Edit the text</span>
            <textarea value={currentPage.text} onChange={(event) => changeText(event.target.value)} rows={9} />
            <small>{lines.length} transcript lines · {words.length} words. Press Enter to create a new line.</small>
          </label>

          <div className="visual-highlight-actions">
            <button type="button" onClick={() => updateCurrent({ highlights: suggestedHighlights(words) })}>Auto highlight</button>
            <button type="button" onClick={() => updateCurrent({ highlights: [] })}>Clear</button>
          </div>

          <label className="visual-control-block">
            <span>Image format</span>
            <select value={format} onChange={(event) => setFormat(event.target.value as VisualFormat)}>
              {Object.entries(FORMAT_OPTIONS).map(([value, option]) => <option key={value} value={value}>{option.label}</option>)}
            </select>
          </label>

          <label className="visual-control-block">
            <span>Typeface</span>
            <select value={font} onChange={(event) => setFont(event.target.value as VisualFont)}>
              <option value="monster">Monster — heavy display</option>
              <option value="clean">Clean — geometric bold</option>
            </select>
          </label>

          <label className="visual-control-block">
            <span>Text size · {fontSize}px</span>
            <input type="range" min={42} max={84} step={2} value={fontSize} onChange={(event) => setFontSize(Number(event.target.value))} />
          </label>

          <fieldset className="visual-control-block">
            <legend>Background</legend>
            <div className="visual-theme-options">
              {(Object.entries(THEME_OPTIONS) as [VisualTheme, (typeof THEME_OPTIONS)[VisualTheme]][]).map(([value, option]) => (
                <button key={value} type="button" onClick={() => setTheme(value)} className={theme === value ? "selected" : ""}>
                  <i style={{ background: option.background }} />{option.label}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="visual-control-block">
            <legend>Highlight color</legend>
            <div className="visual-accent-options">
              {ACCENTS.map((color) => (
                <button key={color} type="button" onClick={() => setAccent(color)} className={accent === color ? "selected" : ""} style={{ background: color }} aria-label={`Use highlight color ${color}`} />
              ))}
            </div>
          </fieldset>
        </aside>

        <main className="visual-editor-stage">
          <div className="visual-dock-grid">
            {([0, 1] as const).map((slot) => {
              const pageId = dockSlots[slot];
              const page = pages.find((item) => item.id === pageId);
              const pageLines = page ? linesFrom(page.text) : [];
              const pageHighlights = new Set(page?.highlights ?? []);
              const cardNumber = page ? pages.findIndex((item) => item.id === page.id) + 1 : null;
              return (
                <section
                  key={slot}
                  className={`visual-dock-slot ${activeSlot === slot ? "active" : ""} ${page ? "occupied" : "empty"}`}
                  onDragOver={(event) => {
                    event.preventDefault();
                    event.dataTransfer.dropEffect = "move";
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    const droppedId = event.dataTransfer.getData("text/plain");
                    if (pages.some((item) => item.id === droppedId)) dockPage(droppedId, slot);
                  }}
                >
                  {page ? (
                    <div
                      className="visual-card-shell"
                      draggable
                      onDragStart={(event) => {
                        event.dataTransfer.effectAllowed = "move";
                        event.dataTransfer.setData("text/plain", page.id);
                      }}
                      onClick={() => dockPage(page.id, slot)}
                    >
                      <div className="visual-dock-handle">
                        <span aria-hidden="true">⠿</span>
                        <strong>Card {cardNumber}</strong>
                        <small>{activeSlot === slot ? "Editing" : "Reference"}</small>
                      </div>
                      <div className="visual-artboard" style={previewStyle}>
                        <div className="visual-word-field">
                          {pageLines.map((line, lineIndex) => {
                            const wordOffset = pageLines.slice(0, lineIndex).reduce((total, item) => total + item.length, 0);
                            return (
                              <div className="visual-transcript-line" key={`${lineIndex}-${line.join("-")}`}>
                                {line.map((word, index) => {
                                  const wordIndex = wordOffset + index;
                                  return (
                                    <button
                                      key={`${wordIndex}-${word}`}
                                      type="button"
                                      onClick={(event) => {
                                        event.stopPropagation();
                                        setActiveSlot(slot);
                                        togglePageHighlight(page, wordIndex);
                                      }}
                                      className={pageHighlights.has(wordIndex) ? "highlighted" : ""}
                                      title="Click to toggle highlight"
                                    >
                                      {word.toLocaleUpperCase()}
                                    </button>
                                  );
                                })}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  ) : (
                    <button type="button" className="visual-empty-dock" onClick={() => dockPage(currentPage.id, slot)}>
                      <span>+</span>
                      <strong>Empty {slot === 0 ? "left" : "right"} holder</strong>
                      <small>Drag a card here or dock the active card</small>
                    </button>
                  )}
                </section>
              );
            })}
          </div>
        </main>
      </div>
    </div>
  );
}
