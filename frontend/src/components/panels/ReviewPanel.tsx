"use client";

import { useCallback, useEffect, useState } from "react";
import { Api, type ClaimRow, type Proposal } from "@/lib/api";
import { ErrorText } from "@/components/ErrorText";

/** Review queue for uncertain ontology changes and contested / unverified claims. */
export function ReviewPanel({ workspaceId }: { workspaceId: string }) {
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [claims, setClaims] = useState<ClaimRow[]>([]);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => {
    Api.proposals(workspaceId).then(setProposals).catch(setError);
    Api.claims(workspaceId, "contested").then(setClaims).catch(setError);
  }, [workspaceId]);
  useEffect(() => load(), [load]);

  const resolve = async (p: Proposal, decision: "accept" | "reject", extra: Record<string, string> = {}) => {
    try { await Api.resolveProposal(workspaceId, p.id, { decision, note: "", ...extra }); load(); } catch (e) { setError(e); }
  };

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <div className="card text-sm">
        <h3 className="mb-2 font-semibold">Ontology proposals ({proposals.length})</h3>
        <ErrorText error={error} />
        <ul className="space-y-2">
          {proposals.map((p) => {
            const payload = p.payload as { claim_ids?: string[]; entity_ids?: string[] };
            return (
              <li key={p.id} className="rounded border border-slate-200 p-2">
                <span className="badge bg-slate-100">{p.kind.replace(/_/g, " ")}</span> {p.summary}
                <div className="mt-1 flex flex-wrap gap-2 text-xs">
                  {p.kind === "new_relation_type" && <>
                    <button className="btn-secondary" onClick={() => resolve(p, "accept")}>Accept new type</button>
                    <button className="btn-secondary" onClick={() => resolve(p, "reject", { map_to: prompt("Map to canonical type", "RELATED_TO") ?? "RELATED_TO" })}>Map to existing</button>
                  </>}
                  {p.kind === "entity_merge" && <>
                    <button className="btn-secondary" onClick={() => resolve(p, "accept")}>Merge</button>
                    <button className="btn-secondary" onClick={() => resolve(p, "reject")}>Keep separate</button>
                  </>}
                  {p.kind === "contradiction" && <>
                    {(payload.claim_ids ?? []).map((cid, i) => (
                      <button key={cid} className="btn-secondary" onClick={() => resolve(p, "accept", { reject_claim_id: cid })}>Reject claim {i + 1}</button>
                    ))}
                    <button className="btn-secondary" onClick={() => resolve(p, "reject")}>Keep both perspectives</button>
                  </>}
                  {(p.kind === "homonym" || p.kind === "type_mismatch") && <>
                    <button className="btn-secondary" onClick={() => resolve(p, "accept")}>Acknowledge</button>
                    <button className="btn-secondary" onClick={() => resolve(p, "reject")}>Dismiss</button>
                  </>}
                </div>
              </li>
            );
          })}
        </ul>
      </div>
      <div className="card text-sm">
        <h3 className="mb-2 font-semibold">Contested claims</h3>
        <ul className="space-y-1">
          {claims.map((c) => (
            <li key={c.id} className="flex justify-between gap-2">
              <span>{c.text} <span className="text-xs text-slate-500">({c.source_count} sources)</span></span>
              <span className="flex gap-1 text-xs">
                <button className="text-emerald-700 hover:underline" onClick={async () => { await Api.reviewClaim(workspaceId, c.id, "teacher_verified"); load(); }}>verify</button>
                <button className="text-red-700 hover:underline" onClick={async () => { await Api.reviewClaim(workspaceId, c.id, "rejected"); load(); }}>reject</button>
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
