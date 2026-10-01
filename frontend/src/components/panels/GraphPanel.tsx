"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type cytoscape from "cytoscape";
import { Api, type EntityDetail, type GraphData } from "@/lib/api";
import { ErrorText } from "@/components/ErrorText";

const CATEGORY_COLORS: Record<string, string> = {
  Concept: "#2563eb", Person: "#db2777", Event: "#ea580c", Place: "#16a34a", Organization: "#7c3aed",
  Technology: "#0891b2", Method: "#ca8a04", Other: "#64748b",
};

export function GraphPanel({ workspaceId }: { workspaceId: string }) {
  const box = useRef<HTMLDivElement>(null);
  const cy = useRef<cytoscape.Core | null>(null);
  const [data, setData] = useState<GraphData | null>(null);
  const [detail, setDetail] = useState<EntityDetail | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState<unknown>(null);

  const select = useCallback(async (id: string) => {
    try { setDetail(await Api.entity(workspaceId, id)); } catch (e) { setError(e); }
  }, [workspaceId]);

  useEffect(() => { Api.graphOverview(workspaceId).then(setData).catch(setError); }, [workspaceId]);

  useEffect(() => {
    if (!data || !box.current) return;
    let disposed = false;
    import("cytoscape").then(({ default: cytoscapeLib }) => {
      if (disposed || !box.current) return;
      cy.current?.destroy();
      cy.current = cytoscapeLib({
        container: box.current,
        elements: [
          ...data.nodes.map((n) => ({ data: { id: n.id, label: n.name, color: CATEGORY_COLORS[n.category] ?? "#64748b" } })),
          ...data.edges.map((e) => ({ data: { id: e.id, source: e.source, target: e.target,
            label: e.type.toLowerCase().replace(/_/g, " "), contested: e.status === "contested" ? 1 : 0 } })),
        ],
        style: [
          { selector: "node", style: { label: "data(label)", "background-color": "data(color)", "font-size": 9, width: 14, height: 14 } },
          { selector: "edge", style: { label: "data(label)", "font-size": 7, "curve-style": "bezier", "target-arrow-shape": "triangle",
            width: 1, "line-color": "#94a3b8", "target-arrow-color": "#94a3b8", color: "#475569" } },
          { selector: "edge[contested = 1]", style: { "line-color": "#dc2626", "line-style": "dashed" } },
        ],
        layout: { name: "cose", animate: false },
      });
      cy.current.on("tap", "node", (evt) => select(evt.target.id()));
    });
    return () => { disposed = true; cy.current?.destroy(); cy.current = null; };
  }, [data, select]);

  return (
    <div className="grid gap-3 lg:grid-cols-[1fr_380px]">
      <div className="card">
        <form className="mb-2 flex gap-2" onSubmit={async (e) => {
          e.preventDefault();
          const hits = await Api.graphSearch(workspaceId, q);
          if (hits[0]) { setData(await Api.neighborhood(workspaceId, hits[0].id, 1)); select(hits[0].id); }
        }}>
          <input className="input" placeholder="Find a concept…" value={q} onChange={(e) => setQ(e.target.value)} />
          <button className="btn-secondary">Focus</button>
          <button type="button" className="btn-secondary" onClick={() => Api.graphOverview(workspaceId).then(setData)}>All</button>
        </form>
        <ErrorText error={error} />
        {data && data.nodes.length === 0 && <p className="text-sm text-slate-500">No knowledge yet — research the topic first.</p>}
        <div ref={box} className="h-[60vh] w-full" data-testid="graph-canvas" />
        <div className="flex flex-wrap gap-2 text-xs">
          {Object.entries(CATEGORY_COLORS).map(([k, c]) => <span key={k}><span style={{ color: c }}>●</span> {k}</span>)}
          <span className="text-red-600">– – contested</span>
        </div>
      </div>
      <aside className="card max-h-[75vh] overflow-y-auto text-sm">
        {!detail && <p className="text-slate-500">Click a concept to see why it was added (claims, evidence, sources).</p>}
        {detail && (
          <div className="space-y-2">
            <h3 className="text-base font-semibold">{detail.name} <span className="badge bg-slate-100">{detail.category}</span></h3>
            {detail.aliases?.length > 0 && <p className="text-xs text-slate-500">Also: {detail.aliases.join(", ")}</p>}
            <p>{detail.description}</p>
            {detail.claims.map((c) => (
              <div key={c.id} className="rounded border border-slate-200 p-2">
                <p className="font-medium">{c.text}</p>
                <p className="text-xs text-slate-500">status: <b>{c.status}</b> · {c.family} · confidence {c.confidence?.toFixed(2)}
                  {" "}(model estimate, not a probability) · from “{c.raw_predicates?.join("”, “")}”</p>
                {c.perspectives?.length > 0 && <p className="text-xs text-amber-700">Perspective: {c.perspectives.join("; ")}</p>}
                {c.contradicts.length > 0 && <p className="text-xs text-red-700">Conflicts with: {c.contradicts.map((x) => x.text).join("; ")}</p>}
                <ul className="mt-1 space-y-1 text-xs">
                  {c.evidence.map((ev) => (
                    <li key={ev.evidence_id}>
                      {ev.quote ? <q className="italic">{ev.quote}</q> : <span className="text-slate-500">(no verified quote)</span>}
                      {" — "}
                      {ev.source_url ? <a className="text-blue-700 hover:underline" href={ev.source_url} target="_blank" rel="noreferrer">{ev.source_title}</a> : ev.source_title}
                    </li>
                  ))}
                </ul>
                <div className="mt-1 flex gap-2 text-xs">
                  <button className="text-emerald-700 hover:underline" onClick={async () => {
                    await Api.reviewClaim(workspaceId, c.id, "teacher_verified"); select(detail.id); }}>verify</button>
                  <button className="text-red-700 hover:underline" onClick={async () => {
                    await Api.reviewClaim(workspaceId, c.id, "rejected"); select(detail.id); }}>reject</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}
