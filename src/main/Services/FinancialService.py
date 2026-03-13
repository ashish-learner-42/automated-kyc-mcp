import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))  # adds src/main/ to path

import Database

logger = logging.getLogger(__name__)

CIBIL_THRESHOLD = 700
DTI_THRESHOLD   = 0.20

"""
Financial Service: Validates CIBIL score (>700) and DTI ratio (<20%),
then updates the customer's financial status in the database.
"""

class FinancialService:

    def __init__(self):
        self.db = Database.Database()

    def perform_financial_verification(self, customer_name: str, pan_number: str = None) -> dict:
        """Validate CIBIL score and DTI ratio, then update financial status in DB.

        When pan_number is provided the lookup is a composite name+PAN query so
        the correct record is used even when multiple customers share a name.
        """
        logger.debug("Financial verification for: %s", customer_name)

        if pan_number:
            row = self.db.get_customer_id_by_name_pan(customer_name, pan_number)
        else:
            cur = self.db.conn.execute(
                "SELECT id FROM customers WHERE name=?", (customer_name,)
            )
            row = cur.fetchone()
        if row is None:
            logger.info("Customer not found: %s", customer_name)
            return {
                "success": False,
                "step":    "financial",
                "data":    {"financial_status": "error"},
                "message": f"Customer '{customer_name}' not found in database.",
            }

        customer_id = row[0]
        data        = self.db.get_financial_data(customer_id)
        if data is None:
            logger.info("Financial data not available for: %s", customer_name)
            return {
                "success": False,
                "step":    "financial",
                "data":    {"financial_status": "error"},
                "message": f"Financial data (CIBIL/DTI) not yet available for '{customer_name}'.",
            }

        cibil_ok = data["cibil"] > CIBIL_THRESHOLD
        dti_ok   = data["dti"]   < DTI_THRESHOLD

        failed_rules = []
        if not cibil_ok:
            failed_rules.append(f"CIBIL {data['cibil']} ≤ {CIBIL_THRESHOLD}")
        if not dti_ok:
            failed_rules.append(f"DTI {data['dti']:.0%} ≥ {DTI_THRESHOLD:.0%}")

        status  = "Pass Verification" if (cibil_ok and dti_ok) else "Failed Verification"
        message = "All rules passed." if not failed_rules else f"Failed: {', '.join(failed_rules)}"

        self.db.update_financial_status(customer_id, status)
        logger.info("%s → %s | %s", customer_name, status, message)

        return {
            "success": cibil_ok and dti_ok,
            "step":    "financial",
            "data": {
                "financial_status": status,
                "cibil_score":      data["cibil"],
                "dti_ratio":        data["dti"],
                "cibil_passed":     cibil_ok,
                "dti_passed":       dti_ok,
                "failed_rules":     failed_rules,
            },
            "message": message,
        }