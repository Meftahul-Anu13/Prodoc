# Run interactively in your own terminal; never paste provider secrets into chat.
param([string]$Python = '', [switch]$Once)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $parserPython = Join-Path $root '.venv-mineru/Scripts/python.exe'
    if (Test-Path $parserPython) { $Python = $parserPython }
    else { $Python = Join-Path $root '.venv/Scripts/python.exe' }
}
if (-not (Test-Path $Python)) { throw 'Create the application/worker virtual environment first; see README.md.' }
Write-Host 'Stop the existing worker first. This worker processes ALL queued formats, including spreadsheets.'
Write-Host 'Source text, cell contents and sheet labels go to Anthropic for layout observation and extraction.'
Write-Host 'Default model: claude-opus-5-5 unless LLM_MODEL is set. Provider-default refusal fallbacks are enabled outside compatibility mode.'
Write-Host 'There is no account-wide spending cap. Reprocessing can bill again. Review retention/region/document rights first.'
if ($env:ANTHROPIC_BASE_URL) { Write-Host 'A custom ANTHROPIC_BASE_URL is set. Verify its destination before consenting.' }
$consent = Read-Host 'Type YES to authorize cloud processing and billing for queued documents'
if ($consent -cne 'YES') { throw 'Cloud processing not enabled.' }
$names = @('EXTRACTION_PROVIDER', 'ALLOW_CLOUD_EXTRACTION', 'ANTHROPIC_API_KEY', 'LLM_MODEL', 'OBSERVATION_MODEL', 'EXTRACTION_MODEL', 'MINERU_HOME', 'MINERU_TIER', 'MINERU_MODEL_SOURCE', 'MINERU_MODEL_BASE_DIR', 'MINERU_MODEL_SMALL_BACKEND', 'ANTHROPIC_COMPAT_MODE')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$pointer = [IntPtr]::Zero
$secureKey = $null
Push-Location $root
try {
    # Use the project's provisioned local CPU profile unless explicitly overridden.
    if (-not $env:MINERU_HOME) { $env:MINERU_HOME = Join-Path $root 'var/mineru' }
    if (-not $env:MINERU_TIER) { $env:MINERU_TIER = 'basic' }
    if (-not $env:MINERU_MODEL_SOURCE) { $env:MINERU_MODEL_SOURCE = 'local' }
    if (-not $env:MINERU_MODEL_BASE_DIR) { $env:MINERU_MODEL_BASE_DIR = Join-Path $root 'var/mineru/models' }
    if (-not $env:MINERU_MODEL_SMALL_BACKEND) { $env:MINERU_MODEL_SMALL_BACKEND = 'onnx' }
    if ($env:SEMANTIC_ENABLED -eq 'true' -and $Python -like '*venv-mineru*') {
        throw 'The isolated MinerU worker does not include FastEmbed. Set SEMANTIC_ENABLED=false for this profile.'
    }
    if (-not $env:ANTHROPIC_API_KEY -and -not $env:ANTHROPIC_AUTH_TOKEN) {
        $secureKey = Read-Host 'Anthropic API key (hidden; not written to disk)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $env:ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $pointer = [IntPtr]::Zero
    }
    $env:EXTRACTION_PROVIDER = 'anthropic'
    $env:ALLOW_CLOUD_EXTRACTION = 'true'
    # Respect an explicitly chosen model (e.g. a gateway's model name).
    if (-not $env:LLM_MODEL) { $env:LLM_MODEL = 'claude-opus-5-5' }
    if (-not $env:OBSERVATION_MODEL) { $env:OBSERVATION_MODEL = $env:LLM_MODEL }
    if (-not $env:EXTRACTION_MODEL) { $env:EXTRACTION_MODEL = $env:LLM_MODEL }
    Write-Host "Requested model: $($env:LLM_MODEL)$(if ($env:ANTHROPIC_COMPAT_MODE -eq 'true') { ' (gateway compatibility mode)' })"

    if ($Once) { & $Python -m backend.app.worker --once }
    else { & $Python -m backend.app.worker }
    if ($LASTEXITCODE -ne 0) { throw 'Worker exited unsuccessfully.' }
} finally {
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($secureKey) { $secureKey.Dispose() }
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    Pop-Location
}
