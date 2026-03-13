import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#database file to stay at project root
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
DB_PATH = PROJECT_ROOT/"kyc_database.db"

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.init_tables()
    
    def init_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id                       INTEGER PRIMARY KEY,
                name                     TEXT,
                pan                      TEXT UNIQUE,
                kyc_status               TEXT DEFAULT 'pending kyc',
                financial_status         TEXT DEFAULT 'pending',
                eligibility_status       TEXT DEFAULT 'pending',
                cibil_score              INTEGER,
                dti_ratio                REAL,
                kyc_verified_at          TEXT,
                financial_verified_at    TEXT,
                eligibility_verified_at  TEXT
            )
        """)

        # Non-destructive migration: add columns if upgrading an existing DB.
        # Timestamp columns are NULL for pre-existing rows, which is treated as
        # "never verified" → the 14-day lock is not applied → steps remain enabled.
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(customers)")}
        for col, defn in [
            ("cibil_score",              "INTEGER"),
            ("dti_ratio",                "REAL"),
            ("kyc_verified_at",          "TEXT"),
            ("financial_verified_at",    "TEXT"),
            ("eligibility_verified_at",  "TEXT"),
        ]:
            if col not in existing:
                self.conn.execute(f"ALTER TABLE customers ADD COLUMN {col} {defn}")
        
        #seed test customers
        self.conn.execute("""
            INSERT OR IGNORE INTO customers (name, pan) VALUES 
            ('Bob Williams', 'BWKYC9876P'),
            ('Alice Johnson', 'AJKYC1234P'),
            ('Charlie Davis', 'CDKYC5678P')
        """)
        self.conn.commit()
        logger.debug("Tables initialized and seed data inserted.")
    
    def fetch_customer_kyc_status_by_name_pan(self, name:str, pan:str):
        cur = self.conn.execute(
            "SELECT id, kyc_status FROM customers WHERE name=? AND pan=?",
            (name, pan)
        )
        return cur.fetchone()
    
    def update_kyc_status(self, customer_id: int, status: str):
        self.conn.execute(
            "UPDATE customers SET kyc_status=?, kyc_verified_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, customer_id),
        )
        self.conn.commit()        
    
    def get_financial_data(self, customer_id: int) -> dict | None:
        cur = self.conn.execute(
            "SELECT cibil_score, dti_ratio FROM customers WHERE id=?", (customer_id,)
        )
        row = cur.fetchone()
        if row is None or row[0] is None or row[1] is None:
            logger.warning("Financial data not available for customer_id=%s", customer_id)
            return None
        return {"cibil": row[0], "dti": row[1]}

    def update_financial_status(self, customer_id: int, status: str):
        self.conn.execute(
            "UPDATE customers SET financial_status=?, financial_verified_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, customer_id),
        )
        self.conn.commit()

    def update_eligibility_status(self, customer_id: int, status: str):
        self.conn.execute(
            "UPDATE customers SET eligibility_status=?, eligibility_verified_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, customer_id),
        )
        self.conn.commit()

    def get_eligibility_status(self, customer_id: int):
        cur = self.conn.execute(
            "SELECT kyc_status, financial_status FROM customers WHERE id=?", (customer_id,)
        )
        return cur.fetchone()

    def get_customer_full_status(self, customer_name: str):
        cur = self.conn.execute(
            """SELECT kyc_status, financial_status, eligibility_status,
                      kyc_verified_at, financial_verified_at, eligibility_verified_at
               FROM customers WHERE name=?""",
            (customer_name,),
        )
        return cur.fetchone()

    def get_customer_full_status_by_pan(self, customer_name: str, pan: str):
        """Composite name+PAN lookup — used when multiple customers share a name."""
        cur = self.conn.execute(
            """SELECT kyc_status, financial_status, eligibility_status,
                      kyc_verified_at, financial_verified_at, eligibility_verified_at
               FROM customers WHERE name=? AND pan=?""",
            (customer_name, pan),
        )
        return cur.fetchone()

    def count_customers_by_name(self, customer_name: str) -> int:
        """Return how many DB rows share the given name (detects duplicate names)."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM customers WHERE name=?", (customer_name,)
        )
        return cur.fetchone()[0]

    def get_customer_id_by_name_pan(self, name: str, pan: str):
        """Return (id,) for a name+PAN pair, or None if not found."""
        cur = self.conn.execute(
            "SELECT id FROM customers WHERE name=? AND pan=?", (name, pan)
        )
        return cur.fetchone()
