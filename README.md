# pdf-bank-statements-to-sqlite

Parse your **bank statement PDFs** (currently ING-DiBa, PayPal coming soon)  
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
currently supporting **ING-DiBa** statements, with **PayPal** support actively in development.

---

### 📦 Current Components

| Script | Source | Status | Description |
|:-------|:--------|:--------|:------------|
| `ing2db.py` | ING-DiBa (Germany) | ✅ stable | Parses original PDF statements from ING (Kontoauszug) and imports them into SQLite. Handles multiline descriptions, page headers, footers, and merges split bookings. |
| `paypal2db.py` | PayPal | 🚧 in progress | Reads PayPal PDF statements, detects incoming/outgoing payments, supports multiple formats. |

---


use docker TO-DO: write Docs...


