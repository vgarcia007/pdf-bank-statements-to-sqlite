"""Reload Datasette inside its own container; no Docker API permissions."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request

from service import durable_json

control = Path(os.getenv('BANK_CONTROL', '/control'))
child = None
stopping = False


def stop(*args):
    global stopping
    stopping = True
    if child and child.poll() is None:
        child.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
generation = None
ready = False
while not stopping:
    try:
        requested = json.loads((control / 'requested.json').read_text())['generation']
    except (FileNotFoundError, ValueError, KeyError):
        requested = 'initial'
    if requested != generation or child is None or child.poll() is not None:
        (control / 'ready.json').unlink(missing_ok=True)
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        child = subprocess.Popen(['datasette', 'serve', '--immutable', '/data/bank.sqlite3', '--host', '0.0.0.0',
                                  '--port', '8000', '--setting', 'sql_time_limit_ms', '3000', '--setting',
                                  'allow_download', 'on', '--metadata', '/data/metadata.yaml'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        generation, ready = requested, False
    if not ready and child.poll() is None:
        try:
            # Verify the open Datasette connection sees the current database, not just an HTTP listener.
            with sqlite3.connect('file:/data/bank.sqlite3?mode=ro', uri=True) as db:
                expected = db.execute('SELECT COUNT(*),COALESCE(MAX(id),0) FROM transactions').fetchone()
            url = 'http://127.0.0.1:8000/bank.json?sql=SELECT+COUNT(*)+AS+n,COALESCE(MAX(id),0)+AS+m+FROM+transactions&_shape=objects'
            with urllib.request.urlopen(url, timeout=2) as response:
                rows = json.load(response)['rows']
            if len(rows) == 1 and (rows[0]['n'], rows[0]['m']) == expected:
                durable_json(control / 'ready.json', {'generation': generation})
                ready = True
        except Exception:
            pass
    time.sleep(.25)
if child:
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
