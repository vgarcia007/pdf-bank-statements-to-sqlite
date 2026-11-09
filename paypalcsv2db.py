#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PayPal CSV -> SQLite importer (idempotent per file).

- Expects German PayPal export with headers like:
  "Datum","Uhrzeit","Zeitzone","Beschreibung","Währung","Brutto","Entgelt","Netto",
  "Guthaben","Transaktionscode","Absender E-Mail-Adresse","Name","Name der Bank",
  "Bankkonto","Versand- und Bearbeitungsgebühr","Umsatzsteuer","Rechnungsnummer",
  "Zugehöriger Transaktionscode"

- Uses 'Netto' as signed amount:
    > 0  => direction='in'
    < 0  => direction='out'
  Stores amount as ABS(Netto) (positive), direction separately.

- Description is composed from several columns for easier searching.

- Writes to existing 'transactions' table (same schema as ing2db.py).
- Tracks imported files by SHA-256 in 'imports' (skip unless --force).
- Optional: restrict to a specific year.
- Ends with WAL checkpoint + journal_mode=DELETE + VACUUM.

Usage:
  python paypal_csv2db.py data/PayPal_2024.csv --db-path data/bank.sqlite3 --account "PayPal <you@example.com>"
  python paypal_csv2db.py data/ --db-path data/bank.sqlite3 --restrict-year 2024
"""

from pathlib import Path
import argparse
import hashlib
import re
import sqlite3
import sys
from typing import List, Tuple

import pandas as pd

# --------------------------- DB schema (same as ING) -------------------------

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY,
  source_file TEXT NOT NULL,
  file_sha256 TEXT NOT NULL UNIQUE,
  imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL,
  date TEXT NOT NULL,                 -- ISO (YYYY-MM-DD)
  amount NUMERIC NOT NULL,            -- positive value
  direction TEXT NOT NULL,            -- 'in' | 'out'
  description TEXT NOT NULL,
  category TEXT NULL,
  currency TEXT NOT NULL DEFAULT 'EUR',
  tx_hash TEXT NOT NULL UNIQUE,
  source_file TEXT NOT NULL,
  source_page_first INTEGER NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tx_account_date ON transactions(account, date);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
"""

def ensure_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA_SQL)
    return con

# --------------------------- Helpers ----------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()

def parse_german_number(s: str) -> float:
    """Parse German formatted amount: '1.234,56' or '-1,00' (NBSP tolerated)."""
    if s is None:
        return 0.0
    s = str(s).strip()
    s = s.replace("\u00A0", "").replace("\u202F", "")  # thin spaces
    neg = s.startswith("-") or s.endswith("-")
    s = s.strip("-")
    # remove thousands dots only
    s = re.sub(r"(?<=\d)\.(?=\d{3}(?:,|$))", "", s)
    s = s.replace(",", ".")
    try:
        val = float(s)
    except ValueError:
        val = 0.0
    return -val if neg else val

def compose_description(row: dict) -> str:
    parts = []
    # Main description
    if row.get("Beschreibung"):
        parts.append(str(row["Beschreibung"]).strip())
    # Name (counterparty)
    if row.get("Name"):
        parts.append(str(row["Name"]).strip())
    # Invoice number
    if row.get("Rechnungsnummer"):
        parts.append(f"Rechnungsnummer: {str(row['Rechnungsnummer']).strip()}")
    # Transaction code
    if row.get("Transaktionscode"):
        parts.append(f"Tx: {str(row['Transaktionscode']).strip()}")
    # Sender email (only if useful)
    if row.get("Absender E-Mail-Adresse"):
        parts.append(f"From: {str(row['Absender E-Mail-Adresse']).strip()}")
    return normalize_ws(" | ".join(p for p in parts if p))

def tx_hash(account: str, date_iso: str, amount_pos: float, direction: str, description: str, source_file: str) -> str:
    key = f"{account}|{date_iso}|{amount_pos:.2f}|{direction}|{normalize_ws(description)}|{source_file}"
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

# --------------------------- Import core -------------------------------------

REQUIRED_COLUMNS = [
    "Datum", "Beschreibung", "Währung", "Brutto", "Entgelt", "Netto",
    "Transaktionscode"
]

OPTIONAL_COLUMNS = [
    "Uhrzeit","Zeitzone","Guthaben","Absender E-Mail-Adresse","Name","Name der Bank",
    "Bankkonto","Versand- und Bearbeitungsgebühr","Umsatzsteuer","Rechnungsnummer",
    "Zugehöriger Transaktionscode"
]

def read_paypal_csv(path: Path) -> pd.DataFrame:
    """
    Read a German PayPal CSV export, keep all columns as strings.
    Handles BOM via utf-8-sig.
    """
    df = pd.read_csv(
        path,
        sep=",",
        quotechar='"',
        encoding="utf-8-sig",
        dtype=str,
        keep_default_na=False
    )
    # Basic presence check
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing expected columns in {path.name}: {missing}")
    return df

def rows_from_paypal_df(df: pd.DataFrame, restrict_year: int | None = None) -> List[dict]:
    rows: List[dict] = []
    for _, r in df.iterrows():
        date_str = (r.get("Datum") or "").strip()
        if not date_str:
            continue
        # Parse date DD.MM.YYYY -> YYYY-MM-DD
        try:
            date_iso = pd.to_datetime(date_str, format="%d.%m.%Y").date().isoformat()
        except Exception:
            continue

        if restrict_year is not None:
            if not date_iso.startswith(f"{restrict_year:04d}-"):
                continue

        # Use NET amount (signed)
        net = parse_german_number(r.get("Netto", "0"))
        direction = "in" if net >= 0 else "out"
        amount_pos = abs(net)

        desc = compose_description(r.to_dict())

        rows.append({
            "date_iso": date_iso,
            "amount_pos": amount_pos,
            "direction": direction,
            "description": desc,
            "currency": (r.get("Währung") or "EUR").strip() or "EUR"
        })
    return rows

def insert_rows(con: sqlite3.Connection, rows: List[dict], account: str, source_file: Path) -> int:
    cur = con.cursor()
    inserted = 0
    for row in rows:
        h = tx_hash(
            account=account,
            date_iso=row["date_iso"],
            amount_pos=row["amount_pos"],
            direction=row["direction"],
            description=row["description"],
            source_file=str(source_file),
        )
        cur.execute(
            """
            INSERT OR IGNORE INTO transactions
              (account, date, amount, direction, description, category, currency,
               tx_hash, source_file, source_page_first)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)
            """,
            (
                account,
                row["date_iso"],
                row["amount_pos"],
                row["direction"],
                row["description"],
                row["currency"],
                h,
                str(source_file),
            ),
        )
        inserted += cur.rowcount
    con.commit()
    return inserted

# --------------------------- CLI ---------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Import PayPal CSV into SQLite (each file at most once).")
    ap.add_argument("path", help="CSV file or directory with PayPal CSVs.")
    ap.add_argument("--db-path", default="data/bank.sqlite3", help="Path to SQLite DB.")
    ap.add_argument("-a", "--account", dest="account", default=None,
                    help="Account label to store (e.g. 'PayPal <you@example.com>'). Default: 'PayPal'.")
    ap.add_argument("--restrict-year", type=int, default=None, help="Keep only rows of this year.")
    ap.add_argument("--force", action="store_true", help="Import even if this file hash was already seen.")
    ap.add_argument("--dry-run", action="store_true", help="Parse only, do not write to DB.")
    args = ap.parse_args()

    in_path = Path(args.path)
    db_path = Path(args.db_path)
    con = ensure_db(db_path)

    # Collect CSVs
    if in_path.is_file() and in_path.suffix.lower() == ".csv":
        files = [in_path]
    elif in_path.is_dir():
        files = sorted([p for p in in_path.glob("*.csv")])
    else:
        print(f"Invalid input: {in_path}", file=sys.stderr)
        sys.exit(1)

    if not files:
        print("No CSV files found.", file=sys.stderr)
        sys.exit(2)

    # Pre-scan for known hashes
    to_import: List[Tuple[Path, str]] = []
    cur = con.cursor()
    for f in files:
        fh = sha256_file(f)
        cur.execute("SELECT 1 FROM imports WHERE file_sha256 = ?", (fh,))
        seen = cur.fetchone() is not None
        if seen and not args.force:
            print(f"[skip] already imported: {f.name}")
            continue
        to_import.append((f, fh))

    if not to_import:
        print("Nothing to import.")
        # clean-up for viewer
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        con.execute("PRAGMA journal_mode=DELETE;")
        con.execute("VACUUM;")
        con.close()
        sys.exit(0)

    # Parse + collect
    all_rows: List[dict] = []
    per_file_rows: List[Tuple[Path, str, List[dict]]] = []

    for f, fh in to_import:
        try:
            df = read_paypal_csv(f)
        except Exception as e:
            print(f"[error] {f.name}: {e}", file=sys.stderr)
            continue
        rows = rows_from_paypal_df(df, restrict_year=args.restrict_year)
        if not rows:
            print(f"[info] no rows after filtering in {f.name}")
            continue
        per_file_rows.append((f, fh, rows))
        all_rows.extend(rows)

    if not per_file_rows:
        print("No transactions parsed.")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        con.execute("PRAGMA journal_mode=DELETE;")
        con.execute("VACUUM;")
        con.close()
        sys.exit(0)

    # Account label
    account_label = args.account or "PayPal"

    # Dry run?
    if args.dry_run:
        n_in = sum(1 for r in all_rows if r["direction"] == "in")
        n_out = len(all_rows) - n_in
        print(f"[DRY-RUN] rows: {len(all_rows)}  in: {n_in}  out: {n_out}  account='{account_label}'")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        con.execute("PRAGMA journal_mode=DELETE;")
        con.execute("VACUUM;")
        con.close()
        return

    # Register import + insert
    total_inserted = 0
    for f, fh, rows in per_file_rows:
        cur.execute(
            "INSERT OR IGNORE INTO imports (source_file, file_sha256) VALUES (?, ?)",
            (str(f), fh),
        )
        con.commit()
        ins = insert_rows(con, rows, account=account_label, source_file=f)
        total_inserted += ins
        print(f"[ok] {f.name}: inserted {ins} rows as account='{account_label}'")

    print(f"DB: {db_path}")
    print(f"Imported rows total: {len(all_rows)}  |  Inserted new: {total_inserted}  |  Skipped files: {len(files) - len(per_file_rows)}")

    # Make DB viewer-friendly
    con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    con.execute("PRAGMA journal_mode=DELETE;")
    con.execute("VACUUM;")
    con.close()

if __name__ == "__main__":
    main()
