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
        <main className="mx-auto max-w-[1600px] px-3 py-2">{children}</main>
      </body>
    </html>
  );
}
