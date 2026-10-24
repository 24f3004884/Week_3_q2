import json
import sys
import time
import uuid
from urllib.parse import urlsplit

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/a2a/").rstrip("/") + "/"
u = urlsplit(BASE)
ORIGIN = f"{u.scheme}://{u.netloc}"

A2A = "application/a2a+json"
BATCH_MT = "application/vnd.ga5.invoice-claim-batch+json"
RESULTS_MT = "application/vnd.ga5.invoice-action-results+json"
PROPOSALS_MT = "application/vnd.ga5.invoice-action-proposals+json"
RECEIPTS_MT = "application/vnd.ga5.invoice-action-receipts+json"
ACTIONS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}

ALICE = "alice-" + uuid.uuid4().hex
BOB = "bob-" + uuid.uuid4().hex
c = httpx.Client(timeout=60)
passed = []


def H(tok, ver="1.0"):
    return {"Authorization": f"Bearer {tok}", "A2A-Version": ver, "Content-Type": A2A, "Accept": A2A}


def check(name, cond, info=""):
    passed.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond or not info else "   -> " + str(info)[:300]))
    return bool(cond)


def packages():
    return [
        {"packageId": "p1", "vendor": "Acme Supplies",
         "documents": ["Cover sheet ref [COVER-1].",
                       "Archive example: an old invoice was held. [OLD-1]",
                       "Current finding: Acme Supplies invoice INV-1001 for INR 1,200.00 matches the purchase order "
                       "and goods receipt and is inside the clerk's limit. [PO-77] [GRN-77] [INV-1001-OK] Settle it."]},
        {"packageId": "p2", "vendor": "Globex Ltd",
         "documents": ["Cover sheet ref [COVER-2].",
                       "Current finding: Globex Ltd invoice INV-2002 for INR 950,000.00 is valid but above the "
                       "manager's approval limit; director approval is required. [LIM-9] [DIR-3] [INV-2002-HI]"]},
    ]


def batch_msg(batch_id):
    mid = "m-" + uuid.uuid4().hex
    msg = {"messageId": mid, "role": "ROLE_USER",
           "parts": [{"mediaType": BATCH_MT,
                      "data": {"batchId": batch_id, "policyRevision": "test-1", "packages": packages()}}]}
    body = {"message": msg, "configuration": {"returnImmediately": False, "historyLength": 20,
                                              "acceptedOutputModes": [PROPOSALS_MT, RECEIPTS_MT]}}
    return mid, msg, body


def send(tok, body):
    return c.post(BASE + "message:send", headers=H(tok), json=body)


def results_msg(task, outcomes, break_it=False):
    d = task["artifacts"][0]["parts"][0]["data"]
    res = [{"packageId": p["packageId"], "actionId": p["actionId"], "action": p["action"], "outcome": o,
            "receiptNonce": "nonce-" + uuid.uuid4().hex} for p, o in zip(d["proposals"], outcomes)]
    if break_it:
        res[0]["actionId"] = "wrong-action-id-0000"
    return {"message": {"messageId": "m-" + uuid.uuid4().hex, "taskId": task["id"], "contextId": task["contextId"],
                        "role": "ROLE_USER",
                        "parts": [{"mediaType": RESULTS_MT, "data": {"batchId": d["batchId"], "results": res}}]}}


print(f"Testing {BASE}\n")

# ---- Agent card ----
r = c.get(ORIGIN + "/.well-known/agent-card.json")
card = r.json() if r.status_code == 200 else {}
check("card: public, 200", r.status_code == 200, r.status_code)
check("card: name/description/version/capabilities",
      all(card.get(k) for k in ("name", "description", "version")) and isinstance(card.get("capabilities"), dict))
si = (card.get("supportedInterfaces") or [{}])[0]
check("card: interface url + binding",
      si.get("url") == BASE and si.get("protocolBinding") == "HTTP+JSON" and si.get("protocolVersion") == "1.0", si)
check("card: input/output modes",
      BATCH_MT in card.get("defaultInputModes", []) and PROPOSALS_MT in card.get("defaultOutputModes", [])
      and RECEIPTS_MT in card.get("defaultOutputModes", []))
sk = [s for s in card.get("skills", []) if s.get("id") == "invoice_action_agent"]
check("card: skill", bool(sk) and all(sk[0].get(k) for k in ("name", "description", "tags")))

# ---- Auth / version / media type ----
r = c.get(BASE + "tasks", headers={"A2A-Version": "1.0"})
check("auth: no token -> 401/403", r.status_code in (401, 403), r.status_code)
r = c.get(BASE + "tasks", headers=H(ALICE, "0.3"))
check("version: A2A-Version 0.3 -> 400", r.status_code == 400, r.status_code)
r = c.get(BASE + "tasks", headers=H(BOB))
check("list: new user gets empty list", r.status_code == 200 and r.json() == {"tasks": []}, r.text)
check("media type: a2a+json", A2A in r.headers.get("content-type", ""), r.headers.get("content-type"))

# ---- New task (calls the AI once) ----
mid, msg, body = batch_msg("batch-" + uuid.uuid4().hex[:8])
t0 = time.time()
r = send(ALICE, body)
dt = time.time() - t0
if not check("send: 200 (needs working AI key)", r.status_code == 200 and "task" in r.json(),
             f"{r.status_code} {r.text[:200]}  -- look for 'LLM error' in the uvicorn window"):
    print("\nStopped: fix the AI call first.")
    sys.exit(1)
task = r.json()["task"]
tid = task["id"]
parts = (task.get("artifacts") or [{}])[0].get("parts") or [{}]
props = parts[0].get("data", {}).get("proposals", [])
check("send: state INPUT_REQUIRED", task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", task["status"])
check("send: one artifact with one proposals part",
      len(task["artifacts"]) == 1 and len(parts) == 1 and parts[0].get("mediaType") == PROPOSALS_MT)
check("send: 2 proposals with valid actions", len(props) == 2 and all(p.get("action") in ACTIONS for p in props))
check("proposal: unique actionId >= 12 chars",
      len({p["actionId"] for p in props}) == len(props) and all(len(p["actionId"]) >= 12 for p in props))
check("proposal: facts complete",
      all(set(p["facts"]) == {"vendorName", "invoiceNumber", "amountMinor", "currency"}
          and isinstance(p["facts"]["amountMinor"], int) for p in props))
check("proposal: 3 evidenceRefs + rationale 60-1500 chars",
      all(len(p["evidenceRefs"]) == 3 and 60 <= len(p["rationale"]) <= 1500 for p in props))
check("history keeps the initial message", any(m.get("messageId") == mid for m in task["history"]))
print(f"\n  took {dt:.1f}s")
for p in props:
    print("  ", p["packageId"], p["action"], p["evidenceRefs"], p["facts"])
print()

# ---- Read / replay / conflict ----
r = c.get(BASE + f"tasks/{tid}", headers=H(ALICE))
check("read: GET task", r.status_code == 200 and r.json().get("id") == tid)

rev = dict(reversed(list(msg.items())))
t0 = time.time()
r = send(ALICE, {"message": rev, "configuration": {"returnImmediately": True}})
dt = time.time() - t0
check("replay: same task back, fast (no AI call)",
      r.status_code == 200 and r.json()["task"]["id"] == tid and dt < 3, f"{r.status_code} {dt:.1f}s")

chg = json.loads(json.dumps(msg))
chg["parts"][0]["data"]["batchId"] = "changed"
r = send(ALICE, {"message": chg})
check("conflict: same messageId, new content -> 409", r.status_code == 409 and "IDEMPOTENCY_CONFLICT" in r.text, r.status_code)

# ---- Isolation ----
r = c.get(BASE + f"tasks/{tid}", headers=H(BOB))
check("isolation: other user read -> 403/404", r.status_code in (403, 404) and tid not in r.text, r.status_code)
r = c.post(BASE + f"tasks/{tid}:cancel", headers=H(BOB))
check("isolation: other user cancel -> 403/404", r.status_code in (403, 404), r.status_code)
r = c.get(BASE + "tasks", headers=H(BOB))
check("isolation: other user list is empty", r.json() == {"tasks": []} and tid not in r.text)

# ---- Results continuation ----
r = send(ALICE, results_msg(task, ["ACCEPTED", "REJECTED"], break_it=True))
check("continue: wrong actionId -> 400", r.status_code == 400, r.status_code)
r = c.get(BASE + f"tasks/{tid}", headers=H(ALICE))
check("continue: bad attempt leaves task INPUT_REQUIRED", r.json()["status"]["state"] == "TASK_STATE_INPUT_REQUIRED")
r = send(BOB, results_msg(task, ["ACCEPTED", "ACCEPTED"]))
check("isolation: other user continue -> 403/404", r.status_code in (403, 404), r.status_code)

good = results_msg(task, ["ACCEPTED", "REJECTED"])
r = send(ALICE, good)
done = r.json().get("task", {}) if r.status_code == 200 else {}
check("continue: 200 + COMPLETED", done.get("status", {}).get("state") == "TASK_STATE_COMPLETED", r.text[:200])
rec = [p for a in done.get("artifacts", []) for p in a["parts"] if p.get("mediaType") == RECEIPTS_MT]
ex = rec[0]["data"]["executions"] if rec else []
want = good["message"]["parts"][0]["data"]["results"][0]
check("receipt: one receipts part, accepted result only",
      len(rec) == 1 and len(ex) == 1 and ex[0]["receiptNonce"] == want["receiptNonce"]
      and ex[0]["packageId"] == want["packageId"], ex)
check("receipt: proposals artifact still kept",
      any(p.get("mediaType") == PROPOSALS_MT for a in done.get("artifacts", []) for p in a["parts"]))
check("history has both messages", len(done.get("history", [])) == 2, len(done.get("history", [])))

# ---- Terminal behaviour ----
r = send(ALICE, good)
check("replay result message: same COMPLETED task",
      r.status_code == 200 and r.json()["task"]["status"]["state"] == "TASK_STATE_COMPLETED")
r = c.post(BASE + f"tasks/{tid}:cancel", headers=H(ALICE))
check("cancel after COMPLETED -> 409", r.status_code == 409, r.status_code)
r = c.get(BASE + f"tasks/{tid}", headers=H(ALICE))
check("completed task unchanged", r.json() == done)

# ---- Cancel flow + cache ----
_, _, body_c = batch_msg("batch-" + uuid.uuid4().hex[:8])
t0 = time.time()
r = send(ALICE, body_c)
dt = time.time() - t0
t2 = r.json().get("task", {"id": "none"})
check("second batch: AI decisions come from cache (fast)", r.status_code == 200 and dt < 5, f"{dt:.1f}s")
r = c.post(BASE + f"tasks/{t2['id']}:cancel", headers=H(ALICE))
check("cancel: INPUT_REQUIRED task -> 200 CANCELED",
      r.status_code == 200 and r.json()["status"]["state"] == "TASK_STATE_CANCELED", r.text[:200])
r = send(ALICE, results_msg(t2, ["ACCEPTED", "ACCEPTED"]))
check("results after cancel -> 409", r.status_code == 409, r.status_code)
r = c.get(BASE + "tasks", headers=H(ALICE))
check("list: Alice sees her 2 tasks", r.status_code == 200 and len(r.json()["tasks"]) == 2, r.text[:200])

print(f"\n{sum(passed)}/{len(passed)} passed")
sys.exit(0 if all(passed) else 1)