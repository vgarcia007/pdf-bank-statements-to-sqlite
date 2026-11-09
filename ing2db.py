#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ING statement -> SQLite importer (idempotent).
- Parses multi-line descriptions, handles double date lines.
- Page-aware: strip page headers (Girokonto Nummer..., Kontoauszug..., Datum, Seite x von y)
  and anchored footers (company/legal lines) without killing IBAN/BIC in descriptions.
- Safe amount parsing: thousands separator is dot or narrow spaces (NBSP \u00A0, NNBSP \u202F),
  normal spaces are NOT accepted (prevents '... 1981 668,25' glue).
- Post-processing:
    * merge orphan rows (no amount) into previous booking
    * strip trailing page-header fragments from description
- DB write policy:
    * Each PDF file is imported at most once (by SHA-256). Use --force to re-import.
- Finishes with WAL checkpoint + journal_mode=DELETE + VACUUM for viewer-friendly file.

Usage examples:
  python3 ing2db.py /path/to/ING_PDF.pdf --account "DE89 3704 0044 0532 0130 00"
  python3 ing2db.py /path/to/folder --db-path ./data/bank.sqlite3 --csv-out ./out.csv
  python3 ing2db.py /path/to/folder --keep-newlines --force
"""

from pathlib import Path
import argparse
import hashlib
import math
import re
import sqlite3
import sys

import pandas as pd
from pypdf import PdfReader

# ---------------------- Parser regexes & helpers ----------------------------

DATE_TOKEN = r"(0[1-9]|[12][0-9]|3[01])[.](0[1-9]|1[0-2])[.](19|20)\d{2}"
DATE_START_RE = re.compile(rf"^(?P<date>{DATE_TOKEN})\s*(?P<rest>.*)$")
DATE_DOUBLE_START_RE = re.compile(rf"^(?P<date1>{DATE_TOKEN})\s+(?P<date2>{DATE_TOKEN})\s*(?P<rest>.*)$")

# Amount strictly at line end; allow thousands '.' or NBSP/NNBSP but NO normal spaces.
# Also forbid a digit immediately before the amount block (prevents glue like '... 1981 668,25').
AMOUNT_RE = re.compile(
    r"(?<!\d)(?P<amt>(?:-)?(?:\d{1,3}(?:[.\u00A0\u202F]\d{3})*|\d+),\d{2})(?P<trailminus>-)?\s*(?:€|EUR)?\s*$"
)

# Page header lines (anchored at start)
HEADER_RE = re.compile(
    r"^("
    r"ING\b|ING-?DiBa\b|"
    r"Girokonto\s+Nummer\b|Kontoauszug\b|"
    r"Datum\b|Seite\s+\d+\s+von\s+\d+\b"
    r")",
    re.IGNORECASE,
)

# Page footer lines (anchored at start) — intentionally NO IBAN/BIC here
FOOTER_RE = re.compile(
    r"^("
    r"ING-?DiBa\b|Theodor-Heuss-Allee\b|Vorsitzende des Aufsichtsrates\b|Vorstand:|"
    r"Sitz:\s*Frankfurt am Main\b|HRB\s*\d+\b|Steuernummer:\b|USt-IdNr\.?\b|"
    r"Internet:\s*www\.ing\.de\b|E-Mail:\s*info@ing\.de\b|"
    r"Seite\s+\d+\s+von\s+\d+\b|Auszugsnummer\b|"
    r"Eingeräumte Kontoüberziehung\b|Alter Saldo\b|Neuer Saldo\b"
    r")",
    re.IGNORECASE,
)

# IBAN probe (optional)
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?\d{4}){4,7})\b")

# Trailing header-fragment stripper (at END of description)
MONTHS = r"(?:Januar|Februar|M(?:ä|ae)rz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)"
TRAILING_HEADER_BLOCK_RE = re.compile(
    rf"(?:\s*(?:"
    rf"Girokonto\s+Nummer\s+\d{{6,}}"
    rf"|Kontoauszug\s+{MONTHS}\s+\d{{4}}"
    rf"|Datum\s+{DATE_TOKEN}"
    rf"))+\s*$",
    re.IGNORECASE,
)

def _norm(s: str) -> str:
    """Normalize unicode minus and NBSP variants, strip trailing spaces."""
    return (
        s.replace("−", "-").replace("–", "-").replace("—", "-")
         .replace("\u00A0", " ").replace("\u202F", " ")
         .rstrip()
    )

def _parse_amount(amt_str: str, trailminus: bool = False) -> float:
    """German amount string to float (supports trailing/leading minus)."""
    s = amt_str.strip()
    neg = False
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-"):
        neg, s = True, s[1:]
    if trailminus:
        neg = True
    # remove narrow spaces as thousands separators
    s = s.replace("\u00A0", "").replace("\u202F", "")
    # remove thousands dots only
    s = re.sub(r"(?<=\d)\.(?=\d{3}(?:,|$))", "", s)
    s = s.replace(",", ".")
    val = float(s)
    return -val if neg else val

def extract_account_identifier(pdf_texts: list[str]) -> str | None:
    """Try to extract the account identifier (IBAN) from the PDF texts."""
    for t in pdf_texts:
        for m in IBAN_RE.finditer(t):
            raw = " ".join(m.group(1).split())
            if raw[:2].isalpha():
                return raw
    return None

def strip_trailing_headers_in_description(text: str) -> str:
    """Remove trailing page-header fragments from description."""
    if not text:
        return text
    s = text.rstrip()
    s = TRAILING_HEADER_BLOCK_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s

# ---------------------- Core parser -----------------------------------------

def extract_ing_page_text(page) -> str:
    """Recognize raised amount footnotes using font information, not a third decimal.

    PDF extraction joins e.g. a 10pt amount and its 6pt superscript marker into
    one token. Remove only markers proved by that font transition and an exact
    occurrence count at the end of an amount line. The original PDF stays intact.
    """
    markers = {}
    previous = None

    def visit(text, cm, tm, font, size):
        nonlocal previous
        value = text.strip()
        if not value:
            return
        if previous is not None and re.fullmatch(r"[1-9]", value) and 0 < size <= previous[1] * 0.75:
            key = (previous[0], value)
            markers[key] = markers.get(key, 0) + 1
        amount = AMOUNT_RE.search(_norm(value))
        previous = (value[amount.start():], size) if amount and size > 0 else None

    text = page.extract_text(0, visitor_text=visit) or ""
    for (amount, marker), count in markers.items():
        pattern = re.compile(r"(?<![\d,.])" + re.escape(amount) + r"[ \t]*" + re.escape(marker)
                             + r"(?=[ \t]*(?:€|EUR)?[ \t]*(?:\r?\n|$))")
        if len(pattern.findall(text)) == count:
            text = pattern.sub(lambda _: amount, text)
    return text


def parse_ing_kontoauszug(file: Path, keep_newlines: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """Parse a single ING PDF into rows (date, description, amount) and return (DataFrame, page_texts)."""
    reader = PdfReader(file)
    rows = []
    current = None  # {"date": str, "amount": float|None, "desc_parts": [str]}
    page_texts = []

    def finalize():
        nonlocal current
        if not current:
            return
        sep = "\n" if keep_newlines else " "
        desc = sep.join(p.strip() for p in current["desc_parts"] if p.strip())
        rows.append({"date": current["date"], "description": desc, "amount": current["amount"]})
        current = None

    for page in reader.pages:
        text = extract_ing_page_text(page)
        page_texts.append(text)
        lines = text.split("\n")
        seen_first_date = False
        i = 0
        while i < len(lines):
            raw = lines[i]
            line = _norm(raw)

            # anchored footer → close and skip rest of page
            if FOOTER_RE.match(line):
                finalize()
                break

            # page head: ignore headers/noise until first date line
            if not seen_first_date:
                if HEADER_RE.match(line) or not line.strip():
                    i += 1
                    continue
                if DATE_DOUBLE_START_RE.match(line) or DATE_START_RE.match(line):
                    seen_first_date = True
                else:
                    i += 1
                    continue

            if not line.strip():
                i += 1
                continue

            # two dates at start (booking + value)
            m2 = DATE_DOUBLE_START_RE.match(line)
            if m2:
                date_str = m2.group("date1")
                rest = m2.group("rest").strip()
                finalize()
                current = {"date": date_str, "amount": None, "desc_parts": []}
                if rest:
                    amt_m = AMOUNT_RE.search(rest)
                    if amt_m:
                        current["amount"] = _parse_amount(amt_m.group("amt"), bool(amt_m.group("trailminus")))
                        desc = rest[:amt_m.start()].rstrip()
                    else:
                        desc = rest
                    if desc:
                        current["desc_parts"].append(desc)
                i += 1
                continue

            # one date at start
            m1 = DATE_START_RE.match(line)
            if m1:
                date_str = m1.group("date")
                rest = (m1.group("rest") or "").strip()

                # swallow value-date in next line if present
                if not rest and i + 1 < len(lines):
                    nxt = _norm(lines[i + 1])
                    m_next_date = DATE_START_RE.match(nxt)
                    if m_next_date:
                        i += 1
                        rest = (m_next_date.group("rest") or "").strip()

                finalize()
                current = {"date": date_str, "amount": None, "desc_parts": []}
                if rest:
                    amt_m = AMOUNT_RE.search(rest)
                    if amt_m:
                        current["amount"] = _parse_amount(amt_m.group("amt"), bool(amt_m.group("trailminus")))
                        desc = rest[:amt_m.start()].rstrip()
                    else:
                        desc = rest
                    if desc:
                        current["desc_parts"].append(desc)
                i += 1
                continue

            # continuation
            if current:
                amt_m = AMOUNT_RE.search(line)
                if amt_m:
                    lead = line[:amt_m.start()].rstrip()
                    if lead:
                        current["desc_parts"].append(lead)
                    if current["amount"] is None:
                        current["amount"] = _parse_amount(amt_m.group("amt"), bool(amt_m.group("trailminus")))
                else:
                    current["desc_parts"].append(line)

            i += 1

    finalize()
    df = pd.DataFrame(rows)
    if df.empty:
        return df, page_texts
    df["date"] = pd.to_datetime(df["date"], format="%d.%m.%Y", errors="coerce")
    df = df.dropna(subset=["date"])
    return df, page_texts

# ---------------------- Post-processing -------------------------------------

def compact_orphan_description_rows(df: pd.DataFrame, join_with: str = " ") -> pd.DataFrame:
    """Merge rows with missing amount into the previous row by concatenating descriptions."""
    if df.empty:
        return df
    out = []
    for _, row in df.iterrows():
        amt = row.get("amount", None)
        is_missing = amt is None or (isinstance(amt, float) and math.isnan(amt))
        if is_missing:
            if out and row["description"]:
                prev = out[-1].copy()
                prev_desc = str(prev.get("description", "")).rstrip()
                cur_desc = str(row["description"]).lstrip()
                merged = (prev_desc + (join_with if prev_desc and cur_desc else "") + cur_desc).strip()
                prev["description"] = merged
                out[-1] = prev
            elif not out:
                out.append(row.to_dict())  # defensive
        else:
            out.append(row.to_dict())
    return pd.DataFrame(out, columns=df.columns)

def clean_descriptions(df: pd.DataFrame) -> pd.DataFrame:
    """Strip trailing header fragments that slipped into descriptions."""
    if df.empty:
        return df
    df = df.copy()
    df["description"] = df["description"].map(strip_trailing_headers_in_description)
    return df

# --------------------------- DB helpers -------------------------------------

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
  date TEXT NOT NULL,                 -- ISO date (YYYY-MM-DD)
  amount NUMERIC NOT NULL,            -- always positive
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

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def normalize_text(s: str) -> str:
    s = _norm(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

#def tx_hash(account: str, date_iso: str, amount_pos: float, direction: str, description: str) -> str:
#    key = f"{account}|{date_iso}|{amount_pos:.2f}|{direction}|{normalize_text(description)}"
#    return hashlib.sha256(key.encode("utf-8")).hexdigest()

#def tx_hash(account: str, date_iso: str, amount_pos: float, direction: str, description: str, source_file: str) -> str:
#    key = f"{account}|{date_iso}|{amount_pos:.2f}|{direction}|{re.sub(r'\\s+', ' ', str(description)).strip()}|{source_file}"
#    return hashlib.sha256(key.encode('utf-8')).hexdigest()
def tx_hash(account, date_iso, amount_pos, direction, description, source_file):
    clean_desc = re.sub(r"\s+", " ", str(description)).strip()
    key = f"{account}|{date_iso}|{amount_pos:.2f}|{direction}|{clean_desc}|{source_file}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def insert_transactions(con: sqlite3.Connection, df: pd.DataFrame, account: str, source_file: Path):
    cur = con.cursor()
    rows = 0
    for _, r in df.iterrows():
        date_iso = pd.to_datetime(r["date"]).date().isoformat()
        amount_signed = float(r["amount"])
        direction = "out" if amount_signed < 0 else "in"
        amount_pos = abs(amount_signed)
        description = str(r["description"])
        #h = tx_hash(account, date_iso, amount_pos, direction, description)
        h = tx_hash(account, date_iso, amount_pos, direction, description, str(source_file))
        cur.execute(
            """
            INSERT OR IGNORE INTO transactions
              (account, date, amount, direction, description, category, currency,
               tx_hash, source_file, source_page_first)
            VALUES (?, ?, ?, ?, ?, NULL, 'EUR', ?, ?, NULL)
            """,
            (account, date_iso, amount_pos, direction, description, h, str(source_file)),
        )
        rows += cur.rowcount  # 1 if inserted, 0 if ignored
    con.commit()
    return rows

# ------------------------------- CLI ----------------------------------------

def main():
    p = argparse.ArgumentParser(description="Import ING statement PDFs into SQLite (each PDF at most once).")
    p.add_argument("path", help="PDF file or directory with ING PDFs.")
    p.add_argument("-a", "--account", dest="account", default=None,
                   help="Account identifier to store (IBAN or custom). If omitted, try to extract IBAN from PDF; if not found, abort.")
    p.add_argument("--db-path", default="./data/bank.sqlite3",
                   help="Path to SQLite DB (default: ./data/bank.sqlite3).")
    p.add_argument("--csv-out", default=None, help="Optional CSV export path.")
    p.add_argument("--sep", default=";", help="CSV delimiter (default: ';').")
    p.add_argument("--keep-newlines", action="store_true", help="Keep newlines inside description.")
    p.add_argument("--force", action="store_true", help="Import even if this PDF hash was already seen.")
    p.add_argument("--dry-run", action="store_true", help="Parse and show counts, but do not write to DB.")
    args = p.parse_args()

    in_path = Path(args.path)
    db_path = Path(args.db_path)

    # Collect PDFs
    if in_path.is_file() and in_path.suffix.lower() == ".pdf":
        pdfs = [in_path]
    elif in_path.is_dir():
        pdfs = [p for p in sorted(in_path.glob("*.pdf")) if "kontoauszug" in p.name.lower()]
        if not pdfs:
            print(f"No matching PDFs in {in_path}", file=sys.stderr)
            sys.exit(2)
    else:
        print(f"Invalid input: {in_path}", file=sys.stderr)
        sys.exit(1)

    # Open DB and ensure schema
    con = ensure_db(db_path)

    # Decide which PDFs to import based on file hash (skip already imported unless --force)
    to_import: list[tuple[Path, str]] = []
    cur = con.cursor()
    for pdf in pdfs:
        fhash = sha256_file(pdf)
        cur.execute("SELECT 1 FROM imports WHERE file_sha256 = ?", (fhash,))
        seen = cur.fetchone() is not None
        if seen and not args.force:
            print(f"[skip] already imported: {pdf.name}")
            continue
        to_import.append((pdf, fhash))

    if not to_import:
        print("Nothing to import.")
        # still clean journal for viewer
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        con.execute("PRAGMA journal_mode=DELETE;")
        con.execute("VACUUM;")
        con.close()
        sys.exit(0)

    frames = []
    detected_account = None

    # Parse selected PDFs
    for pdf, fhash in to_import:
        df, page_texts = parse_ing_kontoauszug(pdf, keep_newlines=args.keep_newlines)
        if df.empty:
            print(f"[warn] no rows parsed from {pdf.name}")
            # Mark as imported anyway? Usually no. So we skip marking in imports for empty parses.
            continue

        # Post-processing
        joiner = "\n" if args.keep_newlines else " "
        df = compact_orphan_description_rows(df, join_with=joiner)
        df = clean_descriptions(df)
        df["source_file"] = str(pdf)
        frames.append((pdf, fhash, df))

        # Try detect account if not provided yet
        if detected_account is None:
            acct = extract_account_identifier(page_texts)
            if acct:
                detected_account = acct

    if not frames:
        print("No transactions parsed.")
        # clean journal and exit
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        con.execute("PRAGMA journal_mode=DELETE;")
        con.execute("VACUUM;")
        con.close()
        sys.exit(3)

    # Merge all parsed rows for optional CSV reporting
    df_all = pd.concat([df for _, _, df in frames], ignore_index=True)

    # Determine account identifier
    account = args.account or detected_account
    if not account:
        print("No account identifier provided and no IBAN found in PDFs. Use --account.", file=sys.stderr)
        # do not write partial state
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        con.execute("PRAGMA journal_mode=DELETE;")
        con.execute("VACUUM;")
        con.close()
        sys.exit(4)

    # Optional CSV export
    if args.csv_out:
        df_all.to_csv(args.csv_out, index=False, sep=args.sep, encoding="utf-8")
        print(f"CSV written: {args.csv_out} ({len(df_all)} rows)")

    if args.dry_run:
        n_in = (df_all["amount"].astype(float) >= 0).sum()
        n_out = (df_all["amount"].astype(float) < 0).sum()
        print(f"[DRY-RUN] Parsed rows: {len(df_all)}  incoming: {n_in}  outgoing: {n_out}")
        print(f"[DRY-RUN] Account: {account}")
        # clean journal and exit
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        con.execute("PRAGMA journal_mode=DELETE;")
        con.execute("VACUUM;")
        con.close()
        return

    # Write per-PDF: first register import (idempotent), then insert rows
    total_inserted = 0
    for pdf, fhash, df in frames:
        cur.execute(
            "INSERT OR IGNORE INTO imports (source_file, file_sha256) VALUES (?, ?)",
            (str(pdf), fhash),
        )
        con.commit()
        inserted = insert_transactions(con, df, account=account, source_file=pdf)
        total_inserted += inserted
        print(f"[ok] {pdf.name}: inserted {inserted} rows")

    print(f"DB: {db_path}  |  Account: {account}")
    print(f"Imported rows total: {len(df_all)}  |  Inserted new: {total_inserted}  |  Skipped files: {len(pdfs) - len(frames)}")

    # Finish in viewer-friendly shape
    con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    con.execute("PRAGMA journal_mode=DELETE;")
    con.execute("VACUUM;")
    con.close()

if __name__ == "__main__":
    main()
