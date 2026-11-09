# pdf-bank-statements-to-sqlite

## Uploadverwaltung und aktueller Docker-Betrieb

Der interne Importdienst mit Warteschlange, geschützter API, sicherer
Veröffentlichung und Datasette-Aktualisierung ist in
[IMPORT_SERVICE.md](IMPORT_SERVICE.md) beschrieben. Diese Anleitung gilt für
den aktuellen Compose-Betrieb; Invoice 2 bietet dafür eine Oberfläche unter
Konten. Die folgenden direkten CLI-Beispiele dienen dem separaten Parserbetrieb.
Im Dienstbetrieb Konsolenimporte über `import_service/submit.py` einreichen.

ING-PDFs bleiben unverändert. Der Parser erkennt hochgestellte Fußnotenziffern
hinter Beträgen. PayPal wird über CSV-Exporte importiert.

Parse your **bank statement PDFs** (ING-DiBa PDFs and German PayPal CSV exports)  
into a structured **SQLite** database – ready for [Datasette](https://datasette.io) or any SQL tool.

---

### 💡 Inspiration

Big thanks to [**@kaiklede**](https://github.com/kaiklede)  
and his excellent project [**ing-kontoauszug-parser**](https://github.com/kaiklede/ing-kontoauszug-parser),  
which inspired this repository.  
This project started as a fork in spirit – and grew into a more general, multi-source parser.

---

### 🧭 Overview

The idea is simple:

1. You drop your PDF statements (e.g. `Kontoauszug_2024.pdf`) into a folder.  
2. A parser script (e.g. `ing2db.py`) extracts all bookings:
   - **date**, **description**, **amount**, **direction** (in/out), **account**
3. Everything gets written into a **SQLite** database (`data/bank.sqlite3`).
4. Optionally, you spin up [Datasette](https://datasette.io) to explore, filter or search.

So you end up with one searchable database containing all your transactions —  
currently supporting **ING-DiBa** PDF statements and German **PayPal CSV** exports.

---

### 📦 Current Components

| Script | Source | Status | Description |
|:-------|:--------|:--------|:------------|
| `ing2db.py` | ING-DiBa (Germany) | ✅ stable | Parses original PDF statements from ING (Kontoauszug) and imports them into SQLite. Handles multiline descriptions, page headers, footers, and merges split bookings. |
| `paypalcsv2db.py` | PayPal | ✅ available | Imports German PayPal CSV exports into the same database. |

---

## 🚀 Getting Started

### Prerequisites

- **Docker & Docker Compose** (recommended) — or —
- **Python 3.11+** with `pip` (for standalone usage)

### Quick Start with Docker (Recommended)

1. **Clone the repository:**
   ```bash
   git clone https://github.com/vgarcia007/pdf-bank-statements-to-sqlite.git
   cd pdf-bank-statements-to-sqlite
   ```

2. **Place your ING PDF statements** in the `./data` folder:
   ```bash
   mkdir -p data
   cp /path/to/your/Kontoauszug*.pdf ./data/
   ```

3. **Build the Docker container:**
   ```bash
   make build
   ```

4. **Run the parser:**
   ```bash
   make run ARGS="/app/data"
   ```
   
   This will parse all PDFs in `./data` and create/update `./data/bank.sqlite3`.

5. **View your transactions with Datasette:**
   ```bash
   docker compose up viewer
   ```
   
   Then open http://localhost:8123 in your browser to explore your transactions.

---

## 📖 Usage Guide

### Running with Docker (Using Makefile)

The `Makefile` provides convenient shortcuts for common Docker operations:

#### **`make build`**
Builds the Docker image with all dependencies.
```bash
make build
```

#### **`make up`**
Starts the container in the background (detached mode).
```bash
make up
```

#### **`make down`**
Stops and removes the container.
```bash
make down
```

#### **`make restart`**
Restarts the container (runs `make down` then `make up`).
```bash
make restart
```

#### **`make logs`**
Shows and follows container logs in real-time.
```bash
make logs
```

#### **`make sh`**
Opens an interactive bash shell inside the running container.
```bash
make sh
```

#### **`make run ARGS="..."`**
Executes the parser script inside the container with custom arguments.

**Examples:**
```bash
# Parse all PDFs in /app/data
make run ARGS="/app/data"

# Parse a single PDF with a custom account identifier
make run ARGS="/app/data/Kontoauszug_2024.pdf --account 'DE89 1234 5678 9012 3456 78'"

# Parse with CSV export
make run ARGS="/app/data --csv-out /app/data/transactions.csv"

# Dry run (parse without writing to database)
make run ARGS="/app/data --dry-run"

# Force re-import of already processed PDFs
make run ARGS="/app/data --force"
```

#### **`make clean`**
Removes containers, images, and volumes (but keeps `./data` folder on your host).
```bash
make clean
```

---

### Running Standalone (Without Docker)

If you prefer to run the parser directly with Python:

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Run the parser:**
   ```bash
   python3 ing2db.py /path/to/pdfs
   ```

---

## 🔧 Parser Options (`ing2db.py`)

The `ing2db.py` script accepts the following command-line arguments:

### Required Arguments

- **`path`**  
  Path to a single PDF file or a directory containing ING PDF statements.  
  PDFs must contain "kontoauszug" (case-insensitive) in the filename.

### Optional Arguments

- **`-a, --account <IBAN>`**  
  Specify the account identifier (IBAN or custom string).  
  If omitted, the script attempts to extract the IBAN from the PDF.  
  If extraction fails, the script will abort.
  
  ```bash
  python3 ing2db.py ./data --account "DE89 1234 5678 9012 3456 78"
  ```

- **`--db-path <path>`**  
  Path to the SQLite database file.  
  **Default:** `./data/bank.sqlite3`
  
  ```bash
  python3 ing2db.py ./data --db-path /custom/path/my_bank.db
  ```

- **`--csv-out <path>`**  
  Export parsed transactions to a CSV file (in addition to the database).
  
  ```bash
  python3 ing2db.py ./data --csv-out ./transactions.csv
  ```

- **`--sep <delimiter>`**  
  CSV column delimiter.  
  **Default:** `;` (semicolon)
  
  ```bash
  python3 ing2db.py ./data --csv-out ./data.csv --sep ","
  ```

- **`--keep-newlines`**  
  Preserve newlines in transaction descriptions instead of joining them with spaces.
  
  ```bash
  python3 ing2db.py ./data --keep-newlines
  ```

- **`--force`**  
  Re-import PDFs even if they have already been processed.  
  By default, the script tracks imported files by SHA-256 hash to avoid duplicates.
  
  ```bash
  python3 ing2db.py ./data --force
  ```

- **`--dry-run`**  
  Parse PDFs and show statistics without writing to the database.  
  Useful for testing or previewing results.
  
  ```bash
  python3 ing2db.py ./data --dry-run
  ```

---

## 💡 Usage Examples

### Example 1: Import a single PDF
```bash
python3 ing2db.py ./data/Kontoauszug_Januar_2024.pdf
```

### Example 2: Import all PDFs from a directory
```bash
python3 ing2db.py ./data
```

### Example 3: Import with custom account and CSV export
```bash
python3 ing2db.py ./data \
  --account "DE89 1234 5678 9012 3456 78" \
  --csv-out ./transactions.csv
```

### Example 4: Preview without writing to database
```bash
python3 ing2db.py ./data --dry-run
```

### Example 5: Re-import previously processed files
```bash
python3 ing2db.py ./data --force
```

### Example 6: Using Docker with custom database location
```bash
make run ARGS="/app/data --db-path /app/data/custom.db"
```

---

## 🔍 Viewing Your Data with Datasette

Once you've imported your transactions, you can explore them with Datasette:

1. **Start the Datasette viewer:**
   ```bash
   docker compose up viewer
   ```

2. **Open in your browser:**
   ```
   http://localhost:8123
   ```

3. **Explore your transactions:**
   - Filter by date range, amount, description keywords
   - Run SQL queries directly
   - Export filtered results to CSV
   - Visualize spending patterns

You can also use any SQLite client (DB Browser for SQLite, DBeaver, etc.) to query `./data/bank.sqlite3`.

---

## 🗄️ Database Schema

The SQLite database contains two main tables:

### `transactions` table
- `id` (INTEGER, PRIMARY KEY) — Auto-incrementing ID
- `tx_hash` (TEXT, UNIQUE) — Unique transaction identifier (SHA-256)
- `account` (TEXT) — Account identifier (IBAN)
- `date` (TEXT) — Transaction date (ISO format: YYYY-MM-DD)
- `description` (TEXT) — Transaction description
- `amount` (NUMERIC) — Transaction amount (always positive)
- `direction` (TEXT) — "in" or "out"
- `category` (TEXT) — Optional category (currently NULL)
- `currency` (TEXT) — Currency code (default: EUR)
- `source_file` (TEXT) — Original PDF filename
- `source_page_first` (INTEGER) — First page number (nullable)
- `created_at` (TIMESTAMP) — Record creation timestamp

### `imports` table
- `id` (INTEGER, PRIMARY KEY) — Auto-incrementing ID
- `source_file` (TEXT) — Original PDF filename
- `file_sha256` (TEXT, UNIQUE) — SHA-256 hash of imported PDF
- `imported_at` (TIMESTAMP) — Import timestamp

---

## 🐛 Troubleshooting

### "No matching PDFs found"
**Problem:** No PDFs were found in the specified directory.  
**Solution:** Ensure PDF filenames contain "kontoauszug" (case-insensitive). Rename files if necessary:
```bash
mv statement.pdf Kontoauszug_2024.pdf
```

### "No account identifier provided and no IBAN found in PDFs"
**Problem:** The parser couldn't extract the IBAN from the PDF.  
**Solution:** Manually specify the account with the `--account` flag:
```bash
python3 ing2db.py ./data --account "DE89 1234 5678 9012 3456 78"
```

### "No transactions parsed"
**Problem:** The PDF format might not match the expected ING-DiBa layout.  
**Solution:** 
- Verify the PDF is an ING-DiBa "Kontoauszug" (account statement)
- Try opening the PDF to ensure it's not corrupted or password-protected
- Use `--dry-run` to see parser output without modifying the database

### Container issues
**Problem:** Docker container won't start or build fails.  
**Solution:**
```bash
# Clean up and rebuild
make clean
make build

# Check Docker logs
make logs
```

---

## 📝 Notes

- **Idempotent imports:** The script tracks imported files by SHA-256 hash. Re-running the script on the same PDFs won't create duplicates (unless you use `--force`).
- **Database format:** After each run, the database is optimized (VACUUM) and set to DELETE journal mode for better compatibility with SQLite viewers.
- **File naming:** PDFs must contain "kontoauszug" in the filename to be automatically detected when parsing a directory.

---

## 🤝 Contributing

Contributions are welcome! Feel free to open issues or submit pull requests.

---

## 📄 License

See the repository for license details.


