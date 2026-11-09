"""Console uploads use the same queue and publication protections as Invoice 2."""
import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--type', choices=['ing','paypal'], required=True)
parser.add_argument('--account', required=True)
parser.add_argument('--wait', action='store_true')
parser.add_argument('files', nargs='+')
args = parser.parse_args()
key = uuid.uuid4().hex
token = Path(os.environ['BANK_TOKEN_FILE']).read_text().strip()


def request(path, data=None):
    body = None if data is None else json.dumps(data).encode()
    query = urllib.request.Request('http://127.0.0.1:8080' + path, body,
                                  {'Authorization':'Bearer ' + token, 'Content-Type':'application/json'})
    with urllib.request.urlopen(query, timeout=35) as response:
        return json.load(response)


try:
    if not 1 <= len(args.files) <= 20:
        raise ValueError('Zwischen einer und 20 Dateien auswählen.')
    files, total = [], 0
    for filename in args.files:
        source = Path(filename)
        size = source.stat().st_size
        total += size
        if not 0 < size <= 5 * 1024 * 1024 or total > 20 * 1024 * 1024:
            raise ValueError('Maximal 5 MiB pro Datei und 20 MiB insgesamt.')
        files.append({'name':source.name, 'data':base64.b64encode(source.read_bytes()).decode()})
    job = request('/v1/jobs', {'id':key, 'type':args.type, 'account':args.account, 'files':files})['job']
    print('Auftrag:', key)
    previous = None
    while True:
        if job['message'] != previous:
            print(job['message'], flush=True)
            previous = job['message']
        if not args.wait or job['status'] in ('completed','failed','review'):
            break
        time.sleep(1)
        job = request('/v1/jobs/' + key)['job']
    sys.exit(0 if job['status'] not in ('failed','review') else 1)
except urllib.error.HTTPError as error:
    try:
        message = json.load(error)['error']
    except Exception:
        message = 'Anfrage abgewiesen.'
    print(message, file=sys.stderr)
    sys.exit(1)
except ValueError as error:
    print(str(error), file=sys.stderr)
    sys.exit(1)
except Exception:
    print('Verbindung oder Datei nicht lesbar. Vor Wiederholung Auftrag ' + key + ' in der Importhistorie prüfen.', file=sys.stderr)
    sys.exit(1)
