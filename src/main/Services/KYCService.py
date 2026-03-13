import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))  # adds src/main/ to path

import Database
from Utils.UtilityFunctions import UtilityFunctions

logger = logging.getLogger(__name__)


class KYCService:

    def __init__(self):
        self.db   = Database.Database()
        self.util = UtilityFunctions()

    def perform_kyc_verification(self, customer_name: str) -> dict:
        """
        Locate the customer's KYC PDF, extract name and PAN, match against
        the database record, and update the KYC status accordingly.
        """
        logger.debug("KYC verification requested for: %s", customer_name)

        # 1. Resolve and validate PDF path
        pdf_path = self.util.find_expected_file(customer_name)
        if not self.util.check_file_path(pdf_path):
            logger.warning("KYC PDF not found for: %s (expected: %s)", customer_name, pdf_path)
            return {
                "success": False,
                "step":    "kyc",
                "data":    {"kyc_status": "Document Not Found"},
                "message": f"KYC PDF not found for '{customer_name}'. Expected: {pdf_path.name}",
            }

        # 2. Extract name and PAN from PDF
        extracted = self.util.extract_data_from_kyc_pdf(pdf_path)
        if extracted is None:
            logger.error("Failed to extract data from PDF: %s", pdf_path)
            return {
                "success": False,
                "step":    "kyc",
                "data":    {"kyc_status": "Extraction Failed"},
                "message": "Could not extract data from the KYC PDF.",
            }

        pdf_name = extracted.get("name")
        pdf_pan  = extracted.get("pan")

        if not pdf_name or not pdf_pan:
            logger.warning("Incomplete data in PDF for %s — name=%s pan=%s", customer_name, pdf_name, pdf_pan)
            return {
                "success": False,
                "step":    "kyc",
                "data":    {"kyc_status": "Incomplete Document", "extracted_name": pdf_name, "extracted_pan": pdf_pan},
                "message": "Name or PAN missing in the KYC PDF.",
            }

        # 3. Match against DB record
        row = self.db.fetch_customer_kyc_status_by_name_pan(pdf_name, pdf_pan)
        if row is None:
            logger.info("No DB match for name='%s' pan='%s'", pdf_name, pdf_pan)
            return {
                "success": False,
                "step":    "kyc",
                "data":    {"kyc_status": "Verification Failed", "extracted_name": pdf_name, "extracted_pan": pdf_pan},
                "message": f"No database record matches name='{pdf_name}' and PAN='{pdf_pan}'.",
            }

        customer_id, _current_status = row

        # 4. Update KYC status in DB
        self.db.update_kyc_status(customer_id, "Verified")
        logger.info("KYC Verified for customer_id=%s (%s)", customer_id, customer_name)

        return {
            "success": True,
            "step":    "kyc",
            "data":    {"kyc_status": "Verified", "extracted_name": pdf_name, "extracted_pan": pdf_pan},
            "message": f"KYC verified successfully for '{customer_name}'.",
        }
