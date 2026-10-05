"use client";

import { useCallback, useEffect, useState } from "react";
import { Api, type Source, type SourceDetail } from "@/lib/api";
import { JobProgress } from "@/components/JobProgress";
import { ErrorText } from "@/components/ErrorText";

export function SourcesPanel({ workspaceId, onActivity }: { workspaceId: string; onActivity?: () => void }) {
  const [sources, setSources] = useState<Source[]>([]);
  const [detail, setDetail] = useState<SourceDetail | null>(null);
  const [query, setQuery] = useState("");
  const [url, setUrl] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => { Api.sources(workspaceId).then(setSources).catch(setError); }, [workspaceId]);
  useEffect(() => load(), [load]);
  const run = async (p: Promise<{ id: string }>) => {
    setError(null);
    try { setJobId((await p).id); } catch (e) { setError(e); }
  };

  return (
    <div className="grid gap-3 lg:grid-cols-[1fr_1fr]">
      <div className="space-y-3">
        <div className="card space-y-2">
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); run(Api.research(workspaceId, [query])); }}>
            <input className="input" placeholder="Web research query" value={query} onChange={(e) => setQuery(e.target.value)} />
            <button className="btn" disabled={!query.trim() || !!jobId}>Research</button>
          </form>
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); run(Api.addUrl(workspaceId, url)); }}>
            <input className="input" type="url" placeholder="https://… (HTML or PDF)" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button className="btn-secondary" disabled={!url || !!jobId}>Add URL</button>
          </form>
          <label className="btn-secondary cursor-pointer">Upload .pdf/.txt/.md/.html
            <input type="file" className="hidden" accept=".pdf,.txt,.md,.html,.htm"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) run(Api.upload(workspaceId, f)); }} />
          </label>
          <JobProgress jobId={jobId} onDone={() => { setJobId(null); load(); onActivity?.(); }} />
          <ErrorText error={error} />
        </div>
        <div className="card">
          <h3 className="mb-2 font-semibold">Sources ({sources.length})</h3>
          <ul className="divide-y divide-black/5 text-sm">
            {sources.map((s) => (
              <li key={s.id} className="cursor-pointer py-2 hover:bg-white/60" onClick={() => Api.source(workspaceId, s.id).then(setDetail)}>
                <p className="font-medium">{s.title}</p>
                <p className="text-xs text-muted">{s.origin} · {s.status} · retrieved {new Date(s.retrieved_at).toLocaleDateString()}
                  {s.published_at ? ` · published ${s.published_at}` : ""}</p>
              </li>
            ))}
          </ul>
        </div>
      </div>
      <aside className="card max-h-[80vh] overflow-y-auto text-sm">
        {!detail ? <p className="text-muted">Select a source to inspect its stored text and chunks.</p> : (
          <div>
            <h3 className="font-semibold">{detail.title}</h3>
            {detail.url && <a className="text-xs text-teal-ink hover:underline" href={detail.url} target="_blank" rel="noreferrer">{detail.url}</a>}
            <p className="text-xs text-muted">embedding: {detail.embedding_model ?? "not indexed"} · {detail.chunks.length} chunks</p>
            {detail.chunks.map((c) => (
              <div key={c.id} className="mt-2 rounded bg-white/60 p-2 text-xs">
                <b>#{c.idx}</b> {c.heading && <i>({c.heading})</i>} {c.text.slice(0, 600)}
              </div>
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}
