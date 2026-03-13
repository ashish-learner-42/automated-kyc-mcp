"""
KYC Onboarding Portal — Streamlit MCP Client

Uses FastMCP Client over stdio protocol to communicate with the MCP server.
Each tool call runs in a dedicated thread with its own event loop to avoid
conflicts with Streamlit's internal async runtime.

Sequential pipeline:
  1. Initialize Database     → enabled if DB not yet seeded
  2. Get Customer Status     → unlocked after DB initialized
  3. KYC Verification        → unlocked after status fetched
  4. Financial Verification  → unlocked after KYC passes
  5. Finalize Eligibility    → unlocked after Financial passes

OR: Execute Complete Pipeline (only if no sequential steps started)
"""

import asyncio
import json
import threading
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from fastmcp import Client

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT    = Path(__file__).parent.parent.parent.resolve()          # automated-kyc-mcp/
MCP_SERVER_PATH = PROJECT_ROOT / "src" / "main" / "server" / "mcpserver.py"
DB_PATH         = PROJECT_ROOT / "kyc_database.db"                       # must match Database.py

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 14-day re-check window
#
# Once KYC, Financial, or Eligibility verification is completed for a customer
# it cannot be re-run for RECHECK_DAYS days.  This prevents duplicate
# processing and mirrors a real-world compliance cooling-off period.
#
# Timestamps are stored as SQLite CURRENT_TIMESTAMP strings (UTC,
# "YYYY-MM-DD HH:MM:SS").  NULL means the step was never run, so the lock
# does not apply and the step is freely available.
#
# To bypass for testing: delete kyc_database.db — all customers will be
# treated as new and no timestamps will exist.
# ---------------------------------------------------------------------------
RECHECK_DAYS = 14


def _is_recently_verified(ts_str: str | None) -> bool:
    """Return True if ts_str falls within the last RECHECK_DAYS days (step is locked)."""
    if not ts_str:
        return False
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400 < RECHECK_DAYS
    except Exception:
        return False


def _verified_date(ts_str: str | None) -> str:
    """Human-readable date of last verification, e.g. '14 Mar 2026'."""
    if not ts_str:
        return "N/A"
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y")
    except Exception:
        return "N/A"


def _recheck_date(ts_str: str | None) -> str:
    """Human-readable date when re-verification becomes available."""
    if not ts_str:
        return "N/A"
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (ts + timedelta(days=RECHECK_DAYS)).strftime("%d %b %Y")
    except Exception:
        return "N/A"

# ---------------------------------------------------------------------------
# Page config — must be the first Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="KYC Onboarding Portal",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .tool-name  { font-weight: 700; font-size: 0.95rem; color: #212529; }
    .tool-desc  { font-size: 0.8rem; color: #6c757d; margin-top: 2px; }
    .result-box {
        background: #f8f9fa;
        border-radius: 6px;
        padding: 0.55rem 0.9rem;
        margin-top: 0.4rem;
        font-size: 0.85rem;
    }
    .status-pill {
        display: inline-block;
        padding: 2px 11px;
        border-radius: 12px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .pill-green  { background: #d4edda; color: #155724; }
    .pill-red    { background: #f8d7da; color: #721c24; }
    .pill-yellow { background: #fff3cd; color: #856404; }
    .pill-grey   { background: #e2e3e5; color: #383d41; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# DB pre-flight check
#
# Purpose: avoid forcing the user to click "Initialize Database" every time
# they open the app, even when the database already exists from a previous
# session.
#
# How it works:
#   • Defined here (before session state) so it can be called during init.
#   • Runs once per browser session, guarded by "db_checked" in session_state.
#   • Opens the SQLite file directly — no MCP server spin-up needed for this
#     lightweight read-only probe.
#   • If the customers table already has rows, a synthetic success result is
#     injected for the "init" step so the pipeline treats Step 1 as done and
#     immediately unlocks Step 2 (Get Customer Status).
#   • Any error (file missing, table missing, corrupt DB) is silently ignored;
#     the user will simply see the "Initialize Database" button as usual.
# ---------------------------------------------------------------------------
import sqlite3 as _sqlite3


def _check_db_already_initialized() -> bool:
    """
    Return True if kyc_database.db exists and already contains at least one
    customer row.  This is a lightweight read-only probe — no writes, no MCP.
    """
    try:
        if not DB_PATH.exists():
            return False
        con = _sqlite3.connect(str(DB_PATH))
        cur = con.execute("SELECT COUNT(*) FROM customers")
        count = cur.fetchone()[0]
        con.close()
        return count > 0
    except Exception:
        # Table may not exist yet, or the file is corrupt — treat as not ready.
        return False


def _count_customers_by_name(customer_name: str) -> int:
    """
    Return the number of DB rows whose name matches customer_name.
    Returns 0 if the DB is unavailable or the name is blank.
    Used to detect duplicate names so the UI can ask for PAN disambiguation.
    """
    try:
        if not DB_PATH.exists():
            return 0
        con = _sqlite3.connect(str(DB_PATH))
        cur = con.execute("SELECT COUNT(*) FROM customers WHERE name=?", (customer_name,))
        count = cur.fetchone()[0]
        con.close()
        return count
    except Exception:
        return 0


def _probe_customer_status(customer_name: str, pan_number: str = None) -> dict | None:
    """
    Directly query the DB for a customer's current status and timestamps.

    Returns a pipeline_results["status"]-compatible dict if the customer is
    found, or None if not found / DB unavailable.

    When pan_number is provided the query uses a composite name+PAN filter so
    the correct row is returned even when multiple customers share a name.

    This is called whenever the customer name or PAN changes so that the
    pipeline state is immediately populated without requiring the user to click
    'Get Customer Status' on every session.  The timestamps drive the 14-day
    re-check window for steps 3–5.
    """
    try:
        if not DB_PATH.exists():
            return None
        con = _sqlite3.connect(str(DB_PATH))
        if pan_number:
            cur = con.execute(
                """SELECT kyc_status, financial_status, eligibility_status,
                          kyc_verified_at, financial_verified_at, eligibility_verified_at
                   FROM customers WHERE name=? AND pan=?""",
                (customer_name, pan_number),
            )
        else:
            cur = con.execute(
                """SELECT kyc_status, financial_status, eligibility_status,
                          kyc_verified_at, financial_verified_at, eligibility_verified_at
                   FROM customers WHERE name=?""",
                (customer_name,),
            )
        row = cur.fetchone()
        con.close()
        if row is None:
            return None
        return {
            "success": True,
            "step":    "status",
            "data": {
                "kyc_status":              row[0],
                "financial_status":        row[1],
                "eligibility_status":      row[2],
                "kyc_verified_at":         row[3],
                "financial_verified_at":   row[4],
                "eligibility_verified_at": row[5],
            },
            "message": f"Status loaded from database for {customer_name}.",
        }
    except Exception:
        # DB unavailable or schema mismatch — user can still run Step 2 manually.
        return None


# ---------------------------------------------------------------------------
# Session state
#
# Streamlit re-runs the entire script on every user interaction, but
# session_state persists across re-runs within the same browser session.
# Each key is initialised only once (when absent) so existing values are
# never overwritten by subsequent re-renders.
# ---------------------------------------------------------------------------
_defaults = {
    "pipeline_results":   {},
    "last_customer":      "",
    "last_pan":           "",   # tracks PAN across re-renders for duplicate-name detection
    "sequential_started": False,
    "last_run_time":      None,
    # Tracks whether we have already run the DB pre-flight probe this session.
    # Without this guard the probe would fire on every Streamlit re-render.
    "db_checked":         False,
}
for _k, _v in _defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# Pre-flight DB check — runs exactly once per browser session.
# If the database is already populated from a previous session, inject a
# synthetic "init done" result so the user lands directly on Step 2.
if not st.session_state.db_checked:
    if _check_db_already_initialized():
        st.session_state.pipeline_results["init"] = {
            "success": True,
            "step":    "init",
            "data":    {"customers_seeded": 0},   # 0 = pre-existing rows, not freshly seeded
            "message": "Database already initialised from a previous session.",
        }
    st.session_state.db_checked = True  # never probe again this session

# ---------------------------------------------------------------------------
# MCP Client
# ---------------------------------------------------------------------------
def run_mcp_tool(tool_name: str, args: dict) -> dict:
    """
    Invoke an MCP tool via the FastMCP Client (stdio transport).

    Runs the async client in a daemon thread with its own event loop to avoid
    two problems:
      1. Streamlit's internal async runtime conflicts with a second event loop
         running on the same thread.
      2. Non-daemon threads (used by ThreadPoolExecutor) block Ctrl+C on
         Windows because the OS does not propagate SIGINT to child processes.
         A daemon thread is killed automatically when the main process exits,
         so Ctrl+C shuts down immediately without waiting for the MCP
         subprocess to finish.
    """
    async def _invoke() -> dict:
        async with Client(str(MCP_SERVER_PATH)) as client:
            result = await client.call_tool(tool_name, arguments=args)
            # FastMCP 3.x returns a CallToolResult; prefer structured_content (already a dict)
            if result.structured_content:
                return result.structured_content
            # Fallback: parse first TextContent block
            if result.content and hasattr(result.content[0], "text"):
                return json.loads(result.content[0].text)
            return {
                "success": False, "step": tool_name, "data": {},
                "message": "Server returned no content.",
            }

    # Mutable containers so the daemon thread can hand back a value or error.
    _result: list = []
    _error:  list = []

    def _run_in_new_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            _result.append(loop.run_until_complete(_invoke()))
        except Exception as exc:  # noqa: BLE001
            _error.append(exc)
        finally:
            loop.close()

    # daemon=True — thread is killed immediately when the main process exits
    # (e.g. on Ctrl+C), so the app shuts down without waiting 35 seconds.
    t = threading.Thread(target=_run_in_new_loop, daemon=True)
    t.start()
    t.join(timeout=35)

    try:
        if t.is_alive():
            return {"success": False, "step": tool_name, "data": {},
                    "message": "Tool timed out after 35 seconds."}
        if _error:
            raise _error[0]
        return _result[0]
    except json.JSONDecodeError as exc:
        return {"success": False, "step": tool_name, "data": {},
                "message": f"Could not parse server response: {exc}"}
    except Exception as exc:
        logger.exception("MCP tool call failed: %s", tool_name)
        return {"success": False, "step": tool_name, "data": {},
                "message": f"MCP client error: {exc}"}

# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def result_pill(result: dict) -> str:
    if result.get("success"):
        return '<span class="status-pill pill-green">✓ Success</span>'
    if "skipped" in result.get("message", "").lower():
        return '<span class="status-pill pill-yellow">⏸ Skipped</span>'
    return '<span class="status-pill pill-red">✗ Failed</span>'


def show_result(result: dict):
    """Render result message and key data fields below a tool row."""
    if not result:
        return
    st.markdown(
        f'<div class="result-box">'
        f'{result_pill(result)}&nbsp; {result.get("message", "")}'
        f'</div>',
        unsafe_allow_html=True,
    )
    display_data = {
        k: v for k, v in result.get("data", {}).items()
        if not isinstance(v, list) and v not in (None, "skipped", "error")
    }
    if display_data:
        cols = st.columns(len(display_data))
        for col, (k, v) in zip(cols, display_data.items()):
            label = k.replace("_", " ").title()
            value = f"{v:.0%}" if isinstance(v, float) and "ratio" in k else str(v)
            col.metric(label, value)
    if result.get("data", {}).get("failed_rules"):
        st.warning("Failed rules: " + ", ".join(result["data"]["failed_rules"]))


# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------
st.title("🛡️ KYC Onboarding Portal")
st.caption("MCP Client — Sequential KYC Pipeline")
st.divider()

# ---------------------------------------------------------------------------
# Customer details
# ---------------------------------------------------------------------------
st.subheader("Customer Details")
col_name, _ = st.columns([4, 4])

with col_name:
    customer_name = st.text_input(
        "Customer Name *",
        placeholder="e.g. Bob Williams",
        help="Required. Must match the name in the KYC PDF exactly.",
    )

# Detect duplicate names so we can ask for PAN disambiguation.
# _name_count is checked once per render; it is a cheap indexed query.
_name_stripped = customer_name.strip()
_name_count    = _count_customers_by_name(_name_stripped) if _name_stripped else 0
_duplicate_name = _name_count > 1   # True → PAN required to identify the right record

# Show the PAN field only when there are multiple customers with this name.
pan_number = ""
if _duplicate_name:
    st.warning(
        f"⚠️ **{_name_count} customers** share the name **\"{_name_stripped}\"**. "
        "Please enter the PAN number to identify the correct record."
    )
    col_pan, _ = st.columns([4, 4])
    with col_pan:
        pan_number = st.text_input(
            "PAN Number *",
            placeholder="e.g. BWKYC9876P",
            help="Required when multiple customers share the same name.",
        )

# Reset pipeline when the customer name or (when relevant) the PAN changes.
# The "init" result is preserved because Initialize Database is tied to the
# database, not to any specific customer.
_name_changed = customer_name != st.session_state.last_customer
_pan_changed  = pan_number    != st.session_state.last_pan

if _name_changed or (_duplicate_name and _pan_changed):
    _init_result = st.session_state.pipeline_results.get("init")  # preserve across customer changes
    st.session_state.pipeline_results   = {}
    if _init_result:
        st.session_state.pipeline_results["init"] = _init_result

    # Pre-populate the status step directly from the DB so the user does not
    # have to click 'Get Customer Status' on every session.
    # When the name is ambiguous, only probe once the PAN is entered.
    _probe_pan = pan_number.strip() if _duplicate_name else None
    if not _duplicate_name or _probe_pan:
        _status_result = _probe_customer_status(_name_stripped, _probe_pan)
        if _status_result:
            st.session_state.pipeline_results["status"] = _status_result

    st.session_state.sequential_started = False
    st.session_state.last_run_time      = None
    st.session_state.last_customer      = customer_name
    st.session_state.last_pan           = pan_number

if not _name_stripped:
    st.info("Enter a customer name above to begin.")
    st.stop()

# If multiple customers share this name and PAN hasn't been provided yet,
# block the rest of the pipeline — we don't know which customer to process.
if _duplicate_name and not pan_number.strip():
    st.stop()

st.divider()

# ---------------------------------------------------------------------------
# Step enablement
# ---------------------------------------------------------------------------
results       = st.session_state.pipeline_results
init_done     = results.get("init",      {}).get("success", False)
status_done   = results.get("status",    {}).get("success", False)  # must succeed (customer found)
kyc_done      = results.get("kyc",       {}).get("success", False)  # succeeded this session
financial_done   = results.get("financial",  {}).get("success", False)
eligibility_done = results.get("eligibility", {}).get("success", False)

# 14-day re-check window — read timestamps returned by the status step.
# None timestamps (step never run) are treated as "not recently done" → unlocked.
status_data    = results.get("status", {}).get("data", {})
ts_kyc         = status_data.get("kyc_verified_at")
ts_financial   = status_data.get("financial_verified_at")
ts_eligibility = status_data.get("eligibility_verified_at")

kyc_recently_done         = _is_recently_verified(ts_kyc)
financial_recently_done   = _is_recently_verified(ts_financial)
eligibility_recently_done = _is_recently_verified(ts_eligibility)

# A step is "satisfied" if completed this session OR within the recheck window.
# Satisfied steps are treated as Done for unlocking the next step.
kyc_satisfied         = kyc_done or kyc_recently_done
financial_satisfied   = financial_done or financial_recently_done
eligibility_satisfied = eligibility_done or eligibility_recently_done

# ---------------------------------------------------------------------------
# Pipeline tools — sequential
# ---------------------------------------------------------------------------
st.subheader("Pipeline Tools")

# Base args shared across all customer-specific tools.
# pan_number is included only when the user has entered it (duplicate-name case).
_base_args = {"customer_name": customer_name}
if pan_number.strip():
    _base_args["pan_number"] = pan_number.strip()

TOOL_DEFS = [
    {
        "key":         "init",
        "tool":        "init_all_db",
        "args":        {},
        "label":       "1 · Initialize Database",
        "description": "Create schema and seed test customers. Run once if the database is empty.",
        "enabled":     not init_done,
        "done":        init_done,
    },
    {
        "key":         "status",
        "tool":        "get_customer_status",
        "args":        _base_args,
        "label":       "2 · Get Customer Status",
        "description": "Fetch current KYC, financial, and eligibility status from the database.",
        "enabled":     init_done and not status_done,
        "done":        status_done,
    },
    {
        "key":   "kyc",
        "tool":  "perform_kyc_verification",
        # KYC always extracts and matches PAN from the PDF internally — no pan_number arg needed.
        "args":  {"customer_name": customer_name},
        "label": "3 · KYC Verification",
        # When locked, show when the step was last run and when it can be re-run.
        "description": (
            f"Verified on {_verified_date(ts_kyc)} — re-verification available from {_recheck_date(ts_kyc)}."
            if kyc_recently_done else
            "Extract name and PAN from KYC PDF and match against database records."
        ),
        "enabled": status_done and not kyc_satisfied,
        "done":    kyc_satisfied,
    },
    {
        "key":   "financial",
        "tool":  "verify_financial_status",
        "args":  _base_args,
        "label": "4 · Financial Verification",
        "description": (
            f"Verified on {_verified_date(ts_financial)} — re-verification available from {_recheck_date(ts_financial)}."
            if financial_recently_done else
            "Validate CIBIL score (> 700) and DTI ratio (< 20%) against eligibility rules."
        ),
        "enabled": status_done and kyc_satisfied and not financial_satisfied,
        "done":    financial_satisfied,
    },
    {
        "key":   "eligibility",
        "tool":  "finalize_account_eligibility",
        "args":  _base_args,
        "label": "5 · Finalize Eligibility",
        "description": (
            f"Determined on {_verified_date(ts_eligibility)} — re-determination available from {_recheck_date(ts_eligibility)}."
            if eligibility_recently_done else
            "Make final onboarding decision based on KYC and financial outcomes."
        ),
        "enabled": status_done and financial_satisfied and not eligibility_satisfied,
        "done":    eligibility_satisfied,
    },
]

for tool in TOOL_DEFS:
    key     = tool["key"]
    enabled = tool["enabled"]
    done    = tool["done"]
    result  = results.get(key)

    btn_col, info_col = st.columns([2, 8])

    with btn_col:
        clicked = st.button(
            label               = "✓ Done" if done else "▶ Run",
            key                 = f"btn_{key}",
            disabled            = not enabled,
            type                = "primary" if enabled else "secondary",
            use_container_width = True,
        )

    with info_col:
        st.markdown(
            f'<div class="tool-name">{tool["label"]}</div>'
            f'<div class="tool-desc">{tool["description"]}</div>',
            unsafe_allow_html=True,
        )

    if clicked and enabled:
        with st.spinner(f"Running — {tool['label']}…"):
            res = run_mcp_tool(tool["tool"], tool["args"])
        st.session_state.pipeline_results[key] = res
        st.session_state.sequential_started    = True
        st.session_state.last_run_time         = datetime.now()
        st.rerun()

    if result:
        show_result(result)

    st.write("")  # row spacing

# ---------------------------------------------------------------------------
# Complete pipeline — only available if no sequential steps taken
# ---------------------------------------------------------------------------
st.divider()

# Complete pipeline is locked if:
#   (a) the user already started sequential steps this session, OR
#   (b) any of steps 3-5 are within the 14-day recheck window
any_step_recently_done = kyc_recently_done or financial_recently_done or eligibility_recently_done
complete_locked = st.session_state.sequential_started or (status_done and any_step_recently_done)

st.subheader("Complete Pipeline")
if not complete_locked:
    complete_caption = "Runs all five steps automatically in sequence."
elif st.session_state.sequential_started:
    complete_caption = (
        "Sequential steps already in progress for this customer. "
        "Change the customer name to start a fresh complete run."
    )
else:
    locked_items = []
    if kyc_recently_done:
        locked_items.append(f"KYC (re-opens {_recheck_date(ts_kyc)})")
    if financial_recently_done:
        locked_items.append(f"Financial (re-opens {_recheck_date(ts_financial)})")
    if eligibility_recently_done:
        locked_items.append(f"Eligibility (re-opens {_recheck_date(ts_eligibility)})")
    complete_caption = (
        f"Cannot re-run within {RECHECK_DAYS} days of last verification: "
        + ", ".join(locked_items) + "."
    )
st.caption(complete_caption)

if st.button(
    "▶ Execute Complete Pipeline",
    type                = "primary",
    disabled            = complete_locked,
    use_container_width = False,
):
    pipeline_results = {}
    with st.status("Running complete KYC pipeline…", expanded=True) as status_bar:

        st.write("⚙️  Initializing database…")
        pipeline_results["init"] = run_mcp_tool("init_all_db", {})

        st.write(f"📋  Fetching status for **{customer_name}**…")
        pipeline_results["status"] = run_mcp_tool(
            "get_customer_status", _base_args
        )

        st.write("🔍  Running KYC verification…")
        # KYC extracts and matches PAN from the PDF internally — no pan_number needed.
        pipeline_results["kyc"] = run_mcp_tool(
            "perform_kyc_verification", {"customer_name": customer_name}
        )

        if pipeline_results["kyc"].get("success"):
            st.write("💳  Running financial verification…")
            pipeline_results["financial"] = run_mcp_tool(
                "verify_financial_status", _base_args
            )
        else:
            pipeline_results["financial"] = {
                "success": False, "step": "financial",
                "data":    {"financial_status": "skipped"},
                "message": "Skipped — KYC not verified.",
            }

        if pipeline_results["financial"].get("success"):
            st.write("✅  Finalizing eligibility…")
            pipeline_results["eligibility"] = run_mcp_tool(
                "finalize_account_eligibility", _base_args
            )
        else:
            pipeline_results["eligibility"] = {
                "success": False, "step": "eligibility",
                "data":    {"eligibility_status": "skipped"},
                "message": "Skipped — financial verification not passed.",
            }

        all_ok = all(r.get("success") for r in pipeline_results.values())
        status_bar.update(
            label    = "Pipeline complete ✓" if all_ok else "Pipeline completed with errors",
            state    = "complete" if all_ok else "error",
            expanded = False,
        )

    st.session_state.pipeline_results   = pipeline_results
    st.session_state.sequential_started = True
    st.session_state.last_run_time      = datetime.now()
    st.rerun()

# ---------------------------------------------------------------------------
# Final status — live from DB after any step
# ---------------------------------------------------------------------------
if bool(results):  # show live status panel once any pipeline step has been run
    st.divider()
    st.subheader("Current Status")

    status_result = run_mcp_tool("get_customer_status", {"customer_name": customer_name})

    if status_result.get("success"):
        d       = status_result["data"]
        kyc_val = d.get("kyc_status",         "—")
        fin_val = d.get("financial_status",   "—")
        eli_val = d.get("eligibility_status", "—")

        c1, c2, c3 = st.columns(3)
        c1.metric("KYC Status",         kyc_val)
        c2.metric("Financial Status",   fin_val)
        c3.metric("Eligibility Status", eli_val)

        if eli_val == "Eligible":
            st.success(f"🎉 **{customer_name}** is **Eligible** for onboarding.")
        elif eli_val == "Not Eligible":
            st.error(f"❌ **{customer_name}** is **Not Eligible** for onboarding.")
        elif kyc_val == "Verified" and "pending" in fin_val.lower():
            st.info("✅ KYC verified. Run Financial Verification to continue.")
        elif "pending" in kyc_val.lower():
            st.warning("⏳ KYC not yet verified. Run KYC Verification to continue.")
    else:
        st.warning(status_result.get("message", "Could not fetch status from database."))

    if st.session_state.last_run_time:
        st.caption(f"Last updated: {st.session_state.last_run_time.strftime('%Y-%m-%d %H:%M:%S')}")