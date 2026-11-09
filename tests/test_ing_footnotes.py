"""ING superscript amount regression tests using synthetic PDFs only."""
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,'/app')
import ing2db
from import_service.service import ClosingConnection


def make_pdf(path, raised=True):
    blocks=[(10,40,800,'ING'),(10,40,780,'IBAN DE89 3704 0044 0532 0130 00'),
            (10,40,750,'02.02.2026'),(10,150,750,'Transfer TEST-FIRST'),(10,490,750,'-123,45'),
            (6 if raised else 10,530,754.5 if raised else 750,'1'),
            (10,40,730,'31.01.2026'),(10,150,730,'TEST-REFERENCE'),
            (10,40,700,'02.02.2026'),(10,150,700,'Transfer TEST-SECOND'),(10,490,700,'-50,00'),
            (10,40,680,'01.02.2026'),(10,150,680,'TEST-OTHER-REFERENCE'),
            (10,40,650,'Neuer Saldo 0,00')]
    content='\n'.join(f'BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET' for size,x,y,text in blocks).encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
             b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream']
    pdf=bytearray(b'%PDF-1.4\n');offsets=[]
    for i,obj in enumerate(objects,1):offsets.append(len(pdf));pdf.extend(str(i).encode()+b' 0 obj\n'+obj+b'\nendobj\n')
    xref=len(pdf);pdf.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets:pdf.extend(f'{offset:010d} 00000 n \n'.encode())
    pdf.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode());path.write_bytes(pdf)


class FootnoteTests(unittest.TestCase):
    def test_first_transfer_amount_is_recognized_without_changing_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'synthetic.pdf';make_pdf(source)
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            frame,_=ing2db.parse_ing_kontoauszug(source)
            frame=ing2db.clean_descriptions(ing2db.compact_orphan_description_rows(frame))
            self.assertEqual([-123.45,-50.0],list(frame['amount']))
            self.assertIn('TEST-FIRST',frame.iloc[0]['description'])
            self.assertIn('TEST-REFERENCE',frame.iloc[0]['description'])
            self.assertEqual(before,hashlib.sha256(source.read_bytes()).hexdigest())

    def test_cli_stores_every_transfer_and_registers_original_file_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'synthetic.pdf';db=Path(folder)/'bank.sqlite3';make_pdf(source)
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            subprocess.run([sys.executable,'/app/ing2db.py',str(source),'--db-path',str(db),'--account','DE89 3704 0044 0532 0130 00'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            with sqlite3.connect(db,factory=ClosingConnection) as con:
                self.assertEqual([(123.45,'out'),(50,'out')],con.execute('SELECT amount,direction FROM transactions ORDER BY id').fetchall())
                self.assertEqual(before,con.execute('SELECT file_sha256 FROM imports').fetchone()[0])
            self.assertEqual(before,hashlib.sha256(source.read_bytes()).hexdigest())

    def test_same_size_extra_digit_is_not_treated_as_a_footnote(self):
        class Page:
            def extract_text(self,mode,visitor_text):
                visitor_text('-12,34',None,None,None,10)
                visitor_text('1',None,None,None,10)
                return 'Transfer -12,341\n'
        self.assertEqual('Transfer -12,341\n',ing2db.extract_ing_page_text(Page()))

    def test_unrecognized_amount_is_rejected_by_upload_precheck(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'synthetic.pdf';make_pdf(source,raised=False)
            result=subprocess.run([sys.executable,'/app/import_service/check_pdf.py',str(source),'DE89 3704 0044 0532 0130 00'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            self.assertEqual(2,result.returncode)

    def test_ambiguous_font_evidence_does_not_strip_unverified_occurrences(self):
        class Page:
            def extract_text(self,mode,visitor_text):
                visitor_text('-12,34',None,None,None,10)
                visitor_text('1',None,None,None,6)
                return 'Transfer -12,341\nOther -12,341\n'
        self.assertEqual('Transfer -12,341\nOther -12,341\n',ing2db.extract_ing_page_text(Page()))

    def test_plain_amount_and_unrelated_small_digit_are_unchanged(self):
        class Page:
            def extract_text(self,mode,visitor_text):
                visitor_text('-12,34',None,None,None,10)
                visitor_text('1',None,None,None,6)
                return 'Transfer -12,34\n1 Other information\n'
        self.assertEqual('Transfer -12,34\n1 Other information\n',ing2db.extract_ing_page_text(Page()))


if __name__=='__main__':unittest.main(verbosity=2)
