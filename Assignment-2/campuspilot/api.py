"""FastAPI app: sign-in, chat, memory, trace and evaluation-report endpoints, health, direct tool endpoints, and the
static dashboard. create_app() builds every collaborator once (provider, retriever, memory, tracer, auth, audit, agent)
so tests can inject fakes. Routes are plain `def` on purpose: the blocking LLM/SQLite work then runs in FastAPI's thread
pool.

Security model (Assignment 2):
  * Identity comes ONLY from a verified bearer token (``current_principal``). A student id in a URL or a request body is
    untrusted: it is compared with the principal and refused (403, audited) if it names someone else.
  * Every route is classified in ROUTE_ACCESS as ``public`` or ``protected``. Protected routes sit on a router whose
    dependency requires a valid session. create_app() refuses to start if any route is unclassified or a protected route
    lacks that dependency, so a new private route cannot be added by accident.
  * Authorization is enforced here and in the tool registry, with authz.authorize (deny by default).
  * Private reads and writes are audited BEFORE data is returned or a change is made; if the audit record cannot be
    written the request fails (503) instead of proceeding.
There is still deliberately no list-all endpoint for traces, students or conversations."""
import json
import logging
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from campuspilot import tools
from campuspilot.agent import Agent
from campuspilot.approvals import ApprovalError, ApprovalService, load_or_create_key
from campuspilot.audit import AuditError, AuditStore
from campuspilot.auth import AuthService
from campuspilot.authz import Action, Principal, authorize
from campuspilot.config import Settings, load_settings
from campuspilot.db import init_db
from campuspilot.evaluation import SECURITY_REPORT_KIND, normalize_report, report_integrity, security_report_integrity
from campuspilot.memory import ALLOWED_KEYS, MemoryStore
from campuspilot.provider import build_provider
from campuspilot.rag import HandbookRetriever
from campuspilot.sensitive_actions import ContactStore
from campuspilot.tool_registry import ToolRegistry
from campuspilot.tracing import TraceStore

logger = logging.getLogger("campuspilot.api")

# Every API route and who may call it. Anything not listed here stops the app from starting.
ROUTE_ACCESS = {
    ("GET", "/api/health"): "public",                         # liveness only: no student data
    ("POST", "/api/auth/login"): "public",                    # this is how a session starts
    ("GET", "/api/auth/me"): "protected",
    ("POST", "/api/auth/logout"): "protected",
    ("POST", "/api/chat"): "protected",
    ("GET", "/api/memory/{student_id}"): "protected",
    ("DELETE", "/api/memory/{student_id}/{key}"): "protected",
    ("GET", "/api/traces/{trace_id}"): "protected",
    ("GET", "/api/evaluation/reports"): "protected",
    ("GET", "/api/evaluation/reports/{name}"): "protected",
    ("GET", "/api/students/{student_id}/fees"): "protected",
    ("GET", "/api/students/{student_id}/scholarship"): "protected",
    ("POST", "/api/support-requests"): "protected",
    ("GET", "/api/students/{student_id}/contact"): "protected",
    ("GET", "/api/approvals"): "protected",
    ("GET", "/api/approvals/{approval_id}"): "protected",
    ("POST", "/api/approvals/{approval_id}/approve"): "protected",
    ("POST", "/api/approvals/{approval_id}/reject"): "protected",
    ("POST", "/api/approvals/{approval_id}/cancel"): "protected",
}
# FastAPI's own documentation pages (they expose the API shape, no data) and the static dashboard mount.
FRAMEWORK_PUBLIC_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

REPORT_NAME_RE = re.compile(r"^evaluation_(offline|live)_\d{8}_\d{6}\.json$")  # only files scripts/run_evaluation.py writes
SECURITY_REPORT_NAME_RE = re.compile(r"^assignment2_evaluation_\d{8}_\d{6}\.json$")  # only files scripts/run_security_evaluation.py writes
_REPORT_STAMP_RE = re.compile(r"(\d{8}_\d{6})\.json$")
_bearer = HTTPBearer(auto_error=False)


class LoginIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class SupportRequestIn(BaseModel):
    student_id: str | None = Field(default=None, max_length=32)   # untrusted; the signed-in student is the authority
    category: str
    description: str


class DecisionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    student_id: str | None = Field(default=None, max_length=32)   # untrusted; the signed-in student is the authority
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


def _depends_on_auth(dependant) -> bool:
    """True if this route's dependency tree includes the session check."""
    for dep in dependant.dependencies:
        if getattr(dep.call, "__campuspilot_session_check__", False) or _depends_on_auth(dep):
            return True
    return False


def _flatten_routes(routes, prefix=""):
    """Every route as (route, full path), looking inside included routers (FastAPI wraps them lazily)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:                                        # an included router
            context = getattr(route, "include_context", None)
            yield from _flatten_routes(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
        else:
            yield route, prefix + (getattr(route, "path", "") or "")


def route_problems(app: FastAPI) -> list:
    """Every way the app's routes break the access classification (empty list = fine). Used at startup and in tests.
    Fails closed: a route type this function does not understand is reported as a problem."""
    problems, seen = [], set()
    for route, path in _flatten_routes(app.routes):
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                key = (method, path)
                seen.add(key)
                access = ROUTE_ACCESS.get(key)
                if access is None:
                    problems.append(f"unclassified route {method} {path}")
                elif access == "protected" and not _depends_on_auth(route.dependant):
                    problems.append(f"protected route {method} {path} has no session check")
        elif path in FRAMEWORK_PUBLIC_PATHS:
            continue
        elif type(route).__name__ == "Mount" and path in ("", "/"):
            continue                                           # the static dashboard (public files only)
        else:
            problems.append(f"unclassified non-API route {path!r} ({type(route).__name__})")
    problems += [f"classified route {m} {p} does not exist" for (m, p) in ROUTE_ACCESS if (m, p) not in seen]
    return problems


def create_app(settings: Settings | None = None, provider=None, retriever=None, memory=None, tracer=None,
               auth: AuthService | None = None, audit: AuditStore | None = None, approvals: ApprovalService | None = None) -> FastAPI:
    settings = settings or load_settings()
    init_db(settings.database_path)
    provider = provider or build_provider(settings)
    # Index is built lazily on the first handbook search, so startup needs no model download.
    retriever = retriever or HandbookRetriever(
        settings.handbook_path, settings.chroma_dir,
        top_k=settings.handbook_top_k, min_score=settings.handbook_min_score,
        strong_score=settings.handbook_strong_score)
    memory = memory or MemoryStore(settings.database_path)
    audit = audit or AuditStore(settings.database_path)
    auth = auth or AuthService(settings.database_path, session_ttl=timedelta(minutes=settings.session_ttl_minutes),
                               max_failed=settings.max_failed_logins, lockout=timedelta(minutes=settings.lockout_minutes))
    approvals = approvals or ApprovalService(
        settings.database_path, auth=auth, audit=audit, ttl=timedelta(minutes=settings.approval_ttl_minutes),
        signing_key=load_or_create_key(settings.database_path.parent / ".approval_key"))
    contacts = ContactStore(settings.database_path)
    registry = ToolRegistry(settings.data_dir, settings.database_path, retriever, memory, audit=audit, approvals=approvals)
    tracer = tracer or TraceStore(settings.database_path)
    agent = Agent(provider, registry, memory=memory, tracer=tracer) if provider else None
    app = FastAPI(title="CampusPilot AI")

    # ------------------------------------------------------------ identity
    def current_principal(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
        """The signed-in user, from a valid, unexpired, unrevoked bearer token. Anything else is 401."""
        principal = auth.principal_for_token(creds.credentials) if creds and creds.scheme.lower() == "bearer" else None
        if principal is None:
            raise HTTPException(401, "Not authenticated.", headers={"WWW-Authenticate": "Bearer"})
        return principal

    current_principal.__campuspilot_session_check__ = True      # route_problems() looks for this marker

    public = APIRouter()
    protected = APIRouter(dependencies=[Depends(current_principal)])

    # ------------------------------------------------------------ helpers
    def audit_or_503(event, principal, action, decision, outcome, **kwargs):
        """Write a required audit record or refuse the request: no record, no action."""
        try:
            return audit.record(event, actor=principal, action=action, decision=decision, outcome=outcome, **kwargs)
        except AuditError:
            logger.warning("audit write failed for %s", event)
            raise HTTPException(503, "The audit log is unavailable, so the request was not performed.") from None

    def audit_after(event, principal, action, decision, outcome, **kwargs):
        """The closing record of an action that already happened: a failure is logged, never hidden or raised."""
        try:
            audit.record(event, actor=principal, action=action, decision=decision, outcome=outcome, **kwargs)
        except AuditError:
            logger.warning("audit write failed after %s", event)

    def refuse(principal, decision, action, target=None, *, status=403, detail="Access denied."):
        """Audit a denied request, then answer with a generic refusal (no hint about whether the target exists)."""
        try:
            audit.record("authz.denied", actor=principal, action=action, decision="denied", outcome="blocked",
                         resource_student_id=str(target)[:32] if isinstance(target, (str, int)) else None,
                         details={"reason": decision.reason})
        except AuditError:
            logger.warning("audit write failed for a denied request")       # refused either way
        raise HTTPException(status, detail)

    # ------------------------------------------------------------ public routes
    @public.get("/api/health")
    def health():
        return {"status": "ok", "llm_configured": settings.llm_provider is not None}

    @public.post("/api/auth/login")
    def login(body: LoginIn, request: Request):
        result = auth.authenticate(body.user_id, body.password)
        client = request.client.host if request.client else None
        if not result.ok:
            try:      # the precise reason goes to the audit log only; the client always sees the same answer
                audit.record("auth.login", actor=("anonymous", "anonymous"), action="login", decision="denied",
                             outcome="failure", details={"reason": result.reason, "attempted_user": body.user_id[:32],
                                                         "client": client})
            except AuditError:
                logger.warning("audit write failed for a failed login")
            raise HTTPException(401, "Invalid credentials.", headers={"WWW-Authenticate": "Bearer"})
        principal = result.principal
        audit_or_503("auth.login", principal, "login", "allowed", "success", details={"client": client})
        token, expires_at = auth.create_session(principal)
        return {"token": token, "token_type": "bearer", "expires_at": expires_at,
                "user": {"user_id": principal.user_id, "role": principal.role, "student_id": principal.student_id,
                         "display_name": principal.display_name}}

    # ------------------------------------------------------------ protected routes
    @protected.get("/api/auth/me")
    def me(principal: Principal = Depends(current_principal)):
        return {"user_id": principal.user_id, "role": principal.role, "student_id": principal.student_id,
                "display_name": principal.display_name}

    @protected.post("/api/auth/logout")
    def logout(principal: Principal = Depends(current_principal), creds: HTTPAuthorizationCredentials = Depends(_bearer)):
        revoked = auth.revoke(creds.credentials)               # enforced server-side: the token stops working at once
        audit_after("auth.logout", principal, "logout", "allowed", "success" if revoked else "failure")
        return {"logged_out": revoked}

    @protected.post("/api/chat")
    def chat(body: ChatIn, principal: Principal = Depends(current_principal)):
        if body.student_id is not None:
            # untrusted claim: it may only repeat who is signed in; naming anyone else is refused and audited
            decision = authorize(principal, Action.READ_OWN_PRIVATE, target_student_id=body.student_id)
            if not decision.allowed:
                refuse(principal, decision, "chat", body.student_id)
        if agent is None:
            raise HTTPException(503, "No LLM provider configured. See .env.example.")
        session_id = body.session_id or uuid.uuid4().hex
        r = agent.run(body.message, principal, session_id)
        return {"session_id": session_id, "trace_id": r.trace_id, "response": r.response,
                "memories_used": r.memories_used, "memories_saved": r.memories_saved,
                "secrets_masked": r.secrets_masked,          # categories only, e.g. ["otp"]; the values are never kept
                "tools_called": r.tools_called, "evidence": r.evidence, "latency_ms": r.latency_ms,
                "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "error": r.error}

    @protected.get("/api/memory/{student_id}")
    def get_memory(student_id: str, principal: Principal = Depends(current_principal)):
        decision = authorize(principal, Action.READ_OWN_MEMORY, target_student_id=student_id)
        if not decision.allowed:
            refuse(principal, decision, "read_own_memory", student_id)
        sid = principal.student_id
        audit_or_503("memory.read", principal, "read_own_memory", "allowed", "success", resource_student_id=sid)
        return {"student_id": sid, "memories": memory.recall(sid)}

    @protected.delete("/api/memory/{student_id}/{key}")
    def delete_memory(student_id: str, key: str, principal: Principal = Depends(current_principal)):
        decision = authorize(principal, Action.DELETE_OWN_MEMORY, target_student_id=student_id)
        if not decision.allowed:
            refuse(principal, decision, "delete_own_memory", student_id)
        if key not in ALLOWED_KEYS:
            raise HTTPException(404, "Unknown memory key.")
        sid = principal.student_id
        audit_or_503("memory.delete", principal, "delete_own_memory", "allowed", "pending", resource_student_id=sid,
                     details={"key": key})                       # no audit record, no deletion
        deleted = memory.forget(sid, key)
        audit_after("memory.delete", principal, "delete_own_memory", "allowed", "success" if deleted else "failure",
                    resource_student_id=sid, details={"key": key})
        return {"deleted": deleted}

    @protected.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str, principal: Principal = Depends(current_principal)):
        trace = tracer.get(trace_id) if trace_id.isalnum() and len(trace_id) == 32 else None
        if trace is None:
            raise HTTPException(404, "Trace not found.")
        decision = authorize(principal, Action.VIEW_OWN_TRACE, target_student_id=trace.get("student_id"))
        if not decision.allowed:      # someone else's trace looks exactly like a missing one
            refuse(principal, decision, "view_own_trace", trace.get("student_id"), status=404, detail="Trace not found.")
        return trace

    @protected.get("/api/evaluation/reports")
    def list_reports(principal: Principal = Depends(current_principal)):
        """Real saved evaluation reports (newest first); metadata only. Empty list if none exist. Assignment 1 entries keep
        their original five fields; Assignment 2 security reports are marked with ``kind: "assignment2_security"``."""
        decision = authorize(principal, Action.READ_PUBLIC)
        if not decision.allowed:
            refuse(principal, decision, "evaluation_reports")
        out = []
        # newest first by the timestamp in the file name (a plain name sort would group by mode or prefix instead)
        paths = [p for p in settings.reports_dir.glob("*.json") if REPORT_NAME_RE.match(p.name) or SECURITY_REPORT_NAME_RE.match(p.name)]
        for path in sorted(paths, key=lambda p: _REPORT_STAMP_RE.search(p.name).group(1), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if SECURITY_REPORT_NAME_RE.match(path.name):
                    out.append({"name": path.name, "kind": "assignment2_security", "mode": data["mode"], "timestamp": data["meta"]["timestamp"],
                                "runs": data["metrics"]["scenario_pass_rate"]["denominator"], "integrity": security_report_integrity(data)})
                else:
                    out.append({"name": path.name, "mode": data["mode"], "timestamp": data["meta"]["timestamp"],
                                "runs": data["metrics"]["runs"], "integrity": report_integrity(data)})
            except (OSError, ValueError, KeyError, TypeError):
                continue  # unreadable/malformed report: skip rather than invent anything
        return {"reports": out}

    @protected.get("/api/evaluation/reports/{name}")
    def get_report(name: str, principal: Principal = Depends(current_principal)):
        decision = authorize(principal, Action.READ_PUBLIC)
        if not decision.allowed:
            refuse(principal, decision, "evaluation_reports")
        security = bool(SECURITY_REPORT_NAME_RE.match(name))
        if not security and not REPORT_NAME_RE.match(name):
            raise HTTPException(404, "Report not found.")
        path = settings.reports_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "Report not found.")
        if security:
            if not isinstance(data, dict) or data.get("kind") != SECURITY_REPORT_KIND:
                raise HTTPException(404, "Report not found.")          # a file with this name that is not a security report
            data["integrity"] = security_report_integrity(data)         # computed now; never trusted from the file
        elif isinstance(data, dict):
            data = normalize_report(data)               # old-format reports: provider errors separated at read time
            data["integrity"] = report_integrity(data)  # computed now; never trusted from the file
        return data

    def own_private_read(principal, requested_id, tool_name, fn):
        """Fee / scholarship over HTTP: ownership check, then audit BEFORE the data leaves the server."""
        decision = authorize(principal, Action.READ_OWN_PRIVATE, target_student_id=requested_id)
        if not decision.allowed:
            refuse(principal, decision, tool_name, requested_id)
        sid = principal.student_id                       # never the id from the URL
        result = fn(settings.data_dir, sid)
        audit_or_503("tool.private_read", principal, tool_name, "allowed", "failure" if "error" in result else "success",
                     resource_student_id=sid)
        return result

    @protected.get("/api/students/{student_id}/fees")
    def fees(student_id: str, principal: Principal = Depends(current_principal)):
        return own_private_read(principal, student_id, "check_fee_status", tools.check_fee_status)

    @protected.get("/api/students/{student_id}/scholarship")
    def scholarship(student_id: str, principal: Principal = Depends(current_principal)):
        return own_private_read(principal, student_id, "check_scholarship_status", tools.check_scholarship_status)

    @protected.post("/api/support-requests")
    def support_request(body: SupportRequestIn, principal: Principal = Depends(current_principal)):
        decision = authorize(principal, Action.CREATE_SUPPORT_REQUEST, target_student_id=body.student_id)
        if not decision.allowed:
            refuse(principal, decision, "create_support_request", body.student_id)
        sid = principal.student_id
        audit_or_503("tool.write", principal, "create_support_request", "allowed", "pending", resource_student_id=sid,
                     details={"category": body.category})        # no audit record, no ticket
        result = tools.create_support_request(settings.data_dir, settings.database_path, sid, body.category, body.description)
        audit_after("tool.write", principal, "create_support_request", "allowed",
                    "failure" if "error" in result else "success", resource_student_id=sid,
                    details={k: result[k] for k in ("ticket_id", "status", "category") if k in result})
        return result

    @protected.get("/api/students/{student_id}/contact")
    def contact(student_id: str, principal: Principal = Depends(current_principal)):
        """The student's own contact details as last changed through an approved request (never anyone else's)."""
        decision = authorize(principal, Action.READ_OWN_PRIVATE, target_student_id=student_id)
        if not decision.allowed:
            refuse(principal, decision, "read_contact_details", student_id)
        sid = principal.student_id
        audit_or_503("tool.private_read", principal, "read_contact_details", "allowed", "success", resource_student_id=sid)
        record = contacts.get(sid) or {"email": None, "phone": None, "updated_at": None, "approval_id": None}
        return {"student_id": sid, "email": record["email"], "phone": record["phone"], "updated_at": record["updated_at"]}

    # ------------------------------------------------------------ approvals (human approval of sensitive actions)
    _APPROVAL_STATUS = {"not_found": (404, "Approval request not found."), "forbidden": (403, "Access denied."),
                        "conflict": (409, "This request can no longer be changed."),
                        "expired": (409, "This request has expired."),
                        "integrity": (409, "This request failed an integrity check and was not executed."),
                        "invalid": (422, "The request is not valid."), "limit": (429, "Too many pending requests."),
                        "audit_unavailable": (503, "The audit log is unavailable, so the request was not performed."),
                        "execution_failed": (500, "The approved action could not be completed.")}

    def approval_failure(exc: ApprovalError):
        status, detail = _APPROVAL_STATUS.get(exc.kind, (500, "Request failed."))
        if exc.kind in ("invalid", "limit") and str(exc) and str(exc) != exc.kind:
            detail = str(exc)                      # these messages are written never to contain the submitted value
        raise HTTPException(status, detail) from None

    @protected.get("/api/approvals")
    def list_approvals(status: str | None = None, principal: Principal = Depends(current_principal)):
        """Students: their own requests. Staff: the pending queue (proposed values shown masked)."""
        try:
            items = approvals.list_for(principal, status)
        except ApprovalError as exc:
            approval_failure(exc)
        audit_or_503("approval.list", principal, "list_approvals", "allowed", "success", details={"count": len(items)})
        return {"approvals": items}

    @protected.get("/api/approvals/{approval_id}")
    def get_approval(approval_id: str, principal: Principal = Depends(current_principal)):
        try:
            item = approvals.get(principal, approval_id)
        except ApprovalError as exc:
            approval_failure(exc)
        audit_or_503("approval.read", principal, "view_approval", "allowed", "success", resource_student_id=item["student_id"],
                     approval_id=approval_id)
        return item

    @protected.post("/api/approvals/{approval_id}/approve")
    def approve(approval_id: str, body: DecisionIn | None = None, principal: Principal = Depends(current_principal)):
        try:
            return approvals.decide(approval_id, principal, approve=True, reason=body.reason if body else None)
        except ApprovalError as exc:
            approval_failure(exc)

    @protected.post("/api/approvals/{approval_id}/reject")
    def reject(approval_id: str, body: DecisionIn | None = None, principal: Principal = Depends(current_principal)):
        try:
            return approvals.decide(approval_id, principal, approve=False, reason=body.reason if body else None)
        except ApprovalError as exc:
            approval_failure(exc)

    @protected.post("/api/approvals/{approval_id}/cancel")
    def cancel_approval(approval_id: str, principal: Principal = Depends(current_principal)):
        try:
            return {"approval": approvals.cancel(principal, approval_id)}
        except ApprovalError as exc:
            approval_failure(exc)

    app.include_router(public)
    app.include_router(protected)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        """FastAPI's default 422 body echoes the submitted values back, which would return a mistyped password (or
        anything else the client sent). Keep where and why, drop what."""
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    problems = route_problems(app)
    if problems:      # fail closed: an unclassified or unguarded route must not go live
        raise RuntimeError("route security check failed: " + "; ".join(problems))

    # Serve the dashboard last, so every /api/* route above takes precedence.
    if settings.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")

    return app
