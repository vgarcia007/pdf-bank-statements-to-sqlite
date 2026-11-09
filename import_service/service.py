"""Private import API and serial worker. Existing parsers are executed unchanged."""
import base64
import csv
import datetime as dt
import decimal
import fcntl
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
try:
    from .iban import compact
except ImportError:
    from iban import compact

MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
MAX_BODY = 29 * 1024 * 1024
TERMINAL = {'completed', 'failed', 'review'}


class Rejected(Exception):
    pass


def durable_json(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    sync_dir(path.parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect_ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, factory=ClosingConnection)


def snapshot(source, target):
    with connect_ro(source) as src, sqlite3.connect(target, factory=ClosingConnection) as dst:
        src.backup(dst)
    with open(target, 'rb') as stream:
        os.fsync(stream.fileno())


def fingerprint(row, aliases=None):
    account = next((full for old, full in (aliases or {}).items() if compact(old) == compact(row['account'])), row['account'])
    return (account, row['date'], str(decimal.Decimal(str(row['amount'])).quantize(decimal.Decimal('.01'))),
            row['direction'], ' '.join(row['description'].split()), row['currency'])


class Service:
    def __init__(self, root, control, accounts, runner=None, timeout=600, refresh_timeout=45):
        self.root, self.control = Path(root), Path(control)
        self.state = self.root / 'import-service'
        self.live = self.root / 'bank.sqlite3'
        self.accounts_file = Path(accounts)
        self.timeout, self.refresh_timeout = timeout, refresh_timeout
        self.runner = runner or self.run_parser
        self.stop = threading.Event()
        self.accept_lock = threading.Lock()
        for directory in (self.state, self.state / 'jobs', self.state / 'originals', self.state / 'backups', self.control):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def accounts(self):
        value = json.loads(self.accounts_file.read_text())
        if not isinstance(value, list) or not value:
            raise Rejected('Keine Importkonten konfiguriert.')
        for a in value:
            if not isinstance(a, dict) or a.get('type') not in ('ing', 'paypal') or not isinstance(a.get('account'), str) or not a['account']:
                raise Rejected('Importkonten sind nicht korrekt konfiguriert.')
        return value

    def aliases(self):
        path = self.state / 'account-aliases.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def canonical_account(self, account):
        return next((full for old, full in self.aliases().items() if compact(old) == compact(account)), account)

    def path(self, key):
        if not re.fullmatch(r'[a-f0-9]{32}', key):
            raise Rejected('Ungültige Auftragskennung.')
        return self.state / 'jobs' / (key + '.json')

    def load(self, key):
        path = self.path(key)
        if not path.exists():
            raise Rejected('Importauftrag nicht gefunden.')
        return json.loads(path.read_text())

    def save(self, job, **changes):
        job.update(changes)
        job['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        durable_json(self.path(job['id']), job)

    def jobs(self):
        return sorted((json.loads(p.read_text()) for p in (self.state / 'jobs').glob('*.json')),
                      key=lambda j: (j['created_at'], j['id']))

    @staticmethod
    def public(job):
        return {k: v for k, v in job.items() if k not in ('payload_hash',)}

    def accept(self, payload):
        key = payload.get('id', '')
        self.path(key)
        kind, account = payload.get('type'), payload.get('account')
        if isinstance(account, str): account = self.canonical_account(account)
        if {'type': kind, 'account': account} not in self.accounts():
            raise Rejected('Bitte ein konfiguriertes Konto und das passende Format auswählen.')
        files = payload.get('files')
        if not isinstance(files, list) or not 1 <= len(files) <= 20:
            raise Rejected('Bitte zwischen einer und 20 Dateien auswählen.')
        decoded, total = [], 0
        extension = '.pdf' if kind == 'ing' else '.csv'
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('data'), str):
                raise Rejected('Ungültiger Datei-Upload.')
            name = item['name']
            if not name or len(name) > 180 or '/' in name or '\\' in name or any(ord(c) < 32 for c in name) or not name.lower().endswith(extension):
                raise Rejected('Dateiname oder Dateiformat ist ungültig.')
            if len(item['data']) > ((MAX_FILE + 2) // 3) * 4:
                raise Rejected('Eine Datei darf höchstens 5 MiB groß sein.')
            try:
                data = base64.b64decode(item['data'], validate=True)
            except (ValueError, TypeError):
                raise Rejected('Ungültiger Datei-Upload.')
            total += len(data)
            if not data or len(data) > MAX_FILE or total > MAX_TOTAL:
                raise Rejected('Höchstens 5 MiB pro Datei und 20 MiB pro Auftrag erlaubt.')
            if kind == 'ing' and not data.startswith(b'%PDF-'):
                raise Rejected('Die ausgewählte Datei ist kein PDF.')
            if kind == 'paypal':
                self.validate_csv(data)
            sha = hashlib.sha256(data).hexdigest()
            decoded.append((name, sha, data))
        signature = hashlib.sha256(json.dumps([kind, account, [(n, s) for n, s, _ in decoded]], ensure_ascii=False).encode()).hexdigest()
        with self.accept_lock:
            if self.path(key).exists():
                previous = self.load(key)
                if previous['payload_hash'] != signature:
                    raise Rejected('Diese Auftragskennung wurde bereits für andere Dateien verwendet.')
                return previous
            if len([j for j in self.jobs() if j['status'] not in TERMINAL]) >= 20:
                raise Rejected('Die Warteschlange ist voll. Bitte später erneut versuchen.')
            if shutil.disk_usage(self.root).free < total + self.live.stat().st_size * 3 + 100 * 1024 * 1024:
                raise Rejected('Für einen sicheren Import fehlt freier Speicherplatz.')
            for _, sha, data in decoded:
                dest = self.state / 'originals' / (sha + extension)
                if not dest.exists():
                    tmp = dest.with_suffix('.upload')
                    with tmp.open('wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(tmp, dest)
            sync_dir(self.state / 'originals')
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            job = dict(id=key, type=kind, account=account, status='queued', created_at=now,
                       payload_hash=signature, current=0, inserted=0, skipped=0, message='Wartet auf Verarbeitung.',
                       files=[dict(name=n, sha256=s, status='pending', inserted=0) for n, s, _ in decoded])
            self.save(job)
            return job

    @staticmethod
    def validate_csv(data):
        try:
            text = data.decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(text), strict=True)
            required = {'Datum', 'Beschreibung', 'Währung', 'Brutto', 'Entgelt', 'Netto', 'Transaktionscode'}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError()
            count = 0
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError()
                dt.datetime.strptime(row['Datum'].strip(), '%d.%m.%Y')
                for col in ('Brutto', 'Entgelt', 'Netto'):
                    value = row[col].strip().replace('\u00a0', '').replace('\u202f', '')
                    if not re.fullmatch(r'-?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d{1,2})?-?', value) or (value.startswith('-') and value.endswith('-')):
                        raise ValueError()
                if not re.fullmatch('[A-Z]{3}', row['Währung'].strip()) or not row['Transaktionscode'].strip():
                    raise ValueError()
                count += 1
            if not count:
                raise ValueError()
            return count
        except (UnicodeError, ValueError, csv.Error, TypeError):
            raise Rejected('Die CSV enthält keine vollständig lesbaren PayPal-Buchungen im erwarteten deutschen Exportformat.')

    def run_parser(self, job, source, work):
        script = '/app/ing2db.py' if job['type'] == 'ing' else '/app/paypalcsv2db.py'
        try:
            # Output can contain private transaction data. Never send it to HTTP or Docker logs.
            with open(os.devnull, 'wb') as sink:
                if job['type'] == 'ing':
                    check = subprocess.run([sys.executable, '/app/import_service/check_pdf.py', str(source), job['account']],
                                           stdout=subprocess.PIPE, stderr=sink, timeout=self.timeout)
                    if check.returncode == 2:
                        raise Rejected('Mindestens eine Buchung hat keinen vollständig lesbaren Betrag. Nichts übernommen; bitte den Auszug prüfen.')
                    if check.returncode:
                        raise Rejected('PDF nicht lesbar oder Konto passt nicht zur Auswahl. Bitte einen originalen ING-Auszug und das zugehörige Konto auswählen.')
                    expected = int(check.stdout.strip())
                else:
                    expected = self.validate_csv(source.read_bytes())
                result = subprocess.run([sys.executable, script, str(source), '--db-path', str(work),
                                         '--account', job['account']], stdout=sink, stderr=sink, timeout=self.timeout)
            if result.returncode:
                raise Rejected('Eine Datei konnte nicht vollständig verarbeitet werden. Der bisherige Datenstand bleibt erhalten.')
            return expected
        except subprocess.TimeoutExpired:
            raise Rejected('Die Verarbeitung hat das Zeitlimit überschritten. Der bisherige Datenstand bleibt erhalten.')

    def process(self, job):
        work = self.state / (job['id'] + '.sqlite3')
        try:
            job['account'] = self.canonical_account(job['account'])
            self.save(job, status='processing', message='Arbeitskopie wird erstellt.')
            if shutil.disk_usage(self.root).free < self.live.stat().st_size * 3 + 100 * 1024 * 1024:
                raise Rejected('Für einen sicheren Import fehlt freier Speicherplatz. Nichts übernommen.')
            original_hash = digest(self.live)
            if any(Path(str(self.live) + suffix).exists() for suffix in ('-wal', '-journal')):
                raise Rejected('Die Bankdatenbank wird außerhalb des Importdienstes bearbeitet. Bitte den Konsolenimport beenden.')
            snapshot(self.live, work)
            with connect_ro(work) as db:
                before = db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
                max_id = db.execute('SELECT COALESCE(MAX(id),0) FROM transactions').fetchone()[0]
            for index, file in enumerate(job['files'], 1):
                self.save(job, current=index, message=f'Datei {index} von {len(job["files"])} wird verarbeitet.')
                with connect_ro(work) as db:
                    existing = db.execute('SELECT source_file FROM imports WHERE file_sha256=?', (file['sha256'],)).fetchone()
                    count = db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
                    if existing:
                        stored_accounts = {r[0] for r in db.execute('SELECT DISTINCT account FROM transactions WHERE source_file=?', existing)}
                        if stored_accounts and stored_accounts != {job['account']}:
                            raise Rejected('Eine bekannte Datei gehört zu einem anderen Konto. Bitte die Kontoauswahl prüfen.')
                        file['status'] = 'skipped'
                        job['skipped'] += 1
                        self.save(job)
                        continue
                ext = '.pdf' if job['type'] == 'ing' else '.csv'
                source = self.state / 'originals' / (file['sha256'] + ext)
                if digest(source) != file['sha256']:
                    raise Rejected('Die Prüfsumme einer Quelldatei stimmt nicht mehr.')
                expected = self.runner(job, source, work)
                with connect_ro(work) as db:
                    registered = db.execute('SELECT 1 FROM imports WHERE file_sha256=?', (file['sha256'],)).fetchone()
                    added = db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] - count
                if not registered or added <= 0:
                    raise Rejected('Eine Datei lieferte keine neuen, vollständig registrierten Buchungen. Bitte Format und Inhalt prüfen.')
                if expected is not None and added != expected:
                    raise Rejected(f'Die Anzahl gelesener und gespeicherter Buchungen stimmt nicht überein ({expected} gelesen, {added} gespeichert). Nichts übernommen; bitte den Auszug prüfen.')
                file.update(status='processed', inserted=added)
                self.save(job)
            self.save(job, status='validating', message='Buchungen und Datenbank werden geprüft.')
            aliases = self.aliases()
            with connect_ro(work) as db:
                if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise Rejected('Die Prüfung der neuen Datenbank ist fehlgeschlagen.')
                db.row_factory = sqlite3.Row
                known = set()
                for row in db.execute('SELECT * FROM transactions WHERE id<=?', (max_id,)):
                    known.add(fingerprint(row, aliases))
                new = list(db.execute('SELECT * FROM transactions WHERE id>?', (max_id,)))
                seen = set()
                for row in new:
                    dt.date.fromisoformat(row['date'])
                    amount = decimal.Decimal(str(row['amount']))
                    if row['account'] != job['account'] or row['direction'] not in ('in', 'out') or not amount.is_finite() or amount < 0 or not re.fullmatch('[A-Z]{3}', row['currency']):
                        raise Rejected('Die neuen Buchungen enthalten ungültige Angaben.')
                    fp = fingerprint(row, aliases)
                    if fp in known or fp in seen:
                        self.save(job, status='review', message='Mögliche doppelte Buchungen aus überlappenden Auszügen erkannt. Nichts übernommen. Bitte die Dateien prüfen.')
                        return
                    seen.add(fp)
                after = db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
            if after == before:
                self.save(job, status='completed', message='Alle Dateien sind bereits bekannt. Keine Datenänderung.', inserted=0)
                return
            if digest(self.live) != original_hash or any(Path(str(self.live) + s).exists() for s in ('-wal', '-journal')):
                raise Rejected('Der Bankbestand wurde zwischenzeitlich außerhalb des Dienstes geändert. Nichts übernommen; bitte erneut starten.')
            self.publish(job, work, original_hash, after - before)
        except Rejected as error:
            if job['status'] == 'processing' and 0 < job['current'] <= len(job['files']):
                job['files'][job['current'] - 1]['status'] = 'failed'
            self.save(job, status='failed', message=str(error))
        except Exception:
            self.save(job, status='failed', message='Der Import konnte nicht sicher abgeschlossen werden. Bitte den Auftragsstatus und den Dienst prüfen.')
        finally:
            # A pending publication must retain its recovery files.
            if not (self.state / 'publication.json').exists():
                for suffix in ('', '-wal', '-shm', '-journal'):
                    Path(str(work) + suffix).unlink(missing_ok=True)

    def refresh(self, generation):
        durable_json(self.control / 'requested.json', {'generation': generation})
        deadline = time.monotonic() + self.refresh_timeout
        while time.monotonic() < deadline:
            try:
                value = json.loads((self.control / 'ready.json').read_text())
                if value.get('generation') == generation:
                    return
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(.2)
        raise Rejected('Datasette konnte den neuen Stand nicht bestätigen.')

    def publish(self, job, work, previous_hash, inserted):
        backup = self.state / 'backups' / (job['id'] + '.sqlite3')
        snapshot(self.live, backup)
        if digest(self.live) != previous_hash:
            raise Rejected('Der Bankbestand wurde während der Sicherung geändert. Nichts übernommen.')
        with open(work, 'rb') as stream:
            os.fsync(stream.fileno())
        journal = dict(id=job['id'], previous_hash=previous_hash, backup_hash=digest(backup), new_hash=digest(work), inserted=inserted)
        self.save(job, status='publishing', message='Geprüfter Datenstand wird bereitgestellt.')
        durable_json(self.state / 'publication.json', journal)
        os.replace(work, self.live)
        sync_dir(self.root)
        try:
            self.refresh(job['id'])
        except Rejected:
            self.rollback(journal, job)
            return
        self.save(job, status='completed', inserted=inserted, message=f'{inserted} neue Buchungen übernommen.')
        (self.state / 'publication.json').unlink()
        sync_dir(self.state)
        # Keep the last ten successful pre-import snapshots; originals and history persist.
        backups = sorted((self.state / 'backups').glob('*.sqlite3'), key=lambda p: p.stat().st_mtime)
        for obsolete in backups[:-10]:
            obsolete.unlink()

    def rollback(self, journal, job):
        backup = self.state / 'backups' / (journal['id'] + '.sqlite3')
        if digest(self.live) not in (journal['previous_hash'], journal['new_hash'], journal['backup_hash']):
            raise Rejected('Unerwarteter Datenstand bei Wiederherstellung. Dienst muss geprüft werden; weitere Importe bleiben gesperrt.')
        recovery = self.state / 'recovery.sqlite3'
        if digest(backup) != journal['backup_hash']:
            raise Rejected('Die Rückfallsicherung ist beschädigt. Weitere Importe bleiben gesperrt.')
        shutil.copyfile(backup, recovery)
        with recovery.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(recovery, self.live)
        sync_dir(self.root)
        self.refresh('rollback-' + journal['id'])
        self.save(job, status='failed', inserted=0, message='Aktualisierung fehlgeschlagen. Der vorherige Datenstand wurde wiederhergestellt.')
        (self.state / 'publication.json').unlink()
        sync_dir(self.state)

    def recover(self):
        if (self.state / 'account-repair.json').exists():
            raise Rejected('Kontokorrektur ist noch nicht abgeschlossen. Wartungswerkzeug erneut ausführen.')
        path = self.state / 'publication.json'
        if path.exists():
            journal = json.loads(path.read_text())
            job = self.load(journal['id'])
            if job['status'] == 'completed' and digest(self.live) == journal['new_hash']:
                self.refresh(job['id'])
                path.unlink()
                sync_dir(self.state)
            else:
                self.rollback(journal, job)
        for job in self.jobs():
            if job['status'] not in TERMINAL and job['status'] != 'queued':
                self.save(job, status='failed', inserted=0, message='Verarbeitung durch Neustart unterbrochen. Nichts übernommen; bitte einen neuen Auftrag starten.')
                for suffix in ('', '-wal', '-shm', '-journal'):
                    Path(str(self.state / (job['id'] + '.sqlite3')) + suffix).unlink(missing_ok=True)

    def worker(self):
        with (self.state / 'worker.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.recover()
            while not self.stop.is_set():
                # Unresolved publication blocks all later writes, including after a rollback failure.
                if (self.state / 'publication.json').exists():
                    self.recover()
                queued = [j for j in self.jobs() if j['status'] == 'queued']
                if queued:
                    self.process(queued[0])
                else:
                    self.stop.wait(.5)


def handler(service, token, worker):
    class Handler(BaseHTTPRequestHandler):
        uploads = threading.BoundedSemaphore(2)

        def log_message(self, *args):
            pass

        def send(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            if not self.uploads.acquire(blocking=False):
                self.send({'error': 'Bitte den Upload später erneut versuchen.'}, 503)
                return
            try:
                self.dispatch()
            finally:
                self.uploads.release()

        def dispatch(self):
            self.connection.settimeout(30)
            if self.path == '/health' and self.command == 'GET':
                self.send({'ok': worker.is_alive()}, 200 if worker.is_alive() else 503)
                return
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send({'error': 'Zugriff verweigert.'}, 401)
                return
            try:
                if self.command == 'GET' and self.path == '/v1/jobs':
                    self.send({'jobs': [service.public(j) for j in service.jobs()[::-1][:50]], 'accounts': service.accounts(), 'available': worker.is_alive()})
                elif self.command == 'GET' and self.path.startswith('/v1/jobs/'):
                    self.send({'job': service.public(service.load(self.path[len('/v1/jobs/'):]))})
                elif self.command == 'POST' and self.path == '/v1/jobs':
                    if not worker.is_alive() or (service.state / 'publication.json').exists():
                        self.send({'error': 'Importdienst benötigt eine Prüfung. Es wurde kein Auftrag angenommen.'}, 503)
                        return
                    length = self.headers.get('Content-Length', '')
                    if not length.isdigit() or not 0 < int(length) <= MAX_BODY or self.headers.get('Content-Type') != 'application/json':
                        self.send({'error': 'Ungültiger oder zu großer Upload.'}, 413)
                        return
                    raw = self.rfile.read(int(length))
                    if len(raw) != int(length):
                        raise Rejected('Upload unvollständig.')
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise Rejected('Ungültiger Auftrag.')
                    self.send({'job': service.public(service.accept(payload))}, 202)
                else:
                    self.send({'error': 'Nicht gefunden.'}, 404)
            except (Rejected, ValueError, TypeError) as error:
                message = str(error) if isinstance(error, Rejected) else 'Ungültige Anfrage.'
                self.send({'error': message}, 422)
            except Exception:
                self.send({'error': 'Importdienst vorübergehend nicht verfügbar.'}, 503)
    return Handler


def main():
    os.umask(0o077)
    service = Service(os.getenv('BANK_DATA', '/app/data'), os.getenv('BANK_CONTROL', '/control'),
                      os.getenv('BANK_ACCOUNTS', '/app/data/import-service/accounts.json'))
    token = Path(os.environ['BANK_TOKEN_FILE']).read_text().strip()
    if len(token) < 32:
        raise SystemExit('Import token configuration invalid')
    worker = threading.Thread(target=service.worker, daemon=True)
    worker.start()
    ThreadingHTTPServer(('0.0.0.0', 8080), handler(service, token, worker)).serve_forever()


if __name__ == '__main__':
    main()
