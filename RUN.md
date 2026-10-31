# How to run

Run every command in PowerShell from the project folder.

## Start the app

**1. Start your LLM proxy on port 20133, then check it answers:**

```powershell
.\.venv\Scripts\python.exe -m scripts.ask_gateway "Reply OK"
```

**2. Terminal 1: backend (API + worker together):**

```powershell
.\.venv\Scripts\python.exe run.py
```

**3. Terminal 2: frontend:**

```powershell
npm --prefix frontend run dev
```

**4. Open http://127.0.0.1:5173 and log in** as `reviewer@example.test`.

Press `Ctrl+C` in a terminal to stop it.

## If the ports are already in use

This stops anything left running on ports 8000 and 5173, plus any leftover worker:

```powershell
Get-NetTCPConnection -LocalPort 8000,5173 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'backend\.app|run\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## First-time setup only

The database is created when the backend starts. If you have no login yet, run this once and choose a password of at least 12 characters:

```powershell
$env:DATABASE_URL = 'sqlite:///./var/docintel.db'
.\.venv\Scripts\python.exe -m backend.app.cli init-db
.\.venv\Scripts\python.exe -m backend.app.cli create-user --email reviewer@example.test --role reviewer
```

## Change the LLM

On a fresh clone, create the settings file first, then add your URL, token and model:

```powershell
copy backend\app\gateway_settings.example.py backend\app\gateway_settings.py
```

| To use | Do this |
|---|---|
| Another gateway URL, token or model | Edit `backend/app/gateway_settings.py` (never commit it) |
| Free local Ollama (slow) | `.\.venv\Scripts\python.exe run.py --provider local` |

If `ollama list` shows no models after a reboot, start the server with the model folder set:

```powershell
$env:OLLAMA_MODELS = 'D:\ProjectXOWN\ollama\models'
& 'D:\ProjectXOWN\ollama\app\ollama.exe' serve
```

## Test files

```powershell
.\.venv\Scripts\python.exe -m scripts.make_sample_invoice
```

Upload `var\samples\northwind-invoice-pack-4-pages.pdf`. The expected values are in `var\samples\northwind-invoice-pack-4-pages.answer-key.json`.

## Evaluation

Keep the proxy running for the whole evaluation.

```powershell
# Finish the documents an outage interrupted; finished work is kept
.\.venv-mineru\Scripts\python.exe -m eval.run_ai --resume

# Full run, using cached answers where available
.\.venv-mineru\Scripts\python.exe -m eval.run_ai
```

The report is written to `eval\reports\ai\report.md`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests eval/test_eval.py -q
npm --prefix frontend test
npm --prefix frontend run build
```
