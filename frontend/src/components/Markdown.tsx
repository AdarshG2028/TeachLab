"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

/** Markdown + GFM tables + KaTeX math preview (same source format the PDF renderer consumes). */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-md text-[15px] leading-7">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
