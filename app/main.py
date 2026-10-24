import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import ai, config, store

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED"}
WORKING, INPUT, DONE, CANCELED = (
    f"TASK_STATE_{s}" for s in ("WORKING", "INPUT_REQUIRED", "COMPLETED", "CANCELED")
)
HDR = {"A2A-Version": "1.0", "Cache-Control": "no-store"}
locks = {}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(o):
    return hashlib.sha256(canon(o).encode("utf-8")).hexdigest()


def ok(obj):
    return Response(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
        status_code=200, media_type=config.A2A_MT, headers=HDR,
    )


def err(status, name, message, reason=None):
    body = {"error": {"code": status, "status": name, "message": message,
                      "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                   "reason": reason or name, "domain": "a2a-protocol.org"}]}}
    h = dict(HDR)
    if status == 401:
        h["WWW-Authenticate"] = "Bearer"
    return Response(json.dumps(body), status_code=status, media_type="application/json", headers=h)


def invalid(msg="Invalid request"):
    return err(400, "INVALID_ARGUMENT", msg)


def accept_ok(request):
    a = request.headers.get("accept")
    if not a:
        return True
    for tok in a.split(","):
        if tok.split(";")[0].strip().lower() in ("*/*", "application/*", "application/json", config.A2A_MT):
            return True
    return False


def pre(request, body=False):
    h = request.headers.get("authorization", "").strip()
    scheme, _, token = h.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None, err(401, "UNAUTHENTICATED", "Authentication required")
    if request.headers.get("a2a-version", "").strip() != "1.0":
        return None, err(400, "VERSION_NOT_SUPPORTED", "Unsupported protocol version")
    if not accept_ok(request):
        return None, err(406, "NOT_ACCEPTABLE", "Unsupported media type requested")
    if body:
        ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ct not in (config.A2A_MT, "application/json"):
            return None, err(415, "UNSUPPORTED_MEDIA_TYPE", "Unsupported content type")
    return hashlib.sha256(token.encode("utf-8")).hexdigest(), None


class PathFix:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            p = re.sub(r"/{2,}", "/", scope["path"])
            if len(p) > 1 and p.endswith("/"):
                p = p.rstrip("/")
            scope["path"] = p
            scope["raw_path"] = p.encode()
        await self.app(scope, receive, send)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(PathFix)
router = APIRouter()


@app.exception_handler(StarletteHTTPException)
async def _http_err(request, exc):
    return err(exc.status_code, "ERROR", "Request failed")


@app.exception_handler(Exception)
async def _any_err(request, exc):
    print("ERROR", repr(exc))
    return err(500, "INTERNAL", "Internal error")


@app.get("/.well-known/agent-card.json")
async def agent_card(request: Request):
    base = config.PUBLIC_BASE_URL
    if not base:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host"))
        base = f"{proto}://{host}/a2a/"
    card = {
        "name": "Invoice Action Agent",
        "description": "Reads invoice packages, proposes one evidence-backed business action per invoice, and records receipts only for approved actions.",
        "version": "1.0.0",
        "supportedInterfaces": [{"url": base, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": [config.BATCH_MT],
        "defaultOutputModes": [config.PROPOSALS_MT, config.RECEIPTS_MT],
        "securitySchemes": {"bearerAuth": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        "securityRequirements": [{"schemes": {"bearerAuth": {"list": []}}}],
        "skills": [{
            "id": "invoice_action_agent",
            "name": "Invoice action agent",
            "description": "Chooses settle, approval, hold, duplicate-reject or exception actions for invoice packages and cites decisive evidence.",
            "tags": ["invoice", "accounts-payable", "approval", "duplicate-detection"],
            "inputModes": [config.BATCH_MT],
            "outputModes": [config.PROPOSALS_MT, config.RECEIPTS_MT],
        }],
    }
    return Response(json.dumps(card), media_type="application/json", headers={"Cache-Control": "no-store"})


async def new_task(principal, msg, mid, h):
    batch = next((p["data"] for p in msg["parts"]
                  if isinstance(p, dict) and p.get("mediaType") == config.BATCH_MT and isinstance(p.get("data"), dict)), None)
    if not batch or batch.get("batchId") is None or not isinstance(batch.get("packages"), list) or not batch["packages"]:
        return invalid("Invalid invoice batch")
    pkgs = batch["packages"]
    if not all(isinstance(p, dict) for p in pkgs):
        return invalid("Invalid invoice batch")
    ids = [ai.pkg_id(p, i) for i, p in enumerate(pkgs)]
    if len(set(ids)) != len(ids):
        return invalid("Duplicate package ids")

    tid = str(uuid.uuid4())
    cid = msg.get("contextId") if isinstance(msg.get("contextId"), str) and msg.get("contextId") else str(uuid.uuid4())
    task = {
        "id": tid, "contextId": cid,
        "status": {"state": WORKING, "timestamp": now()},
        "artifacts": [],
        "history": [{**msg, "taskId": tid, "contextId": cid}],
    }
    store.commit_step(principal, task, mid, h)
    store.dump_batch(batch)

    try:
        decisions = await ai.decide(pkgs, time.monotonic() + 38)
    except Exception as e:
        print("decide failed:", repr(e))
        store.delete_task(principal, tid, mid)
        return err(503, "UNAVAILABLE", "Temporarily unavailable")

    proposals = []
    for pid, d in zip(ids, decisions):
        refs = d["evidenceRefs"]
        proposals.append({
            "packageId": pid,
            "actionId": "act-" + uuid.uuid4().hex,
            "action": d["action"],
            "facts": d["facts"],
            "evidenceRefs": [f"[{r}]" for r in refs] if config.KEEP_BRACKETS else refs,
            "rationale": ai.rationale(d),
        })

    cur = store.get_task(principal, tid)
    if cur["status"]["state"] != WORKING:
        return ok({"task": cur})
    cur["artifacts"] = [{
        "artifactId": "proposals-" + tid,
        "name": "invoice-action-proposals",
        "parts": [{"mediaType": config.PROPOSALS_MT, "data": {"batchId": batch["batchId"], "proposals": proposals}}],
    }]
    cur["status"] = {"state": INPUT, "timestamp": now()}
    store.save_task(principal, cur)
    return ok({"task": cur})


def continue_task(principal, msg, mid, h):
    tid = msg.get("taskId")
    t = store.get_task(principal, tid) if isinstance(tid, str) else None
    if not t:
        return err(404, "NOT_FOUND", "Task not found")
    state = t["status"]["state"]
    if state in TERMINAL:
        return err(409, "FAILED_PRECONDITION", "Task is already finished", "TASK_TERMINAL")
    if state != INPUT:
        return err(409, "FAILED_PRECONDITION", "Task is not waiting for results", "TASK_NOT_READY")
    if msg.get("contextId") != t["contextId"]:
        return invalid("Context mismatch")
    part = next((p for p in msg["parts"]
                 if isinstance(p, dict) and p.get("mediaType") == config.RESULTS_MT and isinstance(p.get("data"), dict)), None)
    if not part:
        return invalid("Missing results")
    data = part["data"]
    stored = t["artifacts"][0]["parts"][0]["data"]
    if data.get("batchId") != stored["batchId"] or not isinstance(data.get("results"), list):
        return invalid("Batch mismatch")

    props = {p["packageId"]: p for p in stored["proposals"]}
    seen, nonces = set(), {}
    for r in data["results"]:
        pid = r.get("packageId") if isinstance(r, dict) else None
        p = props.get(pid) if isinstance(pid, str) else None
        if (p is None or pid in seen or r.get("actionId") != p["actionId"]
                or r.get("action") != p["action"] or r.get("outcome") not in ("ACCEPTED", "REJECTED")):
            return invalid("Result mismatch")
        seen.add(pid)
        if r["outcome"] == "ACCEPTED":
            n = r.get("receiptNonce")
            if not isinstance(n, str) or not n.strip():
                return invalid("Missing receipt")
            nonces[pid] = n

    executions = [{
        "packageId": pid, "actionId": p["actionId"], "action": p["action"],
        "receiptNonce": nonces[pid], "facts": p["facts"], "evidenceRefs": p["evidenceRefs"],
    } for pid, p in props.items() if pid in nonces]

    t["history"].append(msg)
    t["artifacts"].append({
        "artifactId": "receipts-" + t["id"],
        "name": "invoice-action-receipts",
        "parts": [{"mediaType": config.RECEIPTS_MT, "data": {"batchId": stored["batchId"], "executions": executions}}],
    })
    t["status"] = {"state": DONE, "timestamp": now()}
    store.commit_step(principal, t, mid, h)
    return ok({"task": t})


@router.post("/message:send")
async def message_send(request: Request):
    principal, bad = pre(request, body=True)
    if bad:
        return bad
    try:
        body = json.loads(await request.body())
    except Exception:
        return invalid("Malformed JSON")
    msg = body.get("message") if isinstance(body, dict) else None
    if (not isinstance(msg, dict) or not isinstance(msg.get("messageId"), str) or not msg["messageId"]
            or msg.get("role") != "ROLE_USER" or not isinstance(msg.get("parts"), list)):
        return invalid("Invalid message")
    mid, h = msg["messageId"], sha(msg)
    async with locks.setdefault((principal, mid), asyncio.Lock()):
        prior = store.get_message(principal, mid)
        if prior:
            if prior["hash"] != h:
                return err(409, "ABORTED", "IDEMPOTENCY_CONFLICT: message id reused with different content", "IDEMPOTENCY_CONFLICT")
            t = store.get_task(principal, prior["task_id"])
            return ok({"task": t}) if t else err(404, "NOT_FOUND", "Task not found")
        if msg.get("taskId") is not None:
            return continue_task(principal, msg, mid, h)
        return await new_task(principal, msg, mid, h)


@router.get("/tasks")
async def list_tasks(request: Request):
    principal, bad = pre(request)
    if bad:
        return bad
    try:
        size = max(1, min(int(request.query_params.get("pageSize", 50)), 100))
    except ValueError:
        size = 50
    out, used = [], 0
    for t in store.list_tasks(principal):
        slim = {k: v for k, v in t.items() if k != "history"}
        n = len(json.dumps(slim, ensure_ascii=False))
        if len(out) >= size or used + n > 380_000:
            break
        out.append(slim)
        used += n
    return ok({"tasks": out})


@router.get("/tasks/{task_id}")
async def read_task(task_id: str, request: Request):
    principal, bad = pre(request)
    if bad:
        return bad
    t = store.get_task(principal, task_id)
    return ok(t) if t else err(404, "NOT_FOUND", "Task not found")


@router.post("/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, request: Request):
    principal, bad = pre(request)
    if bad:
        return bad
    t = store.get_task(principal, task_id)
    if not t:
        return err(404, "NOT_FOUND", "Task not found")
    if t["status"]["state"] in TERMINAL:
        return err(409, "FAILED_PRECONDITION", "Task cannot be canceled", "TASK_NOT_CANCELABLE")
    t["status"] = {"state": CANCELED, "timestamp": now()}
    store.save_task(principal, t)
    return ok(t)


app.include_router(router, prefix="/a2a")
app.include_router(router)