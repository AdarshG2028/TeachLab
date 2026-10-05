"use client";

import { useState } from "react";
import type { WorkspaceCreate } from "@/lib/api";

export const LEVELS = [
  { value: 0, label: "Beginner — I need to learn it first", hint: "Roadmap + phase-by-phase tutorial, then notes & tests" },
  { value: 1, label: "Familiar — structured preparation", hint: "Roadmap, notes and tests without a tutorial" },
  { value: 2, label: "Advanced — just the materials", hint: "Notes and tests directly; roadmap optional" },
] as const;

export function WorkspaceForm({ onSubmit, busy }: { onSubmit: (b: WorkspaceCreate) => void; busy?: boolean }) {
  const [form, setForm] = useState({ topic: "", description: "", audience: "", academic_level: "", goals: "",
    familiarity_level: 0, time: "", language: "en" });
  const set = (k: string, v: string | number) => setForm((f) => ({ ...f, [k]: v }));
  return (
    <form
      className="card space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({
          topic: form.topic, description: form.description, audience: form.audience,
          academic_level: form.academic_level, language: form.language,
          teaching_goals: form.goals.split("\n").map((g) => g.trim()).filter(Boolean),
          familiarity_level: form.familiarity_level as 0 | 1 | 2,
          time_available_minutes: form.time ? Number(form.time) : null,
          content_preferences: {},
        });
      }}
    >
      <h2 className="text-lg font-semibold">New research workspace</h2>
      <label className="block"><span className="label">Topic or subject</span>
        <input className="input" required minLength={2} value={form.topic} onChange={(e) => set("topic", e.target.value)}
          placeholder="e.g. Quantum computing, The French Revolution, IoT" /></label>
      <label className="block"><span className="label">Scope / description (optional)</span>
        <textarea className="input" rows={2} value={form.description} onChange={(e) => set("description", e.target.value)} /></label>
      <div className="grid grid-cols-2 gap-3">
        <label className="block"><span className="label">Student audience</span>
          <input className="input" value={form.audience} onChange={(e) => set("audience", e.target.value)} placeholder="high school students" /></label>
        <label className="block"><span className="label">Academic level</span>
          <input className="input" value={form.academic_level} onChange={(e) => set("academic_level", e.target.value)} placeholder="grade 11" /></label>
        <label className="block"><span className="label">Time available (minutes)</span>
          <input className="input" type="number" min={5} value={form.time} onChange={(e) => set("time", e.target.value)} /></label>
        <label className="block"><span className="label">Language</span>
          <input className="input" value={form.language} onChange={(e) => set("language", e.target.value)} /></label>
      </div>
      <label className="block"><span className="label">Teaching goals (one per line)</span>
        <textarea className="input" rows={2} value={form.goals} onChange={(e) => set("goals", e.target.value)} /></label>
      <fieldset>
        <span className="label">Your familiarity with the subject</span>
        {LEVELS.map((l) => (
          <label key={l.value} className="mt-1 flex items-start gap-2 text-sm">
            <input type="radio" name="level" checked={form.familiarity_level === l.value}
              onChange={() => set("familiarity_level", l.value)} />
            <span><b>{l.label}</b><br /><span className="text-muted">{l.hint}</span></span>
          </label>
        ))}
      </fieldset>
      <button className="btn" disabled={busy || form.topic.trim().length < 2}>Create workspace</button>
    </form>
  );
}
