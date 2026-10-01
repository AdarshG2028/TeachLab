"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Api, type Workspace } from "@/lib/api";
import { UserSwitcher } from "@/components/UserSwitcher";
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
    <div className="space-y-4">
      <UserSwitcher onChange={load} />
      <ErrorText error={error} />
      <div className="grid gap-4 md:grid-cols-2">
        <section className="card">
          <h2 className="mb-2 text-lg font-semibold">Your workspaces</h2>
          {items.length === 0 && <p className="text-sm text-slate-500">No workspaces yet.</p>}
          <ul className="divide-y divide-slate-100">
            {items.map((w) => (
              <li key={w.id} className="flex items-center justify-between py-2">
                <div>
                  <a className="font-medium hover:underline" href={`/workspaces/${w.id}`}>{w.title}</a>
                  <p className="text-xs text-slate-500">
                    {LEVELS[w.familiarity_level]?.label.split(" — ")[0]} · {w.audience || "audience n/a"} · updated{" "}
                    {new Date(w.updated_at).toLocaleString()}
                  </p>
                </div>
                <a className="btn-secondary" href={`/workspaces/${w.id}`}>Resume</a>
              </li>
            ))}
          </ul>
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
