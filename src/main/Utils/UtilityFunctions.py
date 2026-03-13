import sys
import logging
from pathlib import Path
from pypdf import PdfReader
import re

logger = logging.getLogger(__name__)

class UtilityFunctions:    
    
    def find_expected_file(self, customer_name: str) -> Path:
        """Resolve the expected KYC PDF path for a given customer name."""
        project_root = Path(__file__).parent.parent.parent.parent.resolve()
        pdf_dir      = project_root / "customer_kyc_pdfs"
        pdf_filename = f"kyc_{customer_name.replace(' ', '_')}.pdf"
        pdf_path     = pdf_dir / pdf_filename

        logger.debug("Resolved PDF path: %s | exists: %s", pdf_path, pdf_path.exists())

        if pdf_path.exists():
            return pdf_path

        # Fallback: case-insensitive scan in case filename casing differs
        logger.warning("Exact path not found; scanning '%s' for case-insensitive match.", pdf_dir)
        for candidate in pdf_dir.glob("*.pdf"):
            if candidate.name.lower() == pdf_filename.lower():
                logger.warning("Matched via case-insensitive scan: %s", candidate)
                return candidate
        return pdf_path  # Non-existent path — caller checks with check_file_path()
    
    def check_file_path(self, pdf_path: Path) -> Path | None:
        """Return the path if it exists on disk, otherwise None."""
        return pdf_path if pdf_path and pdf_path.exists() else None
        
    def extract_data_from_kyc_pdf(self, pdf_path: Path) -> dict | None:
        """Extract customer name and PAN number from a KYC PDF document."""
        try:
            reader = PdfReader(str(pdf_path))
            text   = "".join(page.extract_text() or "" for page in reader.pages)

            logger.debug("PDF text preview: %s", text[:200])

            name_match = re.search(
                r'(?i)Name[:\s]*([A-Za-z\s]+?)(?=\s*(?:PAN|DOB|Address|$))', text
            )
            pan_match = re.search(
                r'(?i)PAN[:\s]*([A-Z]{5}[0-9]{4}[A-Z])', text
            )

            result = {
                "name": name_match.group(1).strip() if name_match else None,
                "pan":  pan_match.group(1)           if pan_match  else None,
            }
            logger.debug("Extracted from PDF: %s", result)
            return result

        except Exception:
            logger.exception("Failed to extract data from PDF: %s", pdf_path)
            return None
