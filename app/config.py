import os
from dotenv import load_dotenv

load_dotenv()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/agent.db")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_REASONING = os.getenv("LLM_REASONING", "").strip()
KEEP_BRACKETS = os.getenv("KEEP_BRACKETS", "0") == "1"

A2A_MT = "application/a2a+json"
BATCH_MT = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSALS_MT = "application/vnd.ga5.invoice-action-proposals+json"
RESULTS_MT = "application/vnd.ga5.invoice-action-results+json"
RECEIPTS_MT = "application/vnd.ga5.invoice-action-receipts+json"

ACTIONS = ("settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception")
