import sys
import time
import uuid

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/a2a/").rstrip("/") + "/"
A2A = "application/a2a+json"
BATCH_MT = "application/vnd.ga5.invoice-claim-batch+json"
H = {"Authorization": "Bearer big-" + uuid.uuid4().hex, "A2A-Version": "1.0",
     "Content-Type": A2A, "Accept": A2A}

FILLER = ("Routine ledger note: vendor onboarding complete, remittance terms net 30, audit sample "
          "reviewed by the shared services team. An archive example from a prior quarter mentioned "
          "a hold and a duplicate, but those were old training decoys and do not apply. ")


def package(i):
    tag = uuid.uuid4().hex[:6].upper()
    docs = [f"Cover sheet reference [COV-{tag}]."]
    for k in range(4):
        docs.append(f"Section {k}: " + FILLER * 3 + f" Ref {uuid.uuid4().hex}.")
    docs.append(f"Archive example (do not use): an old invoice was settled. [OLD-{tag}]")
    docs.append(f"Current finding: Vendor V{i} invoice INV-{3000 + i} for INR {10000 + i * 137}.00 "
                f"matches the purchase order and receipt and is within authority. "
                f"[PO-{tag}] [GRN-{tag}] [OK-{tag}] Settle it.")
    return {"packageId": f"big-{i}", "vendor": f"Vendor V{i}", "documents": docs}


pkgs = [package(i) for i in range(12)]
chars = sum(len(str(p)) for p in pkgs)
print(f"Sending 12 packages, about {chars // 4:,} tokens, to {BASE}")

body = {"message": {"messageId": "m-" + uuid.uuid4().hex, "role": "ROLE_USER",
                    "parts": [{"mediaType": BATCH_MT, "data": {
                        "batchId": "big-" + uuid.uuid4().hex[:8], "policyRevision": "t1", "packages": pkgs}}]},
        "configuration": {"returnImmediately": False}}

t0 = time.time()
r = httpx.post(BASE + "message:send", headers=H, json=body, timeout=60)
dt = time.time() - t0
print(f"HTTP {r.status_code} in {dt:.1f}s")

if r.status_code != 200:
    print("FAIL: check the Render logs for the 'LLM error:' line.")
    sys.exit(1)

task = r.json()["task"]
props = task["artifacts"][0]["parts"][0]["data"]["proposals"]
print("state:", task["status"]["state"], "| proposals:", len(props))
for p in props:
    print(" ", p["packageId"], p["action"], p["evidenceRefs"])

ok = len(props) == 12 and dt < 35 and all(p["action"] == "settle_invoice" for p in props)
print("\nPASS: big batch works" if ok else "\nCHECK: fewer than 12 proposals, over 35s, or unexpected actions")