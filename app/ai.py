import asyncio
import hashlib
import json
import re
import time

import httpx

from . import config, store

BR = re.compile(r"\[([^\[\]\n]{1,160})\]")
ID_KEYS = ("packageId", "package_id", "id")

SYSTEM = """You are a careful accounts-payable analyst. For EACH package choose exactly ONE action:
settle_invoice   - invoice is valid, reconciled (PO/receipt/amount agree) and within autonomous approval authority.
request_approval - commercially valid but outside delegated authority; a named approver must approve.
hold_invoice     - payment pauses until a stated verification (bank details, vendor check, receipt, tax id, etc.) completes.
reject_duplicate - the SAME commercial invoice (same vendor, invoice number, amount) was already paid. A payment for a DIFFERENT invoice is NOT a duplicate.
open_exception   - material records conflict (amount, vendor, PO, currency, dates) and need an exception workflow.

Reading rules:
- Lines are labelled L1, L2, ... Exactly one line is the DECISIVE paragraph: the current finding that determines the action; it contains three bracketed references such as [ABC-123].
- IGNORE the cover-sheet reference, archive/old examples, training decoys/sample cases, quoted or negated action words ("not a duplicate", "do not hold", "would settle only if"), and action words used in unrelated contexts.
- Decide from the real current facts, never from keywords alone.
- Facts describe the invoice itself: vendorName, invoiceNumber, currency (ISO code), amountMinor = invoice total as an INTEGER in minor units (1,234.50 -> 123450; zero-decimal currencies keep the whole amount).
- evidenceRefs = the three bracketed references from the decisive line, copied exactly, WITHOUT the square brackets.

Output ONLY JSON:
{"decisions":[{"packageId":"...","line":"L7","action":"...","evidenceRefs":["a","b","c"],"vendorName":"...","invoiceNumber":"...","amountMinor":12345,"currency":"INR","reason":"one sentence, max 30 words"}]}
"""


def pkg_id(p, i):
    for k in ID_KEYS:
        v = p.get(k)
        if isinstance(v, (str, int)) and str(v):
            return str(v)
    return f"pkg-{i + 1}"


def content_key(p):
    q = {k: v for k, v in p.items() if k not in ID_KEYS}
    s = json.dumps(q, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def chunks(p):
    out = []

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                if path == "" and k in ID_KEYS:
                    continue
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{path}.{i}")
        elif isinstance(x, str):
            for part in re.split(r"\n\s*\n", x):
                if part.strip():
                    out.append((path, part.strip()))
        elif x is not None:
            out.append((path, str(x)))

    walk(p, "")
    return out


def render(pid, p):
    ls = chunks(p)
    text = f"=== PACKAGE {pid} ===\n" + "\n".join(f"L{n} <{path}> {t}" for n, (path, t) in enumerate(ls, 1))
    allrefs = set(BR.findall("\n".join(t for _, t in ls)))
    return text, ls, allrefs


def resolve(d, ls, allrefs):
    action = d.get("action")
    if action not in config.ACTIONS:
        return None
    chunk_refs = []
    m = re.fullmatch(r"L?(\d+)", str(d.get("line", "")).strip())
    if m and 1 <= int(m.group(1)) <= len(ls):
        chunk_refs = uniq(BR.findall(ls[int(m.group(1)) - 1][1]))
    raw = d.get("evidenceRefs") if isinstance(d.get("evidenceRefs"), list) else []
    model_refs = uniq([r for r in (str(x).strip().strip("[]").strip() for x in raw) if r in allrefs])
    if len(chunk_refs) == 3:
        refs = chunk_refs
    elif len(model_refs) >= 3:
        refs = model_refs[:3]
    elif chunk_refs:
        refs = chunk_refs[:3]
    else:
        refs = model_refs
    if not refs:
        return None
    try:
        amount = int(round(float(str(d.get("amountMinor")).replace(",", ""))))
    except Exception:
        return None
    facts = {
        "vendorName": str(d.get("vendorName", "")).strip(),
        "invoiceNumber": str(d.get("invoiceNumber", "")).strip(),
        "amountMinor": amount,
        "currency": str(d.get("currency", "")).strip().upper(),
    }
    if not facts["vendorName"] or not facts["invoiceNumber"] or not facts["currency"]:
        return None
    return {"action": action, "facts": facts, "evidenceRefs": refs, "reason": str(d.get("reason", ""))}


def rationale(dec):
    f, refs = dec["facts"], dec["evidenceRefs"]
    why = " ".join(str(dec.get("reason", "")).split())[:600] or "The decisive paragraph supports this action."
    cites = ", ".join(f"[{r}]" for r in refs)
    return f"Action {dec['action']} for invoice {f['invoiceNumber']} from {f['vendorName']}: {why} Decisive evidence: {cites}."[:1500]


async def llm(user, timeout):
    body = {
        "model": config.LLM_MODEL,
        "temperature": 0,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    if config.LLM_REASONING:
        body["reasoning_effort"] = config.LLM_REASONING
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
        )
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"].strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
    return json.loads(txt)


async def decide(packages, deadline):
    out, pending = {}, []
    for i, p in enumerate(packages):
        key = content_key(p)
        cached = store.get_decision(key)
        if cached:
            out[i] = cached
        else:
            pending.append((i, pkg_id(p, i), key, p))

    for _ in range(3):
        if not pending:
            break
        left = deadline - time.monotonic()
        if left < 6:
            break
        views = [render(pid, p) for _, pid, _, p in pending]
        try:
            data = await llm("\n\n".join(v[0] for v in views), min(left - 3, 36))
            decs = [d for d in data.get("decisions", []) if isinstance(d, dict)]
        except Exception as e:
            print("LLM error:", repr(e))
            if "429" in repr(e):
                await asyncio.sleep(5)
            continue
        by_id = {str(d.get("packageId")): d for d in decs}
        still = []
        for k, ((i, pid, key, p), (_, ls, refs)) in enumerate(zip(pending, views)):
            d = by_id.get(pid) or (decs[k] if len(decs) == len(pending) else None)
            res = resolve(d, ls, refs) if d else None
            if res:
                store.put_decision(key, res)
                out[i] = res
            else:
                still.append((i, pid, key, p))
        pending = still

    if pending and not out:
        raise RuntimeError("model unavailable")
    for i, pid, key, p in pending:
        refs = uniq(BR.findall("\n".join(t for _, t in chunks(p))))[:3]
        out[i] = {
            "action": "open_exception",
            "facts": {"vendorName": "unknown", "invoiceNumber": "unknown", "amountMinor": 0, "currency": "INR"},
            "evidenceRefs": refs,
            "reason": "Automated extraction was inconclusive, so this package is routed to the exception workflow.",
        }
    return [out[i] for i in range(len(packages))]