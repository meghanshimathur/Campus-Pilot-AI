"""FastAPI app: chat, memory, trace and evaluation-report endpoints, health, direct tool endpoints, and the static
dashboard. create_app() builds every collaborator once (provider, retriever, memory, tracer, agent) so tests can
inject fakes. Routes are plain `def` on purpose: the blocking LLM/SQLite work then runs in FastAPI's thread pool.
There is deliberately no list-all endpoint for traces, students or conversations (no authentication in Assignment 1)."""
import json
import re
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from campuspilot import tools
from campuspilot.agent import Agent
from campuspilot.config import Settings, load_settings
from campuspilot.db import init_db
from campuspilot.evaluation import normalize_report, report_integrity
from campuspilot.memory import ALLOWED_KEYS, MemoryStore
from campuspilot.provider import build_provider
from campuspilot.rag import HandbookRetriever
from campuspilot.tool_registry import ToolRegistry
from campuspilot.tracing import TraceStore


class SupportRequestIn(BaseModel):
    student_id: str
    category: str
    description: str


REPORT_NAME_RE = re.compile(r"^evaluation_(offline|live)_\d{8}_\d{6}\.json$")  # only files our script writes


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    student_id: str = Field(min_length=1, max_length=20)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


def create_app(settings: Settings | None = None, provider=None, retriever=None, memory=None, tracer=None) -> FastAPI:
    settings = settings or load_settings()
    init_db(settings.database_path)
    provider = provider or build_provider(settings)
    # Index is built lazily on the first handbook search, so startup needs no model download.
    retriever = retriever or HandbookRetriever(
        settings.handbook_path, settings.chroma_dir,
        top_k=settings.handbook_top_k, min_score=settings.handbook_min_score,
        strong_score=settings.handbook_strong_score)
    memory = memory or MemoryStore(settings.database_path)
    registry = ToolRegistry(settings.data_dir, settings.database_path, retriever, memory)
    tracer = tracer or TraceStore(settings.database_path)
    agent = Agent(provider, registry, memory=memory, tracer=tracer) if provider else None
    app = FastAPI(title="CampusPilot AI")

    @app.post("/api/chat")
    def chat(body: ChatIn):
        if agent is None:
            raise HTTPException(503, "No LLM provider configured. See .env.example.")
        session_id = body.session_id or uuid.uuid4().hex
        r = agent.run(body.message, body.student_id, session_id)
        return {"session_id": session_id, "trace_id": r.trace_id, "response": r.response,
                "memories_used": r.memories_used, "memories_saved": r.memories_saved,
                "tools_called": r.tools_called, "evidence": r.evidence, "latency_ms": r.latency_ms,
                "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "error": r.error}

    @app.get("/api/memory/{student_id}")
    def get_memory(student_id: str):
        sid = registry.canonical_student_id(student_id)
        if sid is None:
            raise HTTPException(404, "Student not found.")
        return {"student_id": sid, "memories": memory.recall(sid)}

    @app.delete("/api/memory/{student_id}/{key}")
    def delete_memory(student_id: str, key: str):
        sid = registry.canonical_student_id(student_id)
        if sid is None:
            raise HTTPException(404, "Student not found.")
        if key not in ALLOWED_KEYS:
            raise HTTPException(404, "Unknown memory key.")
        return {"deleted": memory.forget(sid, key)}

    @app.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str):
        trace = tracer.get(trace_id) if trace_id.isalnum() and len(trace_id) == 32 else None
        if trace is None:
            raise HTTPException(404, "Trace not found.")
        return trace

    @app.get("/api/evaluation/reports")
    def list_reports():
        """Real saved evaluation reports (newest first); metadata only. Empty list if none exist."""
        out = []
        # newest first by the timestamp in the file name (a plain name sort would group by mode instead)
        for path in sorted(settings.reports_dir.glob("evaluation_*.json"), key=lambda p: p.name.split("_", 2)[-1], reverse=True):
            if not REPORT_NAME_RE.match(path.name):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append({"name": path.name, "mode": data["mode"], "timestamp": data["meta"]["timestamp"],
                            "runs": data["metrics"]["runs"], "integrity": report_integrity(data)})
            except (OSError, ValueError, KeyError):
                continue  # unreadable/malformed report: skip rather than invent anything
        return {"reports": out}

    @app.get("/api/evaluation/reports/{name}")
    def get_report(name: str):
        if not REPORT_NAME_RE.match(name):
            raise HTTPException(404, "Report not found.")
        path = settings.reports_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "Report not found.")
        if isinstance(data, dict):
            data = normalize_report(data)               # old-format reports: provider errors separated at read time
            data["integrity"] = report_integrity(data)  # computed now; never trusted from the file
        return data

    @app.get("/api/health")
    def health():
        return {"status": "ok", "llm_configured": settings.llm_provider is not None}

    @app.get("/api/students/{student_id}/fees")
    def fees(student_id: str):
        return tools.check_fee_status(settings.data_dir, student_id)

    @app.get("/api/students/{student_id}/scholarship")
    def scholarship(student_id: str):
        return tools.check_scholarship_status(settings.data_dir, student_id)

    @app.post("/api/support-requests")
    def support_request(body: SupportRequestIn):
        return tools.create_support_request(
            settings.data_dir, settings.database_path,
            body.student_id, body.category, body.description)

    # Serve the dashboard last, so every /api/* route above takes precedence.
    if settings.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")

    return app
