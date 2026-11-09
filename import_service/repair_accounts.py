"""Operator-confirmed account renaming; retains every transaction ID and tx_hash.

Run while importer is stopped. Default is a read-only preview; --apply publishes.
An interrupted operation is rolled back before a subsequent --apply attempt.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
sys.path.insert(0, '/app')
from import_service.iban import compact, valid_german_iban
from import_service.service import Service, Rejected, connect_ro, ClosingConnection, snapshot, digest, durable_json, sync_dir, TERMINAL
import sqlite3


def logical_digest(path, mapping):
    """Compare every stored value, allowing only the explicitly approved account rename."""
    total = hashlib.sha256()
    with connect_ro(path) as db:
        for table in ('imports', 'transactions'):
            cur = db.execute('SELECT * FROM ' + table + ' ORDER BY id')
            columns = [c[0] for c in cur.description]
            for values in cur:
                row = dict(zip(columns, values))
                if table == 'transactions':
                    row['account'] = next((full for old, full in mapping.items() if compact(old) == compact(row['account'])), row['account'])
                total.update(json.dumps([table, row], ensure_ascii=False, sort_keys=True).encode())
    return total.hexdigest()


class AccountRepair:
    def __init__(self, service, mapping):
        self.service, self.mapping = service, mapping
        self.marker = service.state / 'account-repair.json'
        if not mapping or len(set(mapping.values())) != len(mapping):
            raise Rejected('Eindeutige Kontozuordnung erforderlich.')
        for old, full in mapping.items():
            if len(compact(old)) != 20 or not compact(full).startswith(compact(old)) or not valid_german_iban(full):
                raise Rejected('Kontokorrektur benötigt die bisherige 20-stellige Kennung und eine bestätigte vollständige gültige IBAN.')

    def restore(self, journal):
        s = self.service
        folder = s.state / 'repairs' / journal['id']
        if digest(s.live) not in (journal['old_hash'], journal['new_hash'], journal['backup_hash']):
            raise Rejected('Unerwartete externe Änderung. Keine automatische Rücknahme.')
        if digest(folder / 'bank.sqlite3') != journal['backup_hash']:
            raise Rejected('Wartungssicherung beschädigt. Korrektur bleibt gesperrt.')
        work = folder / 'restore.sqlite3'
        shutil.copyfile(folder / 'bank.sqlite3', work)
        with work.open('rb') as stream: os.fsync(stream.fileno())
        os.replace(work, s.live)
        sync_dir(s.root)
        durable_json(s.accounts_file, journal['accounts_before'])
        aliases = s.state / 'account-aliases.json'
        if journal['aliases_before'] is None:
            aliases.unlink(missing_ok=True)
        else:
            durable_json(aliases, journal['aliases_before'])
        s.refresh('account-rollback-' + journal['id'])
        self.marker.unlink()
        sync_dir(s.state)

    def run(self, apply=False):
        s = self.service
        with (s.state / 'worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.marker.exists():
                if not apply: raise Rejected('Unterbrochene Korrektur zuerst mit --apply wiederherstellen.')
                self.restore(json.loads(self.marker.read_text()))
            if (s.state / 'publication.json').exists() or any(j['status'] not in TERMINAL for j in s.jobs()):
                raise Rejected('Zuerst vorhandene Importaufträge und deren Recovery abschließen.')
            if any(Path(str(s.live) + suffix).exists() for suffix in ('-wal','-journal')):
                raise Rejected('Externer Schreiber aktiv. Nichts geändert.')
            accounts = s.accounts()
            with connect_ro(s.live) as db:
                labels = db.execute('SELECT account,COUNT(*) FROM transactions GROUP BY account').fetchall()
            changes = {label: full for label, _ in labels for old, full in self.mapping.items() if compact(label) == compact(old)}
            report = {'changed_transactions': sum(n for label, n in labels if label in changes), 'accounts': len(changes)}
            if not apply: return report
            if not report['changed_transactions']:
                # Idempotent repeat is valid only if configuration and aliases are already consistent.
                if any(a['account'] in self.mapping for a in accounts) or any(s.aliases().get(old) != full for old, full in self.mapping.items()):
                    raise Rejected('Datenbank und Konfiguration widersprechen sich. Bitte Wartungsstand prüfen.')
                return report
            key = uuid.uuid4().hex
            if shutil.disk_usage(s.root).free < s.live.stat().st_size * 3 + 100 * 1024 * 1024:
                raise Rejected('Zu wenig Platz für Sicherung und Arbeitskopie.')
            folder = s.state / 'repairs' / key
            folder.mkdir(parents=True, mode=0o700)
            old_hash = digest(s.live)
            snapshot(s.live, folder / 'bank.sqlite3')
            snapshot(s.live, folder / 'work.sqlite3')
            work = folder / 'work.sqlite3'
            with sqlite3.connect(work, factory=ClosingConnection) as db:
                for old, full in changes.items():
                    db.execute('UPDATE transactions SET account=? WHERE account=?', (full, old))
            with connect_ro(work) as db:
                if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise Rejected('Integritätsprüfung fehlgeschlagen.')
            if logical_digest(s.live, self.mapping) != logical_digest(work, self.mapping) or digest(s.live) != old_hash:
                raise Rejected('Bestandsdaten wurden außerhalb der Kontokorrektur verändert.')
            journal = {'id':key, 'old_hash':old_hash, 'new_hash':digest(work), 'backup_hash':digest(folder / 'bank.sqlite3'),
                       'accounts_before':accounts, 'aliases_before':s.aliases() or None}
            durable_json(self.marker, journal)
            try:
                with work.open('rb') as stream: os.fsync(stream.fileno())
                os.replace(work, s.live)
                sync_dir(s.root)
                configured = [{'type':a['type'], 'account':next((full for old, full in self.mapping.items() if compact(old) == compact(a['account'])), a['account'])} for a in accounts]
                durable_json(s.accounts_file, configured)
                durable_json(s.state / 'account-aliases.json', {**s.aliases(), **self.mapping})
                s.refresh('account-repair-' + key)
                durable_json(folder / 'report.json', {**report, 'logical_digest':logical_digest(s.live,self.mapping), 'mapping':self.mapping})
                self.marker.unlink()
                sync_dir(s.state)
                return report
            except Exception:
                self.restore(journal)
                raise


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--mapping', default='/app/data/import-service/account-repair-mapping.json')
    args = parser.parse_args()
    try:
        service = Service(os.getenv('BANK_DATA','/app/data'),os.getenv('BANK_CONTROL','/control'),
                          os.getenv('BANK_ACCOUNTS','/app/data/import-service/accounts.json'))
        mapping = json.loads(Path(args.mapping).read_text())
        print(json.dumps(AccountRepair(service,mapping).run(args.apply)))
    except BlockingIOError:
        print('Importdienst läuft noch. Für Kontokorrekturen zuerst anhalten; es wurde nichts geändert.', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Kontokorrektur nicht abgeschlossen. Wartungszustand und Sicherung prüfen; bei unterbrochener Korrektur denselben Befehl erneut ausführen.', file=sys.stderr)
        sys.exit(1)
