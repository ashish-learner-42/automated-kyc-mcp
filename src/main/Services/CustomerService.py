import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))  # adds src/main/ to path

import Database

logger = logging.getLogger(__name__)

class CustomerService:

    def __init__(self):
        self.db = Database.Database()

    def initialize_database(self) -> dict:
        """Seed the database with test customers and ensure schema is up to date."""
        self.db.init_tables()
        logger.info("Database initialized.")
        return {
            "success": True,
            "step":    "init",
            "data":    {"customers_seeded": 3},
            "message": "Database initialized with test customers.",
        }

    def get_customer_status(self, customer_name: str, pan_number: str = None) -> dict:
        """Fetch the current KYC, financial, and eligibility status for a customer.

        When pan_number is provided the lookup is a composite name+PAN query so
        the correct record is returned even when multiple customers share a name.
        """
        row = (
            self.db.get_customer_full_status_by_pan(customer_name, pan_number)
            if pan_number
            else self.db.get_customer_full_status(customer_name)
        )
        if row:
            logger.debug("Status fetched for %s", customer_name)
            return {
                "success": True,
                "step":    "status",
                "data": {
                    "kyc_status":              row[0],
                    "financial_status":        row[1],
                    "eligibility_status":      row[2],
                    # UTC timestamps of the last successful verification for each step.
                    # NULL (None) means the step has never been run.
                    # The UI uses these to enforce the 14-day re-check window.
                    "kyc_verified_at":         row[3],
                    "financial_verified_at":   row[4],
                    "eligibility_verified_at": row[5],
                },
                "message": f"Status retrieved for {customer_name}.",
            }
        logger.info("Customer not found: %s", customer_name)
        return {
            "success": False,
            "step":    "status",
            "data":    {},
            "message": f"Customer '{customer_name}' not found in database.",
        }