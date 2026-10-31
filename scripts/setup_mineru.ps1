# Public dependency/model provisioning only. Never sends documents or calls an LLM.
param([ValidateSet('huggingface', 'modelscope')][string]$Source = 'huggingface')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv-mineru/Scripts/python.exe'
$names = @('MINERU_HOME', 'MINERU_MODEL_BASE_DIR', 'MINERU_MODEL_SOURCE', 'MINERU_MODEL_SMALL_BACKEND', 'HF_HOME', 'PIP_CACHE_DIR', 'ALLOW_CLOUD_EXTRACTION')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
Push-Location $root
try {
    $env:MINERU_HOME = Join-Path $root 'var/mineru'
    $env:MINERU_MODEL_BASE_DIR = Join-Path $root 'var/mineru/models'
    $env:MINERU_MODEL_SOURCE = 'local'
    $env:MINERU_MODEL_SMALL_BACKEND = 'onnx'
    $env:HF_HOME = Join-Path $root 'var/mineru/hf-cache'
    $env:PIP_CACHE_DIR = Join-Path $root 'var/pip-cache'
    $env:ALLOW_CLOUD_EXTRACTION = 'false'
    if (-not (Test-Path $python)) {
        & py -3.13 -m venv .venv-mineru
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python 3.13 MinerU environment.' }
    }
    & $python -m pip install -r requirements-mineru.txt
    if ($LASTEXITCODE -ne 0) { throw 'MinerU dependency installation failed.' }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'MinerU dependency consistency check failed.' }
    $kit = Join-Path $root '.venv-mineru/Scripts/mineru-kit.exe'
    & $kit models download --tier basic --small-backend onnx --source $Source
    if ($LASTEXITCODE -ne 0) { throw 'Public model download failed; rerun this script to resume provisioning.' }
    & $kit models verify --tier basic --small-backend onnx
    if ($LASTEXITCODE -ne 0) { throw 'Local MinerU models are incomplete.' }
    Write-Host 'Basic/ONNX models are provisioned. The worker launcher selects this local profile automatically.'
    Write-Host 'No documents or LLM requests were sent. Run scripts/start_pipeline_worker.ps1 only after reviewing its cloud consent notice.'
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    Pop-Location
}
