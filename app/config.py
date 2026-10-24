import os
from dotenv import load_dotenv

load_dotenv()


def _env(name, default=""):
    v = os.getenv(name, "").strip().strip("\"'").strip()
    return v or default


PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL")
DB_PATH = _env("DB_PATH", "data/agent.db")
LLM_BASE_URL = _env("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
if not LLM_BASE_URL.startswith(("http://", "https://")):
    LLM_BASE_URL = "https://" + LLM_BASE_URL
LLM_API_KEY = _env("LLM_API_KEY")
LLM_MODEL = _env("LLM_MODEL", "gemini-3.1-flash-lite")
LLM_REASONING = os.getenv("LLM_REASONING", "low").strip().strip("\"'").strip()
KEEP_BRACKETS = os.getenv("KEEP_BRACKETS", "0").strip() == "1"

A2A_MT = "application/a2a+json"
BATCH_MT = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSALS_MT = "application/vnd.ga5.invoice-action-proposals+json"
RESULTS_MT = "application/vnd.ga5.invoice-action-results+json"
RECEIPTS_MT = "application/vnd.ga5.invoice-action-receipts+json"

ACTIONS = ("settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception")
