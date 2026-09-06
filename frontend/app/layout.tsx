import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Frame / AI — Video Dataset Studio",
  description: "Turn video into structured, AI-ready datasets on your own machine.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-surface text-slate-100 antialiased">
        <div className="ambient-scene" aria-hidden="true">
          <div className="ambient-glow ambient-glow-one" />
          <div className="ambient-glow ambient-glow-two" />
          <div className="technical-grid" />
        </div>

        <div className="site-shell mx-auto flex min-h-screen max-w-[1440px] flex-col px-4 sm:px-7 lg:px-10">
          <header className="app-header flex items-center justify-between py-5">
            <a href="/" className="brand-lockup" aria-label="Frame AI home">
              <span className="brand-mark" aria-hidden="true">
                <span />
                <span />
              </span>
              <span>
                <span className="brand-name">FRAME/AI</span>
                <span className="brand-subtitle">Dataset studio</span>
              </span>
            </a>

            <div className="flex items-center gap-3 sm:gap-6">
              <a href="/#transcripts" className="header-link hidden sm:inline-flex">
                Transcript library
              </a>
              <div className="system-status">
                <span className="status-dot" />
                <span className="status-label">Local pipeline</span>
              </div>
            </div>
          </header>

          <main className="flex-1 py-8 sm:py-12">{children}</main>

          <footer className="app-footer mt-16 flex flex-col gap-3 py-7 text-xs sm:flex-row sm:items-center sm:justify-between">
            <span>Designed for private, local-first video intelligence.</span>
            <span className="footer-meta">No auth · No cloud · No GPU required</span>
          </footer>
        </div>
      </body>
    </html>
  );
}
