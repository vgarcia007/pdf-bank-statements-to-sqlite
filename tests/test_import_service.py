"""Run in the parser/importer container. All databases and uploads are temporary."""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from unittest import mock
from http.server import ThreadingHTTPServer

sys.path.insert(0, '/app')
from ing2db import SCHEMA_SQL
from import_service.service import Service, Rejected, digest, durable_json, snapshot, handler

HEADER = 'Datum,Beschreibung,Währung,Brutto,Entgelt,Netto,Transaktionscode\n'
CSV = (HEADER + '01.11.2026,Testbuchung,EUR,"12,34","0,00","12,34",SYNTHETIC-1\n').encode()


class LocalService(Service):
    def refresh(self, generation):
        self.refreshed = generation


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        with sqlite3.connect(self.root / 'bank.sqlite3') as db:
            db.executescript(SCHEMA_SQL)
            db.execute('PRAGMA journal_mode=DELETE')
        db.close()
        accounts = self.root / 'accounts.json'
        accounts.write_text(json.dumps([{'type': 'paypal', 'account': 'PayPal Test'}, {'type': 'ing', 'account': 'ING Test'}]))
        self.service = LocalService(self.root, self.root / 'control', accounts)

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, key='1' * 32, data=CSV, name='Test.CSV', kind='paypal', account='PayPal Test'):
        return dict(id=key, type=kind, account=account, files=[dict(name=name, data=base64.b64encode(data).decode())])

    def count(self):
        with sqlite3.connect(self.service.live) as db:
            return db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]

    def run_job(self, payload=None):
        job = self.service.accept(payload or self.payload())
        self.service.process(job)
        return self.service.load(job['id'])

    def test_real_paypal_parser_and_repeat(self):
        before = digest(self.service.live)
        job = self.run_job()
        self.assertEqual(('completed', 1), (job['status'], job['inserted']))
        self.assertEqual(1, self.count())
        self.assertNotEqual(before, digest(self.service.live))
        self.assertTrue((self.service.state / 'backups' / (job['id'] + '.sqlite3')).exists())
        next_job = self.run_job(self.payload(key='2' * 32, name='Umbenannt.csv'))
        self.assertEqual(('completed', 1, 0), (next_job['status'], next_job['skipped'], next_job['inserted']))
        self.assertEqual(1, self.count())

    def test_idempotent_handoff_and_collision(self):
        a = self.service.accept(self.payload())
        self.assertEqual(a, self.service.accept(self.payload()))
        with self.assertRaises(Rejected):
            self.service.accept(self.payload(name='other.csv'))
        self.assertEqual(1, len(self.service.jobs()))

    def test_validation_formats_and_money(self):
        for payload in [self.payload(data=b'broken'), self.payload(name='../Test.csv'),
                        self.payload(account='Unknown'), self.payload(kind='ing', account='ING Test', name='a.pdf', data=b'bad'),
                        self.payload(data=CSV.replace(b'12,34', b'not-money')),
                        self.payload(data=CSV.replace(b'01.11.2026', b'31.02.2026')),
                        self.payload(data=HEADER.encode()), self.payload(data=b'a' * (5 * 1024 * 1024 + 1))]:
            with self.assertRaises(Rejected):
                self.service.accept(payload)
        self.assertEqual(0, self.count())

    def test_overlapping_export_stays_in_review(self):
        self.run_job()
        old = digest(self.service.live)
        job = self.run_job(self.payload(key='2' * 32, data=b'\xef\xbb\xbf' + CSV))
        self.assertEqual('review', job['status'])
        self.assertEqual(old, digest(self.service.live))
        self.assertEqual(1, self.count())

    def test_whole_batch_discarded_on_second_failure(self):
        original = self.service.runner
        calls = []
        def runner(job, source, work):
            calls.append(source)
            if len(calls) == 2:
                raise Rejected('Synthetic second-file failure')
            original(job, source, work)
        self.service.runner = runner
        payload = self.payload()
        payload['files'].extend(self.payload(data=CSV.replace(b'SYNTHETIC-1', b'SYNTHETIC-2'))['files'])
        before = digest(self.service.live)
        job = self.run_job(payload)
        self.assertEqual('failed', job['status'])
        self.assertEqual(before, digest(self.service.live))
        self.assertEqual(0, self.count())

    def test_zero_exit_without_registration_is_failure(self):
        self.service.runner = lambda *args: None
        job = self.run_job()
        self.assertEqual('failed', job['status'])
        self.assertEqual(0, self.count())

    def test_duplicate_rows_within_csv_are_not_silently_lost(self):
        row = CSV.split(b'\n')[1] + b'\n'
        job = self.run_job(self.payload(data=CSV + row))
        self.assertEqual('failed', job['status'])
        self.assertIn('Anzahl', job['message'])
        self.assertEqual(0, self.count())

    def test_parallel_handoff_creates_one_job(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.service.accept(self.payload()))) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(2, len(results))
        self.assertEqual(1, len(self.service.jobs()))

    def test_worker_serializes_distinct_queued_jobs(self):
        original = self.service.runner
        active = []
        def runner(job, source, work):
            active.append(1)
            self.assertEqual(1, len(active))
            try: return original(job, source, work)
            finally: active.pop()
        self.service.runner = runner
        first = self.service.accept(self.payload())
        second = self.service.accept(self.payload(key='2' * 32, data=CSV.replace(b'SYNTHETIC-1', b'SYNTHETIC-2')))
        worker = threading.Thread(target=self.service.worker)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if all(self.service.load(j['id'])['status'] == 'completed' for j in (first,second)): break
                time.sleep(.05)
            self.assertEqual(['completed','completed'], [self.service.load(j['id'])['status'] for j in (first,second)])
            self.assertEqual(2, self.count())
        finally:
            self.service.stop.set()
            worker.join(timeout=5)

    def test_no_space_rejects_before_acceptance(self):
        with mock.patch('import_service.service.shutil.disk_usage', return_value=type('Space', (), {'free': 0})()):
            with self.assertRaises(Rejected): self.service.accept(self.payload())
        self.assertEqual([], self.service.jobs())

    def test_parser_timeout_keeps_live_database(self):
        self.service.timeout = .001
        before = digest(self.service.live)
        job = self.run_job()
        self.assertEqual('failed', job['status'])
        self.assertIn('Zeitlimit', job['message'])
        self.assertEqual(before, digest(self.service.live))

    def test_crash_before_replace_recovers_old_database(self):
        original_replace = __import__('os').replace
        def replace(source, target):
            if Path(target) == self.service.live:
                raise SystemExit('Power failure before replace')
            original_replace(source, target)
        job = self.service.accept(self.payload())
        with mock.patch('import_service.service.os.replace', side_effect=replace):
            with self.assertRaises(SystemExit): self.service.process(job)
        self.assertEqual(0, self.count())
        self.service.recover()
        self.assertEqual(0, self.count())
        self.assertFalse((self.service.state / 'publication.json').exists())

    def test_crash_after_completed_record_preserves_new_database(self):
        original_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == self.service.state / 'publication.json':
                raise SystemExit('Power failure after completion record')
            return original_unlink(path, *args, **kwargs)
        job = self.service.accept(self.payload())
        with mock.patch('pathlib.Path.unlink', new=unlink):
            with self.assertRaises(SystemExit): self.service.process(job)
        self.assertEqual(1, self.count())
        self.service.recover()
        self.assertEqual('completed', self.service.load(job['id'])['status'])
        self.assertEqual(1, self.count())
        self.assertFalse((self.service.state / 'publication.json').exists())

    def test_refresh_failure_rolls_back(self):
        original_refresh = self.service.refresh
        def refresh(generation):
            if not generation.startswith('rollback-'):
                raise Rejected('Synthetic viewer failure')
            original_refresh(generation)
        self.service.refresh = refresh
        job = self.run_job()
        self.assertEqual('failed', job['status'])
        self.assertEqual(0, self.count())
        self.assertFalse((self.service.state / 'publication.json').exists())

    def test_crash_after_replace_recovers_previous_database(self):
        def die(generation):
            raise SystemExit('Synthetic power failure')
        self.service.refresh = die
        job = self.service.accept(self.payload())
        with self.assertRaises(SystemExit):
            self.service.process(job)
        self.assertEqual(1, self.count())
        self.service.refresh = lambda generation: None
        self.service.recover()
        self.assertEqual(0, self.count())
        self.assertEqual('failed', self.service.load(job['id'])['status'])

    def test_rollback_refresh_failure_can_recover_again(self):
        self.service.refresh = lambda generation: (_ for _ in ()).throw(Rejected('offline'))
        job = self.run_job()
        self.assertTrue((self.service.state / 'publication.json').exists())
        self.assertEqual(0, self.count())
        self.service.refresh = lambda generation: None
        self.service.recover()
        self.assertFalse((self.service.state / 'publication.json').exists())

    def test_external_writer_detected(self):
        original = self.service.runner
        def runner(job, source, work):
            original(job, source, work)
            with sqlite3.connect(self.service.live) as db:
                db.execute("INSERT INTO imports(source_file,file_sha256) VALUES('synthetic-external','external')")
        self.service.runner = runner
        job = self.run_job()
        self.assertEqual('failed', job['status'])
        self.assertEqual(0, self.count())
        with sqlite3.connect(self.service.live) as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM imports').fetchone()[0])

    def test_interrupted_job_not_automatically_retried(self):
        job = self.service.accept(self.payload())
        self.service.save(job, status='processing')
        self.service.recover()
        self.assertEqual('failed', self.service.load(job['id'])['status'])
        self.assertEqual(0, self.count())

    def test_known_file_cannot_be_assigned_to_another_account(self):
        self.run_job()
        accounts = self.service.accounts()
        accounts.append({'type': 'paypal', 'account': 'Other'})
        self.service.accounts_file.write_text(json.dumps(accounts))
        job = self.run_job(self.payload(key='2' * 32, account='Other'))
        self.assertEqual('failed', job['status'])
        self.assertEqual(1, self.count())

    def test_api_auth_and_persistent_job(self):
        alive = type('Alive', (), {'is_alive': lambda self: True})()
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.service, 'x' * 64, alive))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + '/v1/jobs')
            self.assertEqual(401, error.exception.code)
            request = urllib.request.Request(base + '/v1/jobs', json.dumps(self.payload()).encode(),
                      {'Authorization': 'Bearer ' + 'x' * 64, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(202, response.status)
                value = json.load(response)
                self.assertEqual('queued', value['job']['status'])
                self.assertNotIn('payload_hash', value['job'])
            with urllib.request.urlopen(urllib.request.Request(base + '/v1/jobs', headers={'Authorization': 'Bearer ' + 'x' * 64})) as response:
                self.assertEqual(1, len(json.load(response)['jobs']))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
