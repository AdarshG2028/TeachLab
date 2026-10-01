"""Job handlers. Each is idempotent: re-running after a crash/retry does not duplicate data."""

from __future__ import annotations

from typing import Any

from app.services.content.notes import NotesConfig
from app.services.export.service import ExportRequest
from app.worker.runner import Handler, JobContext


async def _build_graph(ctx: JobContext, source_ids: list[str], session_id: str | None, start: int = 10,
                       span: int = 85) -> dict[str, Any]:
    ws = await ctx.workspace()
    c = ctx.container
    totals: dict[str, Any] = {"sources": 0, "entities": 0, "claims": 0, "evidence": 0, "contradictions": 0,
                              "proposals": 0, "rejected": 0, "failed_sources": []}
    for i, sid in enumerate(source_ids):
        await ctx.progress("extracting knowledge", start + int(span * i / max(1, len(source_ids))),
                           f"source {i + 1}/{len(source_ids)}")
        extraction_ids = await c.knowledge.extract_source(ws.id, sid, topic=ws.topic, session_id=session_id)
        report = await c.knowledge.apply_extractions(ws.id, extraction_ids)
        await c.knowledge.mark_source_graph_built(ws.id, sid)
        totals["sources"] += 1
        for k in ("entities", "claims", "evidence", "contradictions", "proposals", "rejected"):
            totals[k] += report.get(k, 0)
    return totals


async def chat_turn(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    return await ctx.container.research.handle_turn(user_id=ctx.user_id, workspace=ws, session_id=p["session_id"],
                                                    message_id=p["message_id"], progress=ctx.progress)


async def research(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    outcome = await ctx.container.research.research(ws.id, p["queries"], ctx.progress,
                                                    max_new_sources=int(p.get("max_new_sources", 6)))
    graph = await _build_graph(ctx, outcome.new_source_ids, p.get("session_id"), start=50, span=45)
    return {"queries": outcome.queries, "cached_queries": outcome.cached_queries,
            "new_source_ids": outcome.new_source_ids, "reused_source_ids": outcome.reused_source_ids,
            "failures": outcome.failures, "graph": graph}


async def graph_extract(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    return await _build_graph(ctx, p["source_ids"], p.get("session_id"))


async def ingest_url(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    await ctx.progress("fetching", 5, p["url"])
    res = await ctx.container.ingestion.ingest_url(ws.id, p["url"])
    if res.created:
        await ctx.progress("indexing", 30)
        await ctx.container.ingestion.index_source(ws.id, res.source_id)
        graph = await _build_graph(ctx, [res.source_id], p.get("session_id"), start=40, span=55)
    else:
        graph = {}
    return {"source_id": res.source_id, "created": res.created, "reason": res.reason, "graph": graph}


async def ingest_upload(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    await ctx.progress("extracting text", 5, p["filename"])
    res = await ctx.container.ingestion.ingest_upload(ws.id, p["filename"], p["storage_key"], p["content_type"])
    graph = {}
    if res.created:
        await ctx.progress("indexing", 30)
        await ctx.container.ingestion.index_source(ws.id, res.source_id)
        graph = await _build_graph(ctx, [res.source_id], p.get("session_id"), start=40, span=55)
    return {"source_id": res.source_id, "created": res.created, "reason": res.reason, "graph": graph}


async def generate_roadmap(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    ver = await ctx.container.roadmap.generate(ws.id, instructions=p.get("instructions", ""), progress=ctx.progress,
                                               only_phase_key=p.get("only_phase_key"))
    return {"roadmap_version_id": ver.id, "version": ver.version, "phases": len(ver.phases)}


async def generate_notes(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    doc = await ctx.container.notes.generate(ws.id, NotesConfig.model_validate(p["config"]), progress=ctx.progress)
    return {"notes_id": doc.id, "kind": doc.kind, "version": doc.version, "sections": len(doc.sections)}


async def generate_assessment(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    return await ctx.container.assessment.generate(ws.id, p["assessment_id"], progress=ctx.progress)


async def regenerate_question(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    await ctx.progress("regenerating question", 20)
    q = await ctx.container.assessment.regenerate_question(ws.id, p["question_id"], p.get("instructions", ""))
    return {"question_id": q.id}


async def export_pdf(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    await ctx.progress("rendering PDF", 30)
    art = await ctx.container.exports.export(ws.id, ExportRequest.model_validate(p["request"]))
    return {"artifact_id": art.id, "kind": art.kind, "size_bytes": art.size_bytes}


async def rebuild_graph(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    await ctx.progress("replaying stored extractions", 10)
    report = await ctx.container.knowledge.rebuild_graph(ws.id)
    report.pop("entity_ids", None)
    return report


async def reindex_vectors(ctx: JobContext, p: dict[str, Any]) -> dict[str, Any]:
    ws = await ctx.workspace()
    await ctx.progress("re-embedding chunks", 10)
    return {"points": await ctx.container.ingestion.reindex_workspace(ws.id)}


HANDLERS: dict[str, Handler] = {
    "chat_turn": chat_turn, "research": research, "graph_extract": graph_extract, "ingest_url": ingest_url,
    "ingest_upload": ingest_upload, "generate_roadmap": generate_roadmap, "generate_notes": generate_notes,
    "generate_assessment": generate_assessment, "regenerate_question": regenerate_question,
    "export_pdf": export_pdf, "rebuild_graph": rebuild_graph, "reindex_vectors": reindex_vectors,
}
