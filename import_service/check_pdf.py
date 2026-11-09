"""Check selected account without changing the parser or its stored account label."""
import sys
import math
from pathlib import Path
sys.path.insert(0, '/app')
from ing2db import parse_ing_kontoauszug, compact_orphan_description_rows, clean_descriptions
from import_service.iban import compact, first_german_iban, valid_german_iban

frame, pages = parse_ing_kontoauszug(Path(sys.argv[1]))
found = first_german_iban(pages)
if not valid_german_iban(sys.argv[2]) or found != compact(sys.argv[2]):
    raise SystemExit(1)
if frame.empty:
    raise SystemExit(1)
frame = clean_descriptions(compact_orphan_description_rows(frame, join_with=' '))
try:
    invalid = any(not math.isfinite(float(amount)) for amount in frame['amount'])
except (ValueError, TypeError):
    invalid = True
if invalid:
    raise SystemExit(2)
print(len(frame))
