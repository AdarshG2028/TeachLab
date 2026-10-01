"use client";

import { useCallback, useEffect, useState } from "react";
import { Api, type Assessment, type AssessmentConfig, type AssessmentSummary, type Question } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import { JobProgress } from "@/components/JobProgress";
import { ErrorText } from "@/components/ErrorText";

type QType = AssessmentConfig["question_types"] extends (infer T)[] | undefined ? T : string;
type SectionRow = { question_type: QType; count: number; marks_each: number; difficulty: "easy" | "medium" | "hard" | "mixed" };
const DEFAULT_MARKS: Record<string, number> = { mcq: 1, true_false: 1, fill_blank: 1, short_answer: 3, long_answer: 8,
  numerical: 4, coding: 8, application: 5 };

export function AssessmentPanel({ workspaceId, defaultLevel }: { workspaceId: string; defaultLevel: string }) {
  const [types, setTypes] = useState<{ types: string[]; all: string[]; reasons: Record<string, string> } | null>(null);
  const [list, setList] = useState<AssessmentSummary[]>([]);
  const [current, setCurrent] = useState<Assessment | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [cfg, setCfg] = useState({ title: "Unit test", student_level: defaultLevel, duration_minutes: 45, total_marks: "",
    coverage_mode: "strict" as "strict" | "expanded", difficulty: "mixed" as "easy" | "medium" | "hard" | "mixed",
    easy: 30, medium: 50, hard: 20, custom_instructions: "", include_answers_in_paper: false });
  const [sections, setSections] = useState<SectionRow[]>([
    { question_type: "mcq" as QType, count: 5, marks_each: 1, difficulty: "mixed" },
    { question_type: "short_answer" as QType, count: 3, marks_each: 3, difficulty: "mixed" },
  ]);

  const loadList = useCallback(() => { Api.assessments(workspaceId).then(setList).catch(setError); }, [workspaceId]);
  const open = useCallback((aid: string) => { Api.assessment(workspaceId, aid).then(setCurrent).catch(setError); }, [workspaceId]);
  useEffect(() => { Api.questionTypes(workspaceId).then(setTypes).catch(setError); loadList(); }, [workspaceId, loadList]);

  const total = sections.reduce((s, r) => s + r.count * r.marks_each, 0);
  const submit = async () => {
    setError(null);
    const body: AssessmentConfig = {
      title: cfg.title, student_level: cfg.student_level, duration_minutes: cfg.duration_minutes,
      coverage_mode: cfg.coverage_mode, difficulty: cfg.difficulty, custom_instructions: cfg.custom_instructions,
      include_answers_in_paper: cfg.include_answers_in_paper, answers_in_separate_document: true,
      total_marks: cfg.total_marks ? Number(cfg.total_marks) : null,
      difficulty_distribution: cfg.difficulty === "mixed" ? { easy: cfg.easy, medium: cfg.medium, hard: cfg.hard } : null,
      question_types: Array.from(new Set(sections.map((s) => s.question_type))),
      sections: sections.map((s) => ({ ...s, difficulty: s.difficulty === "mixed" ? null : s.difficulty })),
    } as AssessmentConfig;
    try {
      const created = await Api.createAssessment(workspaceId, body);
      setJobId(created.job.id);
      setCurrent(null);
      loadList();
    } catch (e) { setError(e); }
  };

  return (
    <div className="grid gap-3 lg:grid-cols-[420px_1fr]">
      <div className="space-y-3">
        <div className="card space-y-2 text-sm">
          <h3 className="font-semibold">Configure a test</h3>
          <label className="block"><span className="label">Title</span>
            <input className="input" value={cfg.title} onChange={(e) => setCfg({ ...cfg, title: e.target.value })} /></label>
          <div className="grid grid-cols-2 gap-2">
            <label><span className="label">Student level</span>
              <input className="input" value={cfg.student_level} onChange={(e) => setCfg({ ...cfg, student_level: e.target.value })} /></label>
            <label><span className="label">Duration (min)</span>
              <input className="input" type="number" value={cfg.duration_minutes} onChange={(e) => setCfg({ ...cfg, duration_minutes: Number(e.target.value) })} /></label>
            <label><span className="label">Coverage mode</span>
              <select className="input" value={cfg.coverage_mode} onChange={(e) => setCfg({ ...cfg, coverage_mode: e.target.value as "strict" | "expanded" })}>
                <option value="strict">strict — only covered material</option>
                <option value="expanded">expanded — apply & connect covered concepts</option></select></label>
            <label><span className="label">Difficulty</span>
              <select className="input" value={cfg.difficulty} onChange={(e) => setCfg({ ...cfg, difficulty: e.target.value as typeof cfg.difficulty })}>
                <option>easy</option><option>medium</option><option>hard</option><option>mixed</option></select></label>
          </div>
          {cfg.difficulty === "mixed" && (
            <div className="grid grid-cols-3 gap-2">
              {(["easy", "medium", "hard"] as const).map((d) => (
                <label key={d}><span className="label">{d} %</span>
                  <input className="input" type="number" min={0} value={cfg[d]} onChange={(e) => setCfg({ ...cfg, [d]: Number(e.target.value) })} /></label>
              ))}
            </div>
          )}
          <span className="label">Sections</span>
          {sections.map((s, i) => (
            <div key={i} className="flex gap-1">
              <select className="input" value={s.question_type} onChange={(e) => setSections(sections.map((x, k) => k === i ? {
                ...x, question_type: e.target.value as QType, marks_each: DEFAULT_MARKS[e.target.value] ?? 1 } : x))}>
                {(types?.all ?? []).map((t) => <option key={t} value={t}>{t}{types?.types.includes(t) ? "" : " (not suggested)"}</option>)}
              </select>
              <input className="input w-16" type="number" min={1} title="count" value={s.count}
                onChange={(e) => setSections(sections.map((x, k) => k === i ? { ...x, count: Number(e.target.value) } : x))} />
              <input className="input w-16" type="number" min={0.5} step={0.5} title="marks each" value={s.marks_each}
                onChange={(e) => setSections(sections.map((x, k) => k === i ? { ...x, marks_each: Number(e.target.value) } : x))} />
              <button className="btn-secondary" onClick={() => setSections(sections.filter((_, k) => k !== i))}>✕</button>
            </div>
          ))}
          <button className="btn-secondary" onClick={() => setSections([...sections, { question_type: "true_false" as QType,
            count: 2, marks_each: 1, difficulty: "mixed" }])}>+ Section</button>
          <p className="text-xs">Computed total: <b>{total}</b> marks</p>
          <label className="block"><span className="label">Required total marks (optional — validated)</span>
            <input className="input" value={cfg.total_marks} onChange={(e) => setCfg({ ...cfg, total_marks: e.target.value })} /></label>
          <label className="block"><span className="label">Custom instructions</span>
            <textarea className="input" rows={2} value={cfg.custom_instructions} onChange={(e) => setCfg({ ...cfg, custom_instructions: e.target.value })} /></label>
          <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={cfg.include_answers_in_paper}
            onChange={(e) => setCfg({ ...cfg, include_answers_in_paper: e.target.checked })} />Include answers in the student paper (normally off)</label>
          <button className="btn" disabled={!!jobId} onClick={submit}>Generate test</button>
          <JobProgress jobId={jobId} label="Test" onDone={(j) => {
            setJobId(null); loadList();
            const aid = (j.result as { assessment_id?: string }).assessment_id;
            if (aid) open(aid);
          }} />
          <ErrorText error={error} />
        </div>
        <div className="card text-sm">
          <h3 className="mb-1 font-semibold">Tests</h3>
          <ul>{list.map((a) => (
            <li key={a.id}><button className="text-blue-700 hover:underline" onClick={() => open(a.id)}>{a.title}</button>
              <span className="text-xs text-slate-500"> · {a.status} · {a.total_marks} marks</span></li>
          ))}</ul>
        </div>
      </div>
      <div>{current && <AssessmentView workspaceId={workspaceId} a={current} reload={() => open(current.id)} />}</div>
    </div>
  );
}

function AssessmentView({ workspaceId, a, reload }: { workspaceId: string; a: Assessment; reload: () => void }) {
  const report = a.validation_report as { consistent?: boolean; problems?: string[]; total_marks?: number;
    flagged_questions?: number; scope?: Record<string, unknown> };
  const [jobId, setJobId] = useState<string | null>(null);
  return (
    <div className="space-y-2">
      <div className={`card text-sm ${report.consistent ? "" : "border-amber-400"}`}>
        <h3 className="font-semibold">{a.title} — {report.total_marks} marks · {a.questions.length} questions</h3>
        <p>{report.consistent ? "Counts and marks match the configuration." : `Issues: ${(report.problems ?? []).join("; ")}`}</p>
        <p className="text-xs text-slate-500">{report.flagged_questions} question(s) flagged for review · scope: {JSON.stringify(report.scope)}</p>
        <JobProgress jobId={jobId} label="Regenerate" onDone={() => { setJobId(null); reload(); }} />
      </div>
      {a.questions.map((q, i) => <QuestionCard key={q.id} n={i + 1} q={q} workspaceId={workspaceId} aid={a.id}
        onChanged={reload} onJob={setJobId} />)}
    </div>
  );
}

function QuestionCard({ n, q, workspaceId, aid, onChanged, onJob }: {
  n: number; q: Question; workspaceId: string; aid: string; onChanged: () => void; onJob: (id: string) => void;
}) {
  const [edit, setEdit] = useState(false);
  const [text, setText] = useState(q.text);
  const [marks, setMarks] = useState(q.marks);
  const [error, setError] = useState<unknown>(null);
  const answer = q.answer as { text?: string; choice_label?: string; value?: number; units?: string };
  return (
    <div className="card text-sm">
      <div className="flex justify-between">
        <span className="text-xs text-slate-500">Q{n} · {q.section_id} · {q.type} · {q.difficulty} · {q.marks} marks · {q.origin}</span>
        <span className="flex gap-2 text-xs">
          <button className="text-blue-700 hover:underline" onClick={() => setEdit(!edit)}>edit</button>
          <button className="text-blue-700 hover:underline" onClick={async () => {
            const ins = prompt("Instructions for the replacement question (optional)") ?? "";
            onJob((await Api.regenerateQuestion(workspaceId, aid, q.id, ins)).id);
          }}>regenerate</button>
          <button className="text-red-700 hover:underline" onClick={async () => { await Api.deleteQuestion(workspaceId, aid, q.id); onChanged(); }}>remove</button>
        </span>
      </div>
      {!edit ? <Markdown>{q.text}</Markdown> : (
        <div className="space-y-1">
          <textarea className="input" rows={3} value={text} onChange={(e) => setText(e.target.value)} />
          <input className="input w-24" type="number" value={marks} onChange={(e) => setMarks(Number(e.target.value))} />
          <button className="btn" onClick={async () => {
            try { await Api.patchQuestion(workspaceId, aid, q.id, { text, marks }); setEdit(false); onChanged(); } catch (e) { setError(e); }
          }}>Save</button>
          <ErrorText error={error} />
        </div>
      )}
      {q.choices.length > 0 && <ol className="ml-5 list-[upper-alpha]">{q.choices.map((c) => <li key={c}>{c}</li>)}</ol>}
      <details className="mt-1 text-xs"><summary className="cursor-pointer text-slate-500">Answer key (teacher only)</summary>
        <p><b>Answer:</b> {answer.choice_label ? `(${answer.choice_label}) ` : ""}{answer.text ?? `${answer.value} ${answer.units ?? ""}`}</p>
        {q.explanation && <p><b>Explanation:</b> {q.explanation}</p>}
        {q.rubric.length > 0 && <ul>{q.rubric.map((r, i) => <li key={i}>{String(r.criterion)} — {String(r.marks)}</li>)}</ul>}
        {q.source_refs.length > 0 && <p>Based on: {q.source_refs.map((r) => String(r.title)).join("; ")}</p>}
      </details>
      {q.flags.length > 0 && <ul className="mt-1 text-xs text-amber-700">{q.flags.map((f, i) => <li key={i}>⚑ {String(f.message)}</li>)}</ul>}
    </div>
  );
}
