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
# Requires: 관리자 권한(서비스/방화벽/레지스트리)
# ─────────────────────────────────────────────────────────────────────────────

param(
  [string]$PythonPath = "",
  [ValidateSet("auto","delayed-auto","demand","disabled")]
  [string]$StartType = "delayed-auto",
  [switch]$OpenFirewall = $false,
  [switch]$Uninstall = $false
)

$ErrorActionPreference = "Stop"

# ── Helpers
function Write-Info($m){ Write-Host $m -ForegroundColor Cyan }
function Write-Warn($m){ Write-Host $m -ForegroundColor Yellow }
function Write-Err ($m){ Write-Host $m -ForegroundColor Red   }

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $pr = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "이 스크립트는 관리자 권한으로 실행되어야 합니다."
  }
}

# ── Paths
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$ServicePy = Join-Path $PSScriptRoot 'dms_service.py'
$SvcName   = 'DMs'
$Display   = '4DReplay DMs Agent'
$LogDir    = Join-Path $RepoRoot 'logs\DMS'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ── Resolve Python
function Resolve-Python {
  param([string]$Prefer)

  if ($Prefer -and (Test-Path $Prefer)) { return (Resolve-Path $Prefer).Path }

  # py launcher (우선 3.10 선호)
  try {
    $out = & py -0p 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) {
      $line = ($out | Select-String -Pattern "3\.10").Line
      if (-not $line) { $line = $out.Split("`n")[0] }
      $guess = $line -replace '.*\s+', ''
      if (Test-Path $guess) { return (Resolve-Path $guess).Path }
    }
  } catch {}

  # common defaults
  $cands = @(
    'C:\Program Files\Python310\python.exe',
    'C:\Python310\python.exe'
  )
  foreach($p in $cands){
    if(Test-Path $p){ return (Resolve-Path $p).Path }
  }

  throw "Python 3.10 not found. Use -PythonPath to specify."
}

# ── Ensure pywin32 + locate PythonService.exe
function Ensure-Pywin32 {
  param([string]$Py)

  Write-Info "Using Python: $Py"

  # 1) pywin32 설치/업데이트
  & $Py -m pip install --upgrade --no-warn-script-location pywin32 | Out-Host

  # 2) postinstall (파일 배치/등록)
  try {
    & $Py -m pywin32_postinstall -install | Out-Host
  } catch {
    Write-Warn "pywin32_postinstall 실패: $($_.Exception.Message)"
  }

  # 3) site-packages 경로
  $siteRoot = ""
  try {
    $siteRoot = & $Py -c "import site; print(site.getsitepackages()[0])"
    $siteRoot = $siteRoot.Trim()
  } catch {}
  if (-not $siteRoot) {
    try {
      $siteRoot = & $Py -c "import sysconfig; print(sysconfig.get_paths()['platlib'])"
      $siteRoot = $siteRoot.Trim()
    } catch {}
  }
  if (-not $siteRoot -or -not (Test-Path $siteRoot)) {
    throw "site-packages 경로를 찾을 수 없습니다."
  }

  # 4) PythonService.exe 후보들 (대소문자/두 위치 모두 확인)
  $cand = @(
    (Join-Path $siteRoot 'win32\PythonService.exe'),
    (Join-Path $siteRoot 'win32\pythonservice.exe'),
    (Join-Path $siteRoot 'pywin32_system32\PythonService.exe'),
    (Join-Path $siteRoot 'pywin32_system32\pythonservice.exe')
  )

  $found = $cand | Where-Object { Test-Path $_ } | Select-Object -First 1

  if (-not $found) {
    # 보수적 재시도
    & $Py -m pip install --force-reinstall pywin32 | Out-Host
    try { & $Py -m pywin32_postinstall -install | Out-Host } catch {}
    $found = $cand | Where-Object { Test-Path $_ } | Select-Object -First 1
  }

  if (-not $found) {
    throw "PythonService.exe not found under site-packages. Tried: $($cand -join ', ')"
  }

  Write-Info "PythonService resolved: $found"
  return (Resolve-Path $found).Path
}

# ── Service create via SCM + Parameters keys (pywin32 방식)
function Install-Service {
  param(
    [string]$Py,
    [string]$SvcExe
  )

  Write-Info "Installing service via SCM..."

  # 동일 이름 서비스 정리
  sc.exe stop $SvcName 2>$null | Out-Null
  sc.exe delete $SvcName 2>$null | Out-Null
  Start-Sleep -Seconds 1

  $startFlag = switch ($StartType) {
    "auto"          { "auto" }
    "delayed-auto"  { "auto" }      # sc는 auto만 받고, DelayedAutoStart 레지스트리로 지연 설정
    "demand"        { "demand" }
    "disabled"      { "disabled" }
  }

  # 서비스 생성 (binPath는 PythonService.exe)
  $quotedBin = "`"$SvcExe`""
  $createOut = sc.exe create $SvcName binPath= $quotedBin start= $startFlag DisplayName= "$Display"
  if ($LASTEXITCODE -ne 0) { throw "sc create failed: $createOut" }

  # 설명
  sc.exe description $SvcName "Process supervisor + HTTP control for 4DReplay" | Out-Null

  # Parameters (pywin32가 읽는 키)
  $svcRoot = "HKLM:\SYSTEM\CurrentControlSet\Services\$SvcName"
  $base    = Join-Path $svcRoot 'Parameters'
  New-Item -Path $base -Force | Out-Null

  # PythonClassName는 서비스 클래스의 FQCN
  New-ItemProperty -Path $base -Name PythonClassName -Value 'service.DMs.dms_service.DMsService' -PropertyType String -Force | Out-Null

  # AppDirectory: 작업 디렉토리(리포 루트)
  New-ItemProperty -Path $base -Name AppDirectory -Value $RepoRoot -PropertyType String -Force | Out-Null

  # PythonPath: sys.path에 추가할 경로(리포 루트)
  New-ItemProperty -Path $base -Name PythonPath   -Value $RepoRoot -PropertyType String -Force | Out-Null

  # CommandLine: 커맨드라인 인자(필요 시 모듈에서 사용)
  New-ItemProperty -Path $base -Name CommandLine  -Value 'DMs' -PropertyType String -Force | Out-Null

  # Delayed Auto Start 설정
  if ($StartType -eq "delayed-auto") {
    $cur = Get-ItemProperty -Path $svcRoot -Name DelayedAutoStart -ErrorAction SilentlyContinue
    if ($null -eq $cur) {
      New-ItemProperty -Path $svcRoot -Name DelayedAutoStart -PropertyType DWord -Value 1 -Force | Out-Null
    } else {
      Set-ItemProperty -Path $svcRoot -Name DelayedAutoStart -Value 1
    }
  } else {
    # 다른 시작 유형이면 DelayedAutoStart를 0으로(청소)
    try { Set-ItemProperty -Path $svcRoot -Name DelayedAutoStart -Value 0 -ErrorAction SilentlyContinue } catch {}
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
  Write-Info "Removing service '$SvcName'..."
  sc.exe stop $SvcName 2>$null | Out-Null
  sc.exe delete $SvcName 2>$null | Out-Null
  Write-Info "Service '$SvcName' removed (if it existed)."
}

# ── Main
Assert-Admin

if ($Uninstall) {
  Uninstall-Service
  return
}

$Py     = Resolve-Python -Prefer $PythonPath
$SvcExe = Ensure-Pywin32 -Py $Py

Write-Host "RepoRoot   : $RepoRoot"
Write-Host "ServicePy  : $ServicePy"
Write-Host "PythonPath : $Py"
Write-Host "LogDir     : $LogDir"
Write-Host "SvcBinary  : $SvcExe"

Install-Service -Py $Py -SvcExe $SvcExe

Write-Host "`n✅ DMs Service installed and started."
Write-Host "Check:  Get-Service DMs"
Write-Host "Open :  http://localhost:51050/"
