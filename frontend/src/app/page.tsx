"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Api, type Workspace } from "@/lib/api";
import { AccessKey } from "@/components/AccessKey";
import { WorkspaceForm, LEVELS } from "@/components/WorkspaceForm";
import { ErrorText } from "@/components/ErrorText";

export default function Home() {
  const router = useRouter();
  const [items, setItems] = useState<Workspace[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    Api.listWorkspaces().then(setItems).catch(setError);
  }, []);
  useEffect(() => load(), [load]);

  return (
    <div className="space-y-5">
      <section className="card-dark fade-in">
        <p className="label flex items-center gap-2"><span className="tile h-7 w-7 text-xs">TL</span> TeachLab · Teaching research workspace</p>
        <h1 className="gradient-text max-w-2xl text-4xl font-bold leading-tight">
          Research it once. Teach it with confidence.
        </h1>
        <p className="muted mt-2 max-w-xl text-sm">
          Build a roadmap, learn each phase, then get notes, a test and an answer key as PDFs, all grounded in real sources.
        </p>
        <div className="relative z-10 mt-4 text-white"><AccessKey onChange={load} /></div>
      </section>
      <ErrorText error={error} />
      <div className="grid gap-4 md:grid-cols-2">
        <section className="space-y-3">
          <h2 className="px-1 text-lg font-semibold">Your workspaces</h2>
          {items.length === 0 && <p className="muted px-1 text-sm">No workspaces yet. Create your first one.</p>}
          {items.map((w, i) => (
            <a key={w.id} href={`/workspaces/${w.id}`}
              className={`${i % 2 === 0 ? "card-dark" : "card"} fade-in block transition hover:-translate-y-0.5`}>
              <div className="relative z-10 flex items-start justify-between gap-3">
                <div>
                  <p className="label"><span className="dot mr-1" />{LEVELS[w.familiarity_level]?.label.split(" — ")[0]}</p>
                  <h3 className="text-lg font-semibold leading-snug">{w.title}</h3>
                  <p className="muted mt-1 text-xs">
                    {w.audience || "audience n/a"} · updated {new Date(w.updated_at).toLocaleDateString()}
                  </p>
                </div>
                <span className={i % 2 === 0 ? "btn-accent" : "btn"}>Resume →</span>
              </div>
            </a>
          ))}
        </section>
        <WorkspaceForm
          busy={busy}
          onSubmit={async (body) => {
            setBusy(true);
            try {
              const w = await Api.createWorkspace(body);
              router.push(`/workspaces/${w.id}`);
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        />
      </div>
    </div>
  );
}
