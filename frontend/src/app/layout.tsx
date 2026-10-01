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
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-2">
            <a href="/" className="font-semibold">TeachLab</a>
            <span className="text-xs text-slate-500">Functional MVP interface — final design pending</span>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-4">{children}</main>
      </body>
    </html>
  );
}
