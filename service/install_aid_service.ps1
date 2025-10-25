<# 
install_aid_service.ps1
- Install/Update AId Agent as a Windows service (NSSM, NO virtualenv).
- Uses system Python only.
- Run as Administrator.

Example:
  powershell -ExecutionPolicy Bypass -File .\install_aid_service.ps1 -Token "REPLACE_ME"

Options:
  -AIdRoot "C:\4DReplay\AId"
  -ServiceName "AIdAgent"
  -PythonPath "C:\Program Files\Python310\python.exe"
  -BindHost "0.0.0.0"
  -Port 5086
  -Token "YOUR_TOKEN"         # required
  -OpenFirewall               # optional, add inbound firewall rule
  -Uninstall                  # optional, remove service and exit
  -NssmPath "C:\Tools\nssm\nssm.exe"  # optional, absolute path to NSSM (recommended)
#>

[CmdletBinding()]
param(
  [string]$AIdRoot     = "C:\4DReplay\AId",
  [string]$ServiceName = "AIdAgent",
  [string]$PythonPath  = "C:\Program Files\Python310\python.exe",
  [string]$BindHost    = "0.0.0.0",
  [string]$ServiceUser = "FDReplay12",
  [string]$ServicePassword = "Cipet0217",
  [int]   $Port        = 5086,
  [Parameter(Mandatory = $true)]
  [string]$Token        = "AID_TOKEN",
  [switch]$OpenFirewall = $false,
  [switch]$Uninstall    = $false,
  [string]$NssmPath     = "C:\4DReplay\AId\library\nssm\nssm.exe"   # if empty, uses 'nssm' from PATH  
)

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p  = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Run this script as Administrator."
  }
}

function Exec($file, $ArgumentList = $null, [switch]$IgnoreError = $false) {
  if ([string]::IsNullOrWhiteSpace($file)) { throw "Exec: file is empty." }

  $tmpOut = [System.IO.Path]::GetTempFileName()
  $tmpErr = [System.IO.Path]::GetTempFileName()

  try {
    if ([string]::IsNullOrWhiteSpace($ArgumentList)) {
      Write-Host ">> $file" -ForegroundColor DarkGray
      $p = Start-Process -FilePath $file -Wait -PassThru -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
    } else {
      Write-Host ">> $file $ArgumentList" -ForegroundColor DarkGray
      $p = Start-Process -FilePath $file -ArgumentList $ArgumentList -Wait -PassThru -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
    }

    $out = Get-Content -Raw -ErrorAction SilentlyContinue $tmpOut
    $err = Get-Content -Raw -ErrorAction SilentlyContinue $tmpErr
    if ($out) { Write-Host $out }
    if ($err) { Write-Host $err -ForegroundColor Red }

    if (-not $IgnoreError -and ($null -eq $p -or $p.ExitCode -ne 0)) {
      $ec = if ($null -ne $p) { $p.ExitCode } else { "<no process>" }
      throw "Command failed: $file $ArgumentList (ExitCode=$ec)"
    }
  } finally {
    Remove-Item $tmpOut,$tmpErr -ErrorAction SilentlyContinue
  }
}

function Ensure-Dir($path) {
  if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
}

function Has-Command($name) {
  $c = Get-Command $name -ErrorAction SilentlyContinue
  return $null -ne $c
}

# IMPORTANT: avoid using automatic variable $args
function ExecNSSM([string]$NssmArgs) {
  if ([string]::IsNullOrWhiteSpace($NssmArgs)) {
    throw "ExecNSSM called without arguments. (Would open NSSM help and exit 1)"
  }
  $cmd = if ([string]::IsNullOrWhiteSpace($NssmPath)) { "nssm" } else { $NssmPath }
  if ($cmd -ne "nssm" -and -not (Test-Path $cmd)) { throw "NSSM not found: $cmd" }
  Write-Host ">> $cmd $NssmArgs" -ForegroundColor DarkGray
  Exec $cmd $NssmArgs
}

function Remove-ServiceIfExists($name) {
  $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
  if (-not $svc) {
    Write-Host "No existing service named '$name'." -ForegroundColor DarkGray
    return
  }

  Write-Host "Existing service '$name' found. Stopping and removing..." -ForegroundColor Yellow

  try { sc.exe stop $name 1>$null 2>$null | Out-Null } catch {}

  try {
    $q = sc.exe queryex $name
    if ($LASTEXITCODE -eq 0) {
      $pidLine = ($q | Select-String -Pattern "PID\s*:\s*(\d+)" | Select-Object -First 1)
      if ($pidLine) {
        $pid = [int]($pidLine.Matches[0].Groups[1].Value)
        if ($pid -gt 0) {
          Write-Host "Killing PID $pid ..." -ForegroundColor DarkYellow
          try { taskkill /PID $pid /F /T 1>$null 2>$null | Out-Null } catch {}
        }
      }
    }
  } catch {}
  Start-Sleep 1

  $removed = $false
  try { ExecNSSM "remove $name confirm"; $removed = $true } catch {}
  if (-not $removed) {
    try { sc.exe delete $name 1>$null 2>$null | Out-Null } catch {}
  }

  $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Service -Name $name -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
    Start-Sleep 0.5
  }

  if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
    throw "Failed to remove existing service '$name'. Close services.msc; if marked for deletion, reboot is required."
  }
  Write-Host "Removed existing service '$name'." -ForegroundColor Green
}

# =========================
# Paths (service 폴더 기준)
# =========================
$AidDir      = Join-Path $AIdRoot "src"
$ServiceDir  = Join-Path $AIdRoot "service"
$LogsDir     = Join-Path $AIdRoot "logs"
$BatchPath   = Join-Path $ServiceDir "run_aid_agent.cmd"
$StdoutLog   = Join-Path $LogsDir "aid_agent.service.out.log"
$StderrLog   = Join-Path $LogsDir "aid_agent.service.err.log"
$HealthUrl   = "http://127.0.0.1:$Port/health"

# =========================
# Ensure config files exist
# =========================
$CfgDir     = Join-Path $AidDir  "config"
$CfgPrivate = Join-Path $CfgDir  "aid_config_private.json5"
$CfgPublic  = Join-Path $CfgDir  "aid_config_public.json5"

Ensure-Dir $CfgDir
if (-not (Test-Path $CfgPrivate)) { '{}' | Out-File -FilePath $CfgPrivate -Encoding ascii -Force }
if (-not (Test-Path $CfgPublic )) { '{}' | Out-File -FilePath $CfgPublic  -Encoding ascii -Force }

# =========================
# Uninstall mode
# =========================
if ($Uninstall) {
  Assert-Admin
  Remove-ServiceIfExists -name $ServiceName
  Write-Host "Service removed. Leaving files in place: $AIdRoot" -ForegroundColor Green
  exit 0
}

# =========================
# Pre-checks
# =========================
Assert-Admin
if (-not (Test-Path $PythonPath)) { throw "Python not found: $PythonPath" }
if ([string]::IsNullOrWhiteSpace($NssmPath) -and -not (Has-Command "nssm")) {
  throw "nssm command not found. Install NSSM or pass -NssmPath. https://nssm.cc/"
}

# =========================
# Prepare directories
# =========================
Ensure-Dir $AIdRoot
Ensure-Dir $AidDir
Ensure-Dir $ServiceDir
Ensure-Dir $LogsDir

# =========================
# Install required packages (global)
# After LogOn to User -> it doesn't need
# =========================
# Exec "cmd" "/c set PYTHONNOUSERSITE=1 && `"$PythonPath`" -m pip install --upgrade pip --disable-pip-version-check"
# Exec "cmd" "/c set PYTHONNOUSERSITE=1 && `"$PythonPath`" -m pip install --upgrade --force-reinstall --no-warn-script-location --disable-pip-version-check waitress flask requests json5 typing_extensions packaging pyyaml ultralytics GPUtil nvidia-ml-py3 loguru"
# Exec "cmd" "/c set PYTHONNOUSERSITE=1 && `"$PythonPath`" -m pip install --upgrade --index-url https://download.pytorch.org/whl/cu124 torch==2.5.1+cu124 torchvision==0.20.1+cu124 torchaudio==2.5.1+cu124 --disable-pip-version-check"

# =========================
# Write run_aid_agent.cmd  (service 경로 기준)
# =========================
$batch = @"
@echo off
setlocal
cd /d "$AIdRoot"
set "PYTHONUNBUFFERED=1"
rem Include root/service in PYTHONPATH
set "PYTHONPATH=$AIdRoot;$AidDir;$ServiceDir"
mkdir "$LogsDir" 2>nul

rem Start Flask (dev). For prod, consider waitress + wsgi.py.
"$PythonPath" "$ServiceDir\aid_agent.py" ^
  --host $BindHost --port $Port --token "$Token" ^
  --aid-cmd "$PythonPath $AidDir\aid_main.py" ^
  --workdir "$AIdRoot" ^
  --log-dir "$LogsDir"

endlocal
"@
$batch | Out-File -FilePath $BatchPath -Encoding ascii -Force

# =========================
# Recreate service
# =========================
Remove-ServiceIfExists -name $ServiceName

# =========================
# Install service (cmd.exe /c "run_aid_agent.cmd")  ← 따옴표 필수
# =========================
ExecNSSM "install $ServiceName C:\Windows\System32\cmd.exe"
ExecNSSM "set $ServiceName AppParameters /c `"$BatchPath`""
ExecNSSM "set $ServiceName AppDirectory $AIdRoot"
ExecNSSM "set $ServiceName AppStdout $StdoutLog"
ExecNSSM "set $ServiceName AppStderr $StderrLog"
ExecNSSM "set $ServiceName AppStdoutCreationDisposition 2"
ExecNSSM "set $ServiceName AppStderrCreationDisposition 2"
ExecNSSM "set $ServiceName AppKillProcessTree 1"
ExecNSSM "set $ServiceName AppExit 0 Restart"
ExecNSSM "set $ServiceName AppThrottle 1500"
ExecNSSM "set $ServiceName Start SERVICE_AUTO_START"
ExecNSSM "set $ServiceName Type SERVICE_WIN32_OWN_PROCESS"
ExecNSSM "set $ServiceName Description AId Daemon Control Agent for 4DReplay"
ExecNSSM "set $ServiceName ObjectName .\$ServiceUser $ServicePassword"
# 서비스 폴더 권한(쓰기) 부여
Exec "icacls" "`"$AIdRoot`" /grant `"$ServiceUser`":(OI)(CI)M /T" -IgnoreError
Exec "sc.exe" "description $ServiceName `"AId Daemon Control Agent`"" -IgnoreError

# =========================
# Start service
# =========================
ExecNSSM "start $ServiceName"
Start-Sleep -Seconds 2

# =========================
# Optional firewall
# =========================
if ($OpenFirewall) {
  try {
    Exec "netsh" "advfirewall firewall add rule name=`"$ServiceName $Port`" dir=in action=allow protocol=TCP localport=$Port"
  } catch {
    Write-Warning "Firewall rule add failed: $_"
  }
}

# =========================
# Health check
# =========================
try {
  $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri $HealthUrl
  Write-Host ""
  Write-Host ("Health: {0} {1}" -f $resp.StatusCode, $resp.Content) -ForegroundColor Green
} catch {
  Write-Warning "Health check failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "=== SUMMARY ===" -ForegroundColor Cyan
Write-Host "Service     : $ServiceName"
Write-Host "Root        : $AIdRoot"
Write-Host "Batch       : $BatchPath"
Write-Host "Python      : $PythonPath (system)"
Write-Host ("Host:Port   : {0}:{1}" -f $BindHost, $Port)
Write-Host "Logs        : $StdoutLog / $StderrLog"
Write-Host "Health URL  : $HealthUrl"
Write-Host ("Status      : {0}" -f ( & { try { if([string]::IsNullOrWhiteSpace($NssmPath)){ nssm status $ServiceName } else { & $NssmPath status $ServiceName } } catch { "UNKNOWN" } } )) -ForegroundColor Yellow
