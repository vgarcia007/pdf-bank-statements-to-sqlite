"""Create synthetic isolated end-to-end data; never point this at the bank data directory."""
import json
import os
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, '/app')
from ing2db import SCHEMA_SQL

root = Path('/fixture')
for p in (root / 'data/import-service', root / 'control'):
    p.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(root / 'data/bank.sqlite3') as db:
    db.executescript(SCHEMA_SQL)
    db.execute('PRAGMA journal_mode=DELETE')
db.close()
(root / 'data/metadata.yaml').write_text('title: Synthetic import test\n')
(root / 'data/import-service/accounts.json').write_text(json.dumps([
    {'type':'paypal','account':'PayPal Test'}, {'type':'ing','account':'DE89 3704 0044 0532 0130 00'}]))
(root / 'token').write_text('synthetic-test-token-only-00000000000000000000000000000000')
# Minimal PDF containing synthetic ING-style text and the public German example IBAN.
lines = ['ING', 'IBAN DE89 3704 0044 0532 0130 00', 'Kontoauszug November 2026',
         '02.11.2026 Gutschrift Rechnung TEST-ING-1 45,67', 'Neuer Saldo 45,67']
content = 'BT /F1 12 Tf 40 800 Td ' + ' 0 -20 Td '.join('(' + line + ') Tj' for line in lines) + ' ET'
objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
           b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
           b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
           ('<< /Length ' + str(len(content)) + ' >>\nstream\n' + content + '\nendstream').encode()]
pdf = bytearray(b'%PDF-1.4\n'); offsets = [0]
for n, obj in enumerate(objects, 1):
    offsets.append(len(pdf)); pdf.extend(str(n).encode() + b' 0 obj\n' + obj + b'\nendobj\n')
xref = len(pdf)
pdf.extend(b'xref\n0 6\n0000000000 65535 f \n')
for offset in offsets[1:]: pdf.extend(f'{offset:010d} 00000 n \n'.encode())
pdf.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
(root / 'synthetic-ing.pdf').write_bytes(pdf)
for p in root.rglob('*'):
    os.chown(p, 1000, 1000)
os.chown(root, 1000, 1000)
