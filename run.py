"""Start the backend: API (keyword + semantic search) and the processing worker.

    .venv\\Scripts\\python.exe run.py                  # gateway LLM (backend/app/gateway_settings.py)
    .venv\\Scripts\\python.exe run.py --provider local # Ollama on this machine

Both run in this one terminal; Ctrl+C stops both. Start the frontend separately:
    npm --prefix frontend run dev
"""
import argparse
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def settings(provider):
    var = ROOT / "var"
    var.mkdir(exist_ok=True)
    # One private signing secret, so logins survive restarts.
    secret = var / ".jwt_secret"
    if not secret.exists():
        secret.write_text(secrets.token_hex(32), encoding="ascii")
    env = dict(os.environ)
    env.update({
        "JWT_SECRET": secret.read_text(encoding="ascii").strip(),
        "DATABASE_URL": env.get("DATABASE_URL", "sqlite:///./var/docintel.db"),
        "STORAGE_DIR": env.get("STORAGE_DIR", "./var/originals"),
        "EXTRACTION_PROVIDER": provider,
        "ALLOW_CLOUD_EXTRACTION": "false",
        # MinerU: local Basic/ONNX models provisioned by scripts/setup_mineru.ps1.
        "MINERU_HOME": str(var / "mineru"),
        "MINERU_TIER": "basic",
        "MINERU_MODEL_SOURCE": "local",
        "MINERU_MODEL_BASE_DIR": str(var / "mineru" / "models"),
        "MINERU_MODEL_SMALL_BACKEND": "onnx",
    })
    # Long PDFs are extracted in many requests: allow up to 2 hours per document.
    # Finished requests are cached, so reprocessing a timed-out document resumes.
    env.setdefault("WORKER_LEASE_SECONDS", "7200")
    env.setdefault("PROCESS_TIMEOUT_SECONDS", "7000")
    api = {**env, "SEMANTIC_ENABLED": "true", "QDRANT_PATH": str(var / "qdrant"),
           "FASTEMBED_CACHE_PATH": str(var / "fastembed")}
    # The worker never opens the embedded index; the API indexes documents itself.
    worker = {**env, "SEMANTIC_ENABLED": "false"}
    return api, worker


def python(venv):
    path = ROOT / venv / "Scripts" / "python.exe"
    return str(path) if path.exists() else sys.executable


def main():
    parser = argparse.ArgumentParser(description="Start API + worker")
    parser.add_argument("--provider", choices=["gateway", "local"], default="gateway")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.provider == "gateway" and not (os.getenv("GATEWAY_URL") or (ROOT / "backend" / "app" / "gateway_settings.py").exists()):
        parser.error("no LLM configured: copy backend/app/gateway_settings.example.py to gateway_settings.py, "
                     "or use --provider local for Ollama")
    api_env, worker_env = settings(args.provider)
    api_python = python(".venv")
    subprocess.run([api_python, "-m", "backend.app.cli", "init-db"], cwd=ROOT, env=api_env, check=True)
    processes = [
        subprocess.Popen([api_python, "-m", "uvicorn", "backend.app.main:create_app", "--factory",
                          "--host", "127.0.0.1", "--port", str(args.port)], cwd=ROOT, env=api_env),
        # The worker uses the MinerU environment when it exists.
        subprocess.Popen([python(".venv-mineru"), "-m", "backend.app.worker"], cwd=ROOT, env=worker_env),
    ]
    print(f"API on http://127.0.0.1:{args.port}  |  worker provider: {args.provider}  |  Ctrl+C to stop")
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(1)
        print("A backend process exited; stopping the other.")
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
