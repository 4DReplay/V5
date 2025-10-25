# ─────────────────────────────────────────────────────────────────────────────
# install_dms.ps1 (final, PowerShell-only)
# date: 2025-10-25
# owner: 4DReplay
# Usage:
#   powershell -ExecutionPolicy Bypass -File service\DMs\install_dms.ps1 -OpenFirewall
#   powershell -ExecutionPolicy Bypass -File service\DMs\install_dms.ps1 -Uninstall
# Options:
#   -PythonPath "C:\Program Files\Python310\python.exe"
#   -StartType auto|delayed-auto|demand|disabled  (default: delayed-auto)
#   -OpenFirewall  (TCP 51050 허용)
#   -Uninstall     (서비스 제거)
# ─────────────────────────────────────────────────────────────────────────────

param(
  [string]$PythonPath = "",
  [ValidateSet("auto","delayed-auto","demand","disabled")]
  [string]$StartType = "delayed-auto",
  [switch]$OpenFirewall = $false,
  [switch]$Uninstall = $false
)

$ErrorActionPreference = "Stop"

# ── Paths
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$ServicePy = Join-Path $PSScriptRoot 'dms_service.py'
$SvcName   = 'DMs'
$Display   = '4DReplay DMs Agent'
$LogDir    = Join-Path $RepoRoot 'logs\DMS'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Info($m){ Write-Host $m -ForegroundColor Cyan }
function Write-Warn($m){ Write-Host $m -ForegroundColor Yellow }
function Write-Err ($m){ Write-Host $m -ForegroundColor Red   }

# ── Resolve Python
function Resolve-Python {
  param([string]$Prefer)

  if ($Prefer -and (Test-Path $Prefer)) { return $Prefer }

  # py launcher (우선 3.10 선호)
  try {
    $out = & py -0p 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) {
      $line = ($out | Select-String -Pattern "3\.10").Line
      if (-not $line) { $line = $out.Split("`n")[0] }
      $guess = $line -replace '.*\s+', ''
      if (Test-Path $guess) { return $guess }
    }
  } catch {}

  # common defaults
  $cands = @(
    'C:\Program Files\Python310\python.exe',
    'C:\Python310\python.exe'
  )
  foreach($p in $cands){ if(Test-Path $p){ return $p } }

  throw "Python 3.10 not found. Use -PythonPath to specify."
}

# ── Ensure pywin32 + pythonservice.exe
function Ensure-Pywin32 {
  param([string]$Py)

  Write-Info "Using Python: $Py"
  & $Py -m pip install --upgrade --no-warn-script-location pywin32 | Out-Host

  # Bash here-doc 제거: PowerShell에서 -c로 site 패키지 경로 획득
  $sysSite = & $Py -c 'import site; print(site.getsitepackages()[0])'
  $sysSite = $sysSite.Trim()

  $svcExe = Join-Path (Split-Path $Py) 'pythonservice.exe'
  $cand1  = Join-Path $sysSite 'win32\pythonservice.exe'
  $cand2  = Join-Path $sysSite 'pywin32_system32\pythonservice.exe'

  if (!(Test-Path $svcExe)) {
    if     (Test-Path $cand1) { Copy-Item $cand1 $svcExe -Force }
    elseif (Test-Path $cand2) { Copy-Item $cand2 $svcExe -Force }
    else  { throw "pythonservice.exe not found under $sysSite" }
  }
  return $svcExe
}

# ── Service create via SCM + Parameters keys (pywin32 방식)
function Install-Service {
  param([string]$Py, [string]$SvcExe)

  Write-Info "Installing service via SCM..."
  sc.exe stop $SvcName 2>$null | Out-Null
  sc.exe delete $SvcName 2>$null | Out-Null
  Start-Sleep 1

  $startFlag = switch ($StartType) {
    "auto"          { "auto" }
    "delayed-auto"  { "delayed-auto" }
    "demand"        { "demand" }
    "disabled"      { "disabled" }
  }

  # 서비스 생성
  $create = sc.exe create $SvcName binPath= "`"$SvcExe`"" start= $startFlag DisplayName= "$Display"
  if ($LASTEXITCODE -ne 0) { throw "sc create failed: $create" }

  # 설명
  sc.exe description $SvcName "Process supervisor + HTTP control for 4DReplay" | Out-Null

  # Parameters (pywin32 서비스 엔트리)
  $svcRoot = "HKLM:\SYSTEM\CurrentControlSet\Services\$SvcName"
  $base    = Join-Path $svcRoot 'Parameters'
  New-Item -Path $base -Force | Out-Null
  New-ItemProperty -Path $base -Name PythonClassName -Value 'service.DMs.dms_service.DMsService' -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $base -Name AppDirectory    -Value $RepoRoot -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $base -Name PythonPath      -Value $RepoRoot -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $base -Name CommandLine     -Value 'DMs'      -PropertyType String -Force | Out-Null

  # Delayed Auto Start 플래그 (레지스트리 안전 처리)
  if ($StartType -eq "delayed-auto") {
    $cur = Get-ItemProperty -Path $svcRoot -Name DelayedAutoStart -ErrorAction SilentlyContinue
    if ($null -eq $cur) {
      New-ItemProperty -Path $svcRoot -Name DelayedAutoStart -PropertyType DWord -Value 1 -Force | Out-Null
    } else {
      Set-ItemProperty -Path $svcRoot -Name DelayedAutoStart -Value 1
    }
  }

  # 방화벽 규칙
  if ($OpenFirewall) {
    $rule = "DMs-51050"
    if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
      New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort 51050 | Out-Null
      Write-Info "Firewall rule '$rule' added."
    } else {
      Write-Info "Firewall rule '$rule' already exists."
    }
  }

  # 서비스 시작
  try {
    Start-Service $SvcName
  } catch {
    Write-Warn "Start-Service failed: $($_.Exception.Message). Trying via 'sc start'."
    sc.exe start $SvcName | Out-Null
  }
}

# ── Uninstall
function Uninstall-Service {
  sc.exe stop $SvcName 2>$null | Out-Null
  sc.exe delete $SvcName 2>$null | Out-Null
  Write-Info "Service '$SvcName' removed (if it existed)."
}

# ── Main
if ($Uninstall) {
  Uninstall-Service
  return
}

$Py = Resolve-Python -Prefer $PythonPath
$SvcExe = Ensure-Pywin32 -Py $Py

Write-Host "RepoRoot   : $RepoRoot"
Write-Host "ServicePy  : $ServicePy"
Write-Host "PythonPath : $Py"
Write-Host "LogDir     : $LogDir"

Install-Service -Py $Py -SvcExe $SvcExe

Write-Host "`n✅ DMs Service installed and started."
Write-Host "Check:  Get-Service DMs"
Write-Host "Open :  http://localhost:51050/"
