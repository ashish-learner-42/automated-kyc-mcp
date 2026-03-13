import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))  # adds src/main/ to path

import Database

logger = logging.getLogger(__name__)

class EligibilityService:

    def __init__(self):
        self.db = Database.Database()

    def determine_final_eligibility(self, customer_name: str, pan_number: str = None) -> dict:
        """Determine if customer is eligible based on KYC and financial verification status.

        When pan_number is provided the lookup is a composite name+PAN query so
        the correct record is used even when multiple customers share a name.
        """
        logger.debug("Determining eligibility for: %s", customer_name)

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
                "step":    "eligibility",
                "data":    {"eligibility_status": "Not Eligible"},
                "message": f"Customer '{customer_name}' not found in database.",
            }

        customer_id    = row[0]
        kyc, financial = self.db.get_eligibility_status(customer_id)
        eligible       = (kyc == "Verified") and ("Pass" in financial)
        status         = "Eligible" if eligible else "Not Eligible"

        self.db.update_eligibility_status(customer_id, status)
        logger.info("%s → %s  (KYC=%s, Financial=%s)", customer_name, status, kyc, financial)

        return {
            "success": eligible,
            "step":    "eligibility",
            "data": {
                "eligibility_status": status,
                "kyc_status":         kyc,
                "financial_status":   financial,
            },
            "message": f"{customer_name}: KYC='{kyc}', Financial='{financial}' → {status}",
        }
