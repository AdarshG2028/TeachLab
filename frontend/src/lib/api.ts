/**
 * Typed API client. Types come from the backend's OpenAPI schema
 * (`pnpm gen:api` or `openapi-typescript openapi.json -o src/lib/api-types.ts`).
 * UI components never call fetch directly; they use these functions so the
 * final design can replace the temporary components without touching API logic.
 */
import type { components } from "./api-types";

export type S = components["schemas"];
export type Workspace = S["WorkspaceOut"];
export type WorkspaceCreate = S["WorkspaceCreate"];
export type Overview = S["WorkspaceOverview"];
export type Session = S["SessionOut"];
export type Message = S["MessageOut"];
export type Job = S["JobOut"];
export type Source = S["SourceOut"];
export type SourceDetail = S["SourceDetail"];
export type Roadmap = S["RoadmapOut"];
export type RoadmapPhase = S["RoadmapPhaseOut"];
export type RoadmapItem = S["RoadmapItemOut"];
export type Coverage = S["CoverageOut"];
export type Notes = S["NotesOut"];
export type Assessment = S["AssessmentOut"];
export type AssessmentSummary = S["AssessmentSummary"];
export type Question = S["QuestionOut"];
export type Artifact = S["ArtifactOut"];
export type Proposal = S["ProposalOut"];
export type AssessmentConfig = S["AssessmentConfig"];
export type NotesConfig = S["NotesConfig"];
export type ExportRequest = S["ExportRequest"];

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const USER_KEY = "teachlab.userId";

export function getUserId(): string {
  if (typeof window === "undefined") return "local-teacher";
  return window.localStorage.getItem(USER_KEY) || "local-teacher";
}

export function setUserId(id: string): void {
  window.localStorage.setItem(USER_KEY, id);
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: unknown = {}) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-User-Id", getUserId());
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = data?.error;
    const detail = Array.isArray(data?.detail) ? data.detail.map((d: { msg: string }) => d.msg).join("; ") : null;
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? detail ?? res.statusText, err?.details);
  }
  return data as T;
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const ws = (id: string) => `/api/workspaces/${id}`;

export const Api = {
  me: () => api<{ user_id: string }>("/api/me"),
  health: () => api<{ status: string; checks: Record<string, string>; providers: Record<string, string> }>("/api/health"),

  listWorkspaces: () => api<Workspace[]>("/api/workspaces"),
  createWorkspace: (body: WorkspaceCreate) => api<Workspace>("/api/workspaces", json(body)),
  updateWorkspace: (id: string, body: Partial<WorkspaceCreate> & { archived?: boolean }) =>
    api<Workspace>(ws(id), { method: "PATCH", body: JSON.stringify(body) }),
  deleteWorkspace: (id: string) => api<void>(ws(id), { method: "DELETE" }),
  overview: (id: string) => api<Overview>(`${ws(id)}/overview`),

  sessions: (id: string) => api<Session[]>(`${ws(id)}/sessions`),
  session: (id: string, sid: string) => api<Session>(`${ws(id)}/sessions/${sid}`),
  createSession: (id: string, title: string) => api<Session>(`${ws(id)}/sessions`, json({ title })),
  messages: (id: string, sid: string, beforeSeq?: number) =>
    api<Message[]>(`${ws(id)}/sessions/${sid}/messages?limit=50${beforeSeq ? `&before_seq=${beforeSeq}` : ""}`),
  sendMessage: (id: string, sid: string, content: string) =>
    api<S["ChatTurnOut"]>(`${ws(id)}/sessions/${sid}/messages`, json({ content })),

  research: (id: string, queries: string[], sessionId?: string) =>
    api<Job>(`${ws(id)}/research`, json({ queries, session_id: sessionId })),
  addUrl: (id: string, url: string) => api<Job>(`${ws(id)}/sources/url`, json({ url })),
  upload: (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return api<Job>(`${ws(id)}/sources/upload`, { method: "POST", body: fd });
  },
  sources: (id: string) => api<Source[]>(`${ws(id)}/sources`),
  source: (id: string, sourceId: string) => api<SourceDetail>(`${ws(id)}/sources/${sourceId}`),

  graphOverview: (id: string) => api<GraphData>(`${ws(id)}/graph/overview`),
  graphStats: (id: string) => api<Record<string, number>>(`${ws(id)}/graph/stats`),
  graphSearch: (id: string, q: string) => api<GraphNode[]>(`${ws(id)}/graph/search?q=${encodeURIComponent(q)}`),
  entity: (id: string, eid: string) => api<EntityDetail>(`${ws(id)}/graph/entities/${eid}`),
  neighborhood: (id: string, eid: string, depth = 1) =>
    api<GraphData>(`${ws(id)}/graph/entities/${eid}/neighborhood?depth=${depth}`),
  claims: (id: string, status?: string) => api<ClaimRow[]>(`${ws(id)}/graph/claims${status ? `?status=${status}` : ""}`),
  reviewClaim: (id: string, claimId: string, status: string, note = "") =>
    api<ClaimDetail>(`${ws(id)}/graph/claims/${claimId}/review`, json({ status, note })),
  proposals: (id: string, status = "pending") => api<Proposal[]>(`${ws(id)}/ontology/proposals?status=${status}`),
  resolveProposal: (id: string, pid: string, body: S["ProposalResolve"]) =>
    api<Proposal>(`${ws(id)}/ontology/proposals/${pid}/resolve`, json(body)),

  roadmap: (id: string) => api<Roadmap | null>(`${ws(id)}/roadmap`),
  roadmapVersions: (id: string) => api<S["RoadmapVersionOut"][]>(`${ws(id)}/roadmap/versions`),
  generateRoadmap: (id: string, instructions = "") => api<Job>(`${ws(id)}/roadmap/generate`, json({ instructions })),
  regeneratePhase: (id: string, key: string, instructions = "") =>
    api<Job>(`${ws(id)}/roadmap/phases/${key}/regenerate`, json({ instructions })),
  saveRoadmap: (id: string, body: S["RoadmapEdit"]) =>
    api<Roadmap>(`${ws(id)}/roadmap`, { method: "PUT", body: JSON.stringify(body) }),
  startPhase: (id: string, key: string, sessionId: string) =>
    api<S["ChatTurnOut"]>(`${ws(id)}/roadmap/phases/${key}/start`, json({ session_id: sessionId })),
  navigate: (id: string, body: S["NavigateRequest"]) => api<Record<string, unknown>>(`${ws(id)}/roadmap/navigate`, json(body)),
  setPhaseStatus: (id: string, key: string, status: string) =>
    api<{ ok: boolean }>(`${ws(id)}/roadmap/phases/${key}/status`, json({ status })),

  coverage: (id: string) => api<Coverage[]>(`${ws(id)}/coverage`),
  markCoverage: (id: string, body: S["CoverageMark"]) => api<{ ok: boolean }>(`${ws(id)}/coverage/mark`, json(body)),
  gaps: (id: string) => api<GapReport>(`${ws(id)}/gaps`),

  generateNotes: (id: string, config: Partial<NotesConfig>) => api<Job>(`${ws(id)}/notes/generate`, json(config)),
  notesList: (id: string, kind?: string) => api<Notes[]>(`${ws(id)}/notes${kind ? `?kind=${kind}` : ""}`),
  notes: (id: string, nid: string) => api<Notes>(`${ws(id)}/notes/${nid}`),
  saveNotes: (id: string, nid: string, body: S["NotesEdit"]) =>
    api<Notes>(`${ws(id)}/notes/${nid}`, { method: "PUT", body: JSON.stringify(body) }),

  questionTypes: (id: string) => api<{ types: string[]; reasons: Record<string, string>; all: string[] }>(
    `${ws(id)}/assessments/question-types`),
  createAssessment: (id: string, cfg: AssessmentConfig) => api<S["AssessmentCreated"]>(`${ws(id)}/assessments`, json(cfg)),
  assessments: (id: string) => api<AssessmentSummary[]>(`${ws(id)}/assessments`),
  assessment: (id: string, aid: string) => api<Assessment>(`${ws(id)}/assessments/${aid}`),
  patchQuestion: (id: string, aid: string, qid: string, patch: S["QuestionPatch"]) =>
    api<Question>(`${ws(id)}/assessments/${aid}/questions/${qid}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteQuestion: (id: string, aid: string, qid: string) =>
    api<void>(`${ws(id)}/assessments/${aid}/questions/${qid}`, { method: "DELETE" }),
  regenerateQuestion: (id: string, aid: string, qid: string, instructions: string) =>
    api<Job>(`${ws(id)}/assessments/${aid}/questions/${qid}/regenerate`, json({ instructions })),
  addQuestion: (id: string, aid: string, body: S["QuestionCreate"]) =>
    api<Question>(`${ws(id)}/assessments/${aid}/questions`, json(body)),

  exportPdf: (id: string, body: ExportRequest) => api<Job>(`${ws(id)}/exports`, json(body)),
  teachingPack: (id: string, body: S["TeachingPackRequest"]) => api<Job>(`${ws(id)}/teaching-pack`, json(body)),
  artifacts: (id: string) => api<Artifact[]>(`${ws(id)}/artifacts`),
  downloadUrl: (id: string, artifactId: string) =>
    `${API_URL}${ws(id)}/artifacts/${artifactId}/download?user_id=${encodeURIComponent(getUserId())}`,

  job: (jobId: string) => api<Job>(`/api/jobs/${jobId}`),
  jobs: (id: string, activeOnly = false) => api<Job[]>(`${ws(id)}/jobs?active_only=${activeOnly}`),
  jobEventsUrl: (jobId: string) => `${API_URL}/api/jobs/${jobId}/events?user_id=${encodeURIComponent(getUserId())}`,
};

// Graph endpoints return plain dicts (no dedicated schema); typed here for the UI.
export interface GraphNode { id: string; name: string; category: string; description?: string; degree?: number }
export interface GraphEdge { id: string; source: string; target: string; type: string; family: string; status?: string; evidence_count?: number }
export interface GraphData { nodes: GraphNode[]; edges: GraphEdge[] }
export interface Evidence {
  evidence_id: string; chunk_id: string; quote: string | null; quote_verified: boolean; confidence: number;
  raw_predicate: string | null; perspective: string | null; extracted_at: string; source_id: string;
  source_title: string; source_url: string | null;
}
export interface ClaimDetail {
  id: string; text: string; kind: string; predicate: string; raw_predicates: string[]; family: string; status: string;
  relation_status: string; confidence: number; perspectives: string[]; time_context: string | null;
  evidence: Evidence[]; contradicts: { id: string; text: string; status: string }[];
}
export interface EntityDetail { id: string; name: string; category: string; description: string; aliases: string[]; claims: ClaimDetail[] }
export interface ClaimRow { id: string; text: string; status: string; predicate: string; evidence_count: number; source_count: number }
export interface Gap {
  type: string; kind: "required" | "recommended" | "optional" | "info"; severity: string; concept_key?: string;
  claim_id?: string; concept_name: string; detail: string; suggested_queries: string[];
}
export interface GapReport { counts: Record<string, number>; gaps: Gap[]; note: string }
