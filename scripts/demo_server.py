"""Run browser tests against an isolated synthetic demo. No plaintext credentials on disk."""
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    directory = root / 'var' / ('demo-' + uuid4().hex)
    directory.mkdir(parents=True)
    password = secrets.token_urlsafe(24)
    env = {**os.environ, 'JWT_SECRET': secrets.token_hex(32), 'DATABASE_URL': 'sqlite:///' + str(directory / 'demo.db'),
           'STORAGE_DIR': str(directory / 'originals'), 'BOOTSTRAP_PASSWORD': password, 'DEMO_PASSWORD': password,
           'DEMO_EMAIL': 'reviewer@example.test', 'SEMANTIC_ENABLED': 'false', 'EXTRACTION_PROVIDER': 'baseline'}
    subprocess.run([sys.executable, '-m', 'backend.app.cli', 'create-user', '--email', env['DEMO_EMAIL'], '--role', 'reviewer'], env=env, cwd=root, check=True)
    api = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'backend.app.main:create_app', '--factory', '--host', '127.0.0.1', '--port', '8000'], env=env, cwd=root)
    worker = subprocess.Popen([sys.executable, '-m', 'backend.app.worker'], env=env, cwd=root)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if api.poll() is not None:
                raise RuntimeError('Demo API failed to start (port already used?)')
            try:
                if httpx.get('http://127.0.0.1:8000/healthz', timeout=1).status_code == 200:
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError('Demo API did not become ready')
        # Credentials stay in child environment memory; browser traces are disabled.
        executable = 'npm.cmd' if os.name == 'nt' else 'npm'
        result = subprocess.run([executable, 'exec', '--', 'playwright', 'test'], env=env, cwd=root / 'frontend')
        if result.returncode:
            raise SystemExit(result.returncode)
    finally:
        api.terminate()
        worker.terminate()
        api.wait(timeout=10)
        worker.wait(timeout=10)


if __name__ == '__main__':
    main()
