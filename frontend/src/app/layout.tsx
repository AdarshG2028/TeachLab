import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TeachLab",
  description: "Research, teaching preparation and assessment workspace for teachers",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="mx-auto flex max-w-[1600px] items-center justify-between px-4 pt-3">
          <a href="/" className="flex items-center gap-2 text-lg font-bold tracking-tight">
            <span className="tile h-8 w-8 text-xs">TL</span> TeachLab
          </a>
          <span className="muted text-xs">Research · Teach · Assess</span>
        </header>
        <main className="mx-auto max-w-[1600px] px-4 py-3">{children}</main>
      </body>
    </html>
  );
}
