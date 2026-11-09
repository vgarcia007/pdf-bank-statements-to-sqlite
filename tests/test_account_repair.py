"""Synthetic account repair, rollback and full-IBAN tests in the parser container."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, '/app')
from ing2db import SCHEMA_SQL, extract_account_identifier
from import_service.iban import first_german_iban, valid_german_iban
from import_service.repair_accounts import AccountRepair
from import_service.service import Service, Rejected, digest, ClosingConnection

OLD='DE89 3704 0044 0532 0130'
FULL=OLD+' 00'


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        with sqlite3.connect(root/'bank.sqlite3',factory=ClosingConnection) as db:
            db.executescript(SCHEMA_SQL)
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute("INSERT INTO transactions(account,date,amount,direction,description,currency,tx_hash,source_file) VALUES(?,'2026-01-01',12.34,'in','Synthetic','EUR',?,'synthetic.pdf')",(OLD,'a'*64))
            db.execute("INSERT INTO imports(source_file,file_sha256) VALUES('synthetic.pdf',?)",('b'*64,))
        accounts=root/'accounts.json';accounts.write_text(json.dumps([{'type':'ing','account':OLD},{'type':'paypal','account':'PayPal'}]))
        self.s=Service(root,root/'control',accounts)
        self.s.refresh=lambda generation: None
        self.repair=AccountRepair(self.s,{OLD:FULL})

    def tearDown(self): self.tmp.cleanup()

    def row(self):
        with sqlite3.connect(self.s.live,factory=ClosingConnection) as db:return db.execute('SELECT * FROM transactions').fetchone()

    def test_preview_is_read_only(self):
        before=digest(self.s.live)
        self.assertEqual({'changed_transactions':1,'accounts':1},self.repair.run())
        self.assertEqual(before,digest(self.s.live))

    def test_repair_retains_every_other_field_and_is_idempotent(self):
        before=self.row()
        self.repair.run(True)
        after=self.row()
        self.assertEqual(FULL,after[1])
        self.assertEqual(before[:1]+before[2:],after[:1]+after[2:])
        self.assertEqual(FULL,self.s.accounts()[0]['account'])
        self.assertEqual(FULL,self.s.canonical_account(OLD))
        self.assertEqual({'changed_transactions':0,'accounts':0},self.repair.run(True))

    def test_viewer_failure_restores_database_and_configuration(self):
        def refresh(generation):
            if generation.startswith('account-repair-'):raise Rejected('Synthetic viewer failure')
        self.s.refresh=refresh
        before=self.row()
        with self.assertRaises(Rejected):self.repair.run(True)
        self.assertEqual(before,self.row())
        self.assertEqual(OLD,self.s.accounts()[0]['account'])
        self.assertEqual({},self.s.aliases())
        self.assertFalse(self.repair.marker.exists())

    def test_crash_blocks_worker_until_maintenance_resumes(self):
        self.s.refresh=lambda generation: (_ for _ in ()).throw(SystemExit('Power failure'))
        with self.assertRaises(SystemExit):self.repair.run(True)
        self.assertTrue(self.repair.marker.exists())
        with self.assertRaises(Rejected):self.s.recover()
        self.s.refresh=lambda generation: None
        self.repair.run(True)
        self.assertEqual(FULL,self.row()[1])
        self.assertFalse(self.repair.marker.exists())

    def test_checksum_and_exact_suffix_are_checked(self):
        self.assertTrue(valid_german_iban(FULL))
        self.assertEqual(FULL.replace(' ',''),first_german_iban(['IBAN '+FULL]))
        self.assertIsNone(first_german_iban(['IBAN '+OLD+' 01']))
        self.assertIsNone(first_german_iban(['IBAN '+OLD]))
        with self.assertRaises(Rejected):AccountRepair(self.s,{OLD:OLD+' 01'})
        # Demonstrate that the unchanged original parser still truncates, while the wrapper does not.
        self.assertEqual(OLD,extract_account_identifier(['IBAN '+FULL]))


if __name__=='__main__':unittest.main(verbosity=2)
