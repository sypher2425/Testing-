import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Video → AI Dataset",
  description: "Convert a video into a self-describing, AI-ready dataset.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-surface text-slate-100 antialiased">
        <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-4 py-8 sm:px-6 lg:px-8">
          <header className="mb-8 flex items-center justify-between">
            <a href="/" className="flex items-center gap-2 text-lg font-semibold">
              <span className="text-indigo-400">▶</span> Video → AI Dataset
            </a>
            <span className="text-xs text-slate-500">adaptive frame selection · local transcription</span>
          </header>
          <main className="flex-1">{children}</main>
          <footer className="mt-12 text-center text-xs text-slate-600">
            No auth, no cloud, no GPU required — everything runs locally.
          </footer>
        </div>
      </body>
    </html>
  );
}
