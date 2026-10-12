"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

/**
 * remark-math only understands `$…$` / `$$…$$`, but models often write LaTeX as `\(…\)` / `\[…\]`.
 * Convert those delimiters outside code fences and inline code so they render instead of showing raw TeX.
 */
export function normalizeMath(src: string): string {
  return src
    .split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g)
    .map((part, i) =>
      i % 2 === 1
        ? part
        : part
            .replace(/\\\[([\s\S]+?)\\\]/g, (_, m: string) => `\n$$\n${m.trim()}\n$$\n`)
            .replace(/\\\(([\s\S]+?)\\\)/g, (_, m: string) => `$${m.trim()}$`),
    )
    .join("");
}

/** Markdown + GFM tables + KaTeX math preview (same source format the PDF renderer consumes). */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-md text-[15px] leading-7">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
        {normalizeMath(children)}
      </ReactMarkdown>
    </div>
  );
}
