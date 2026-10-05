import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Api, ApiError, api, setUserId } from "@/lib/api";
import { WorkspaceForm } from "@/components/WorkspaceForm";
import { JobProgress } from "@/components/JobProgress";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue(new Response(body === null ? null : JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

describe("api client", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("sends the user identity header and JSON body", async () => {
    setUserId("teacher-a");
    const fetchMock = mockFetch(201, { id: "w1" });
    await Api.createWorkspace({ topic: "IoT", familiarity_level: 1 } as never);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://localhost:8000/api/workspaces");
    expect((init.headers as Headers).get("X-User-Id")).toBe("teacher-a");
    expect((init.headers as Headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body)).toMatchObject({ topic: "IoT", familiarity_level: 1 });
  });

  it("maps backend errors to ApiError with code and details", async () => {
    mockFetch(422, { error: { code: "validation_failed", message: "Totals differ", details: { computed_total: 8 } } });
    await expect(api("/x")).rejects.toMatchObject({ status: 422, code: "validation_failed", message: "Totals differ" });
    mockFetch(404, { error: { code: "not_found", message: "Workspace not found" } });
    const err = (await api("/x").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
  });

  it("builds download and SSE URLs carrying the user id", () => {
    setUserId("teacher b");
    expect(Api.downloadUrl("w1", "a1")).toBe("http://localhost:8000/api/workspaces/w1/artifacts/a1/download?user_id=teacher%20b");
    expect(Api.jobEventsUrl("j1")).toContain("/api/jobs/j1/events?user_id=");
  });
});

describe("WorkspaceForm", () => {
  it("submits topic, familiarity level and goals", () => {
    const onSubmit = vi.fn();
    render(<WorkspaceForm onSubmit={onSubmit} />);
    fireEvent.change(screen.getByPlaceholderText(/Quantum computing/), { target: { value: "French Revolution" } });
    fireEvent.click(screen.getByLabelText(/Advanced/));
    fireEvent.click(screen.getByText("Create workspace"));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ topic: "French Revolution", familiarity_level: 2 }));
  });
});

describe("JobProgress", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("polls when SSE is unavailable and reports completion", async () => {
    vi.stubGlobal("EventSource", undefined);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockFetch(200, { id: "j1", kind: "export_pdf", status: "succeeded", attempts: 1, max_attempts: 2,
      progress: { percent: 100, current: "done" }, result: { artifact_id: "a1" }, error: {}, workspace_id: "w1",
      created_at: "", started_at: null, finished_at: null });
    const done = vi.fn();
    render(<JobProgress jobId="j1" label="PDF" onDone={done} />);
    await vi.advanceTimersByTimeAsync(1600);
    await waitFor(() => expect(done).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("job-progress")).toHaveTextContent("succeeded");
    vi.useRealTimers();
  });
});

describe("GuidedBar", () => {
  const phase = (key: string, status: string) => ({ id: key, key, title: `Title ${key}`, description: "", objectives: [],
    order_index: 0, included: true, teacher_edited: false, estimated_minutes: null, status, items: [] });
  const roadmap = (statuses: string[]) => ({ id: "r", version: 1, organization: "prerequisite", summary: "", origin: "generated",
    created_at: "", phases: statuses.map((s, i) => phase(`p${i + 1}`, s)) }) as never;

  it("offers start when there is no roadmap", async () => {
    const { GuidedBar } = await import("@/components/GuidedBar");
    const onSend = vi.fn();
    render(<GuidedBar roadmap={null} currentPhaseKey={null} busy={false} onSend={onSend} />);
    fireEvent.click(screen.getByText(/Start: build my roadmap/));
    expect(onSend).toHaveBeenCalledWith("start");
  });

  it("shows the current phase and sends next / pack commands", async () => {
    const { GuidedBar } = await import("@/components/GuidedBar");
    const onSend = vi.fn();
    render(<GuidedBar roadmap={roadmap(["completed", "in_progress", "not_started"])} currentPhaseKey="p2" busy={false} onSend={onSend} />);
    expect(screen.getByTestId("guided-bar")).toHaveTextContent("Phase 2 of 3: Title p2");
    fireEvent.click(screen.getByText(/Next phase/));
    fireEvent.click(screen.getByText(/Generate teaching pack/));
    expect(onSend.mock.calls).toEqual([["next"], ["generate teaching pack"]]);
  });

  it("hides next when every phase is done", async () => {
    const { GuidedBar } = await import("@/components/GuidedBar");
    render(<GuidedBar roadmap={roadmap(["completed", "completed"])} currentPhaseKey="p2" busy={false} onSend={vi.fn()} />);
    expect(screen.queryByText(/Next phase/)).toBeNull();
    expect(screen.getByTestId("guided-bar")).toHaveTextContent("All 2 phases completed");
  });
});
