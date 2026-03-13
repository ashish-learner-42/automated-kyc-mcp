# automated-kyc-mcp
MCP stdio implementation for automated KYC application# KYC Onboarding Portal

An automated **Know Your Customer (KYC)** and customer onboarding eligibility system built with [Streamlit](https://streamlit.io) as the UI and [FastMCP](https://gofastmcp.com) as the backend protocol layer.

The application walks a compliance officer through a structured, five-step pipeline — database initialisation → status check → document verification → financial scoring → eligibility decision — with every step enforced sequentially so no stage can be skipped.

---

## Table of Contents

- [Architecture overview](#architecture-overview)
- [Project structure](#project-structure)
- [Class and method reference](#class-and-method-reference)
- [Requirements](#requirements)
- [Setup and run](#setup-and-run)
- [Adding and managing customer data](#adding-and-managing-customer-data)
- [Pipeline walkthrough](#pipeline-walkthrough)
- [Duplicate customer names](#duplicate-customer-names)
- [Example: Bob Williams](#example-bob-williams)
- [Logs](#logs)

---

## Architecture overview

```
┌─────────────────────────────────┐
│   Streamlit UI  (main.py)       │  ← browser-facing client
│   FastMCP Client (stdio)        │
└────────────┬────────────────────┘
             │  MCP protocol over stdio
             ▼
┌─────────────────────────────────┐
│   MCP Server  (mcpserver.py)    │  ← thin @mcp.tool wrappers only
└────────────┬────────────────────┘
             │  delegates 100% of logic
             ▼
┌─────────────────────────────────┐
│   Service layer                 │
│   CustomerService               │
│   KYCService                    │
│   FinancialService              │
│   EligibilityService            │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│   Database.py  (SQLite)         │
│   kyc_database.db               │
└─────────────────────────────────┘
```

**Key design decisions**

| Decision | Rationale |
|---|---|
| `@mcp.tool` methods contain zero business logic | Service layer stays testable in isolation without MCP |
| FastMCP `Client` over stdio | Proper MCP protocol — no subprocess hacks |
| `threading.Thread(daemon=True)` + new event loop per call | Avoids conflicts with Streamlit's async runtime; daemon threads allow clean Ctrl+C shutdown |
| File-based logging only | stdout is reserved exclusively for the MCP JSON protocol |
| `check_same_thread=False` on SQLite connection | MCP server calls tools in a worker thread; connection must be shareable |
| DB pre-flight check on session start | Skips "Initialize Database" when data already exists from a prior session |
| Per-customer DB probe on name change | Populates step statuses and timestamps immediately without requiring "Get Customer Status" click |
| 14-day re-check window | Prevents re-running KYC/Financial/Eligibility within 14 days of last verification |
| Composite name+PAN lookup | Correctly handles multiple customers who share the same name |

---

## Project structure

```
automated-kyc-mcp/
│
├── src/
│   └── main/
│       ├── main.py                      # Streamlit UI + FastMCP client
│       ├── Database.py                  # SQLite CRUD — single source of truth for schema
│       │
│       ├── Services/
│       │   ├── CustomerService.py       # DB init and status retrieval
│       │   ├── KYCService.py            # PDF extraction and identity matching
│       │   ├── FinancialService.py      # CIBIL and DTI rule evaluation
│       │   └── EligibilityService.py   # Final pass/fail decision
│       │
│       ├── Utils/
│       │   └── UtilityFunctions.py      # PDF helpers (find, validate, extract)
│       │
│       └── server/
│           └── mcpserver.py             # FastMCP server — @mcp.tool wrappers only
│
├── customer_kyc_pdfs/                   # One PDF per customer (see naming convention below)
│   ├── kyc_Bob_Williams.pdf
│   ├── kyc_Charlie_Davis.pdf
│   └── kyc_Diana_Prince.pdf
│
├── kyc_database.db                      # SQLite database (auto-created on first run)
├── kyc_app.log                          # Rotating log file written by the MCP server
└── requirements.txt
```

---

## Class and method reference

### `Database` — `src/main/Database.py`

Single SQLite connection object shared across all services.

| Method | Purpose |
|---|---|
| `__init__()` | Opens `kyc_database.db` at project root |
| `init_tables()` | Creates schema, runs non-destructive column migrations, seeds test customers |
| `fetch_customer_kyc_status_by_name_pan(name, pan)` | Returns `(id, kyc_status)` for a name+PAN pair |
| `update_kyc_status(customer_id, status)` | Writes KYC result and timestamp to DB |
| `get_financial_data(customer_id)` | Returns `{"cibil": int, "dti": float}` or `None` if data not yet populated |
| `update_financial_status(customer_id, status)` | Writes financial verification result and timestamp to DB |
| `update_eligibility_status(customer_id, status)` | Writes final eligibility decision and timestamp to DB |
| `get_eligibility_status(customer_id)` | Returns `(kyc_status, financial_status)` for eligibility check |
| `get_customer_full_status(customer_name)` | Returns 6-tuple `(kyc_status, financial_status, eligibility_status, kyc_verified_at, financial_verified_at, eligibility_verified_at)` by name |
| `get_customer_full_status_by_pan(customer_name, pan)` | Same 6-tuple, looked up by **name + PAN** — used when multiple customers share a name |
| `count_customers_by_name(customer_name)` | Returns how many DB rows share a given name — drives the duplicate-name detection in the UI |
| `get_customer_id_by_name_pan(name, pan)` | Returns `(id,)` for a name+PAN pair, or `None` — used by Financial and Eligibility services when PAN is provided |

---

### `CustomerService` — `src/main/Services/CustomerService.py`

Handles database lifecycle and status queries.

| Method | Purpose |
|---|---|
| `initialize_database()` | Calls `db.init_tables()` to ensure schema and seed data are present |
| `get_customer_status(customer_name, pan_number=None)` | Returns current KYC, financial, and eligibility status. When `pan_number` is provided, uses composite name+PAN lookup to disambiguate duplicate names |

---

### `KYCService` — `src/main/Services/KYCService.py`

Document verification — finds the PDF, extracts identity fields, matches against the database.

| Method | Purpose |
|---|---|
| `perform_kyc_verification(customer_name)` | Resolve PDF → extract name+PAN → DB match → update `kyc_status` to "Verified" |

> **Note:** KYC does not accept a `pan_number` argument from the UI. It always extracts the PAN directly from the PDF and uses that for the DB match. This means the PDF itself is the source of truth for identity — the UI PAN field is not involved in KYC.

---

### `FinancialService` — `src/main/Services/FinancialService.py`

Applies eligibility rules against CIBIL score and DTI ratio stored in the database.

| Method | Purpose |
|---|---|
| `perform_financial_verification(customer_name, pan_number=None)` | Reads `cibil_score`/`dti_ratio` from DB, evaluates rules, updates `financial_status`. Uses composite name+PAN lookup when `pan_number` is supplied |

**Thresholds** (module-level constants):

```
CIBIL_THRESHOLD = 700     # score must be strictly greater than 700
DTI_THRESHOLD   = 0.20    # ratio must be strictly less than 20%
```

---

### `EligibilityService` — `src/main/Services/EligibilityService.py`

Makes the final onboarding decision.

| Method | Purpose |
|---|---|
| `determine_final_eligibility(customer_name, pan_number=None)` | Customer is Eligible only if `kyc_status == "Verified"` AND `"Pass" in financial_status`. Uses composite name+PAN lookup when `pan_number` is supplied |

---

### `UtilityFunctions` — `src/main/Utils/UtilityFunctions.py`

PDF helpers used by `KYCService`.

| Method | Purpose |
|---|---|
| `find_expected_file(customer_name)` | Resolves expected PDF path; falls back to case-insensitive scan |
| `check_file_path(pdf_path)` | Returns path if it exists on disk, else `None` |
| `extract_data_from_kyc_pdf(pdf_path)` | Uses `pypdf` to extract `name` and `pan` fields via regex |

---

### `mcpserver.py` — `src/main/server/mcpserver.py`

Registers all five MCP tools. Each is a one-line delegation — no business logic lives here.

| Tool | Signature | Delegates to |
|---|---|---|
| `init_all_db` | `()` | `CustomerService.initialize_database()` |
| `get_customer_status` | `(customer_name, pan_number=None)` | `CustomerService.get_customer_status()` |
| `perform_kyc_verification` | `(customer_name)` | `KYCService.perform_kyc_verification()` |
| `verify_financial_status` | `(customer_name, pan_number=None)` | `FinancialService.perform_financial_verification()` |
| `finalize_account_eligibility` | `(customer_name, pan_number=None)` | `EligibilityService.determine_final_eligibility()` |

---

## Requirements

- Python 3.10 or later
- Packages listed in `requirements.txt`:

```
fastmcp>=0.1.0
pypdf>=3.0.0
streamlit>=1.28.0
pandas>=2.0.0
```

---

## Setup and run

```bash
# 1. Clone the repository
git clone <repo-url>
cd automated-kyc-mcp

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch the Streamlit UI
streamlit run src/main/main.py
```

The MCP server (`mcpserver.py`) is started automatically by the Streamlit app when a tool is invoked — you do **not** need to start it manually.

---

## Adding and managing customer data

### Step 1 — Add a customer record to the database

The database stores each customer with a unique `pan`. `name` does not need to be unique — the system handles duplicate names via PAN disambiguation (see [Duplicate customer names](#duplicate-customer-names)). You can insert rows directly with any SQLite client, or add seed entries in `Database.init_tables()`:

```python
# In Database.py → init_tables()
self.conn.execute("""
    INSERT OR IGNORE INTO customers (name, pan) VALUES
    ('Alice Johnson', 'AJKYC1234P'),
    ('Bob Williams',  'BWKYC9876P')
""")
```

### Step 2 — Populate financial data

`cibil_score` and `dti_ratio` are intentionally `NULL` by default — they are expected to come from an external data provider (credit bureau, bank feed, etc.). Update them directly:

```sql
UPDATE customers
SET cibil_score = 750,
    dti_ratio   = 0.15
WHERE pan = 'AJKYC1234P';
```

Or via Python:

```python
import sqlite3
con = sqlite3.connect("kyc_database.db")
con.execute("UPDATE customers SET cibil_score=750, dti_ratio=0.15 WHERE pan='AJKYC1234P'")
con.commit()
con.close()
```

### Step 3 — Create a KYC PDF

Place a PDF in `customer_kyc_pdfs/` following this naming convention:

```
kyc_<First>_<Last>.pdf        # spaces replaced with underscores
```

Examples:
```
kyc_Bob_Williams.pdf
kyc_Alice_Johnson.pdf
kyc_Charlie_Davis.pdf
```

The PDF must contain the customer's name and PAN number in plain text so `pypdf` can extract them. A minimal structure that works:

```
Name: Bob Williams
PAN: BWKYC9876P
```

---

## Pipeline walkthrough

The UI enforces a strict five-step sequence. Each step is enabled only after the previous one succeeds.

```
Step 1 │ Initialize Database
       │ Creates schema, seeds test customers.
       │ Automatically skipped on re-open if data already exists.
       ▼
Step 2 │ Get Customer Status
       │ Displays current KYC, financial, and eligibility status from DB.
       │ Automatically pre-populated from DB when customer name is entered,
       │ so this button is usually already satisfied on re-open.
       ▼
Step 3 │ KYC Verification
       │ Finds the PDF → extracts name + PAN → matches DB record
       │ → sets kyc_status = "Verified" on match.
       │ Locked for 14 days after a successful verification.
       ▼
Step 4 │ Financial Verification
       │ Reads cibil_score and dti_ratio from DB.
       │ Passes if CIBIL > 700 AND DTI < 20%.
       │ Locked for 14 days after a successful verification.
       ▼
Step 5 │ Finalize Eligibility
       │ Eligible if kyc_status = "Verified" AND financial passed.
       │ Result displayed as a prominent success or failure banner.
       │ Locked for 14 days after a successful determination.
```

**14-day re-check window** — Once KYC, Financial, or Eligibility is successfully completed for a customer, that step cannot be re-run for 14 days. This mirrors a real-world compliance cooling-off period. The lock dates are shown in the step description when active. To bypass for testing, delete `kyc_database.db` — all customers will be treated as new.

**Complete Pipeline** button at the bottom runs all five steps automatically. It is disabled if any of steps 3–5 have been completed within the 14-day window, or if the user has already started the sequential flow in the current session.

**Changing the customer name** resets Steps 2–5 for a fresh run on a different customer. Step 1 (Initialize Database) is preserved — it is tied to the database, not to any individual customer, so it never needs to be re-run just because the customer changed.

---

## Duplicate customer names

The system supports multiple customers who share the same first and last name (e.g., two different "John Smith" records with different PAN numbers).

**How it works:**

1. After you type a customer name, the UI silently queries the database to count how many rows match.
2. **Single match** — the PAN field is hidden and the pipeline proceeds as normal.
3. **Multiple matches** — a warning banner appears and a required **PAN Number** field is displayed. The entire pipeline is blocked until a PAN is entered.
4. Once PAN is provided, all tool calls (Get Customer Status, Financial Verification, Finalize Eligibility) use a composite **name + PAN** query to target the correct record.

**KYC is always unambiguous** — the KYC step extracts the PAN directly from the uploaded PDF and uses that for the database match, regardless of how many customers share the name.

**Adding duplicate-name customers:**

```sql
-- Two customers named "John Smith" with different PANs
INSERT INTO customers (name, pan, cibil_score, dti_ratio)
VALUES ('John Smith', 'JSABC1111P', 750, 0.15);

INSERT INTO customers (name, pan, cibil_score, dti_ratio)
VALUES ('John Smith', 'JSXYZ2222P', 820, 0.12);
```

Each needs its own PDF named by their distinct PAN or a disambiguating convention:
```
kyc_John_Smith.pdf     ← works only if both PANs are in separate files
```
Since the KYC step uses the name to locate the PDF file, duplicate-name customers should ideally have uniquely named PDF files. The PDF's embedded PAN is what the system uses to match the correct DB row, so as long as each PDF contains the right PAN, the match will succeed.

---

## Example: Bob Williams

**Pre-conditions:**
- `kyc_Bob_Williams.pdf` exists in `customer_kyc_pdfs/`
- DB record: `name = "Bob Williams"`, `pan = "BWKYC9876P"`, `cibil_score = 750`, `dti_ratio = 0.15`

**Expected outcomes per step:**

| Step | Result | Detail |
|---|---|---|
| Initialize Database | ✓ Success | Schema created, seed rows inserted |
| Get Customer Status | ✓ Success | All statuses show "pending" initially |
| KYC Verification | ✓ Success | PDF name + PAN matched; status → Verified |
| Financial Verification | ✓ Success | CIBIL 750 > 700, DTI 15% < 20%; status → Pass Verification |
| Finalize Eligibility | ✓ Success | Both checks passed; status → Eligible |

---

## Logs

All server-side activity is written to `kyc_app.log` at the project root.
The file is appended to across runs and is safe to inspect while the app is running.

```bash
# Tail the log in real time
tail -f kyc_app.log          # macOS / Linux
Get-Content kyc_app.log -Wait  # PowerShell
```

Log format:
```
2026-03-14 00:39:23,456  Services.KYCService          INFO      KYC Verified for customer_id=1 (Bob Williams)
```



