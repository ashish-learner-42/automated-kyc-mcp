import sys
import json
import logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — must happen before any local imports
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.resolve()  # automated-kyc-mcp/
SRC_MAIN     = Path(__file__).parent.parent.resolve()                # src/main/
sys.path.insert(0, str(SRC_MAIN))

# ---------------------------------------------------------------------------
# Logging — file handler only; stdout is reserved for the JSON protocol
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename=str(PROJECT_ROOT / "kyc_app.log"),
    level=logging.DEBUG,
    format="%(asctime)s  %(name)-28s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service imports
# ---------------------------------------------------------------------------
try:
    from fastmcp import FastMCP
    from Services.CustomerService   import CustomerService
    from Services.KYCService        import KYCService
    from Services.FinancialService  import FinancialService
    from Services.EligibilityService import EligibilityService
except ImportError as exc:
    logging.critical("Import failed: %s", exc)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Global instances
# ---------------------------------------------------------------------------
mcp             = FastMCP("AutomatedKYCandOnboardingEligibility")
customer_svc    = CustomerService()
kyc_svc         = KYCService()
financial_svc   = FinancialService()
eligibility_svc = EligibilityService()

# ---------------------------------------------------------------------------
# MCP Tools — protocol wrappers only, no business logic here
# ---------------------------------------------------------------------------

@mcp.tool()
def init_all_db() -> dict:
    """Initialize SQLite database and seed test customers."""
    return customer_svc.initialize_database()

@mcp.tool()
def get_customer_status(customer_name: str, pan_number: str = None) -> dict:
    """Return current KYC, financial, and eligibility status for a customer.
    Provide pan_number when multiple customers share the same name."""
    return customer_svc.get_customer_status(customer_name, pan_number)

@mcp.tool()
def perform_kyc_verification(customer_name: str) -> dict:
    """Extract name and PAN from KYC PDF, match against database, and update KYC status.
    KYC always uses the PAN from the PDF for matching, so pan_number is not needed here."""
    return kyc_svc.perform_kyc_verification(customer_name)

@mcp.tool()
def verify_financial_status(customer_name: str, pan_number: str = None) -> dict:
    """Validate CIBIL score (>700) and DTI ratio (<20%) for the customer.
    Provide pan_number when multiple customers share the same name."""
    return financial_svc.perform_financial_verification(customer_name, pan_number)

@mcp.tool()
def finalize_account_eligibility(customer_name: str, pan_number: str = None) -> dict:
    """Mark customer Eligible only if KYC is Verified and financial check passed.
    Provide pan_number when multiple customers share the same name."""
    return eligibility_svc.determine_final_eligibility(customer_name, pan_number)

# ---------------------------------------------------------------------------
# CLI dispatch — used by the Streamlit UI via subprocess
# ---------------------------------------------------------------------------
AVAILABLE_TOOLS = {
    "init_all_db":                  init_all_db,
    "get_customer_status":          get_customer_status,
    "perform_kyc_verification":     perform_kyc_verification,
    "verify_financial_status":      verify_financial_status,
    "finalize_account_eligibility": finalize_account_eligibility,
}

def call_tool(tool_name: str, args: dict) -> dict:
    """Dispatch a tool call by name and return the result."""
    if tool_name not in AVAILABLE_TOOLS:
        return {
            "success": False,
            "step":    tool_name,
            "data":    {},
            "message": f"Unknown tool '{tool_name}'. Available: {list(AVAILABLE_TOOLS)}",
        }
    try:
        return AVAILABLE_TOOLS[tool_name](**args)
    except Exception as exc:
        logger.exception("Tool '%s' raised an exception.", tool_name)
        return {"success": False, "step": tool_name, "data": {}, "message": str(exc)}

if len(sys.argv) >= 3 and sys.argv[1] == "--tool":
    _result = call_tool(sys.argv[2], json.loads(sys.argv[3] if len(sys.argv) > 3 else "{}"))
    print(json.dumps(_result, default=str))
    sys.exit(0)

# ---------------------------------------------------------------------------
# MCP server mode
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")