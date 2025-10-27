# -----------------------------------------------------------------------------
# install_dms.ps1  (portable, PowerShell-only, ASCII-safe)
# date : 2025-10-27
# owner: 4DReplay
#
# 목적:
#   - 어떤 PC에서든 동일 절차로 DMs 윈도우 서비스를 설치/재설치/삭제
#   - 기존 서비스가 있으면: STOP -> DELETE -> (재)설치
#   - pywin32 설치/검증, 레지스트리(Parameters) 보강, 방화벽 오픈, 지연자동시작
#   - sc(alias) 충돌 방지: 항상 sc.exe 사용
#
# 사용법(관리자 PowerShell):
#   cd C:\4DReplay\V5
#   powershell -ExecutionPolicy Bypass -File service\DMs\install_dms.ps1 -OpenFirewall
#   powershell -ExecutionPolicy Bypass -File service\DMs\install_dms.ps1 -Uninstall
#
# 옵션:
#   -PythonPath "C:\Program Files\Python310\python.exe"
#   -StartType auto|delayed-auto|demand|disabled   (default: delayed-auto)
#   -OpenFirewall                                   (TCP 51050 허용)
#   -Uninstall                                      (서비스 제거만)
#
# 요구:
#   - Windows 관리자 권한
#   - Python 3.10(x64) 권장
# -----------------------------------------------------------------------------

param(
  [string]$PythonPath = "",
  [ValidateSet("auto","delayed-auto","demand","disabled")]
  [string]$StartType = "delayed-auto",
  [switch]$OpenFirewall = $false,
  [switch]$Uninstall = $false
)

$ErrorActionPreference = "Stop"

# --- Helpers -----------------------------------------------------------------
function Write-Info($m){ Write-Host $m -ForegroundColor Cyan }
function Write-Warn($m){ Write-Host $m -ForegroundColor Yellow }
function Write-Err ($m){ Write-Host $m -ForegroundColor Red }

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $pr = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script as Administrator."
  }
}

# Always real sc.exe (avoid 'sc' alias = Set-Content)
$SC = Join-Path $env:SystemRoot "System32\sc.exe"
if (-not (Test-Path $SC)) { throw "sc.exe not found: $SC" }

function Sc-Line {
  param([string]$ArgsLine)
  $p = Start-Process -FilePath $SC -ArgumentList $ArgsLine -NoNewWindow -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "sc.exe $ArgsLine failed (ExitCode=$($p.ExitCode))" }
}

# 64-bit registry writers (강제 64비트 view)
$RegHKLM64 = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
  [Microsoft.Win32.RegistryHive]::LocalMachine,
  [Microsoft.Win32.RegistryView]::Registry64
)


# --- PATH helpers (system + per-service) -------------------------------------
function Ensure-SystemPathIncludes {
  param([string[]]$Entries)

  # 현재 시스템 PATH
  $cur = [Environment]::GetEnvironmentVariable('Path','Machine')
  $parts = ($cur -split ';') | Where-Object { $_ } | ForEach-Object { $_.Trim() }

  $add = @()
  foreach ($e in $Entries) {
    if (-not (Test-Path $e)) { continue }
    $abs = (Resolve-Path $e).Path
    if (-not ($parts | Where-Object { $_.ToLower() -eq $abs.ToLower() })) {
      $add += $abs
    }
  }
  if ($add.Count -gt 0) {
    $newPath = ($parts + $add) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'Machine')

    # 환경변수 변경 브로드캐스트
    Add-Type -Namespace Win32 -Name NativeMethods -MemberDefinition @"
using System;
using System.Runtime.InteropServices;
public static class NativeMethods {
  [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)]
  public static extern IntPtr SendMessageTimeout(IntPtr hWnd, int Msg, IntPtr wParam, string lParam, int flags, int timeout, out IntPtr lpdwResult);
}
"@ -ErrorAction SilentlyContinue
    $HWND_BROADCAST = [IntPtr]0xffff
    $WM_SETTINGCHANGE = 0x1A
    $null = [Win32.NativeMethods]::SendMessageTimeout($HWND_BROADCAST, $WM_SETTINGCHANGE, [IntPtr]::Zero, "Environment", 2, 5000, [ref]([IntPtr]::Zero))
  }
}

function Set-ServiceEnvironmentPath {
  param(
    [string]$ServiceName,
    [string[]]$ExtraEntries
  )

  # 서비스 키 보장
  $svcKey = $RegHKLM64.CreateSubKey("SYSTEM\CurrentControlSet\Services\$ServiceName")

  # 시스템 PATH + 추가 경로 결합
  $sysPath = [Environment]::GetEnvironmentVariable('Path','Machine')
  $all = @()
  if ($sysPath) { $all += ($sysPath -split ';') }

  foreach ($e in $ExtraEntries) {
    if (Test-Path $e) { $all += (Resolve-Path $e).Path }
  }

  # 중복 제거(대소문자 무시)
  $set = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
  $ordered = New-Object System.Collections.Generic.List[string]
  foreach ($p in $all) { if ($p -and $set.Add($p)) { [void]$ordered.Add($p) } }

  $mergedPath = ($ordered -join ';')

  # 기존 Environment(REG_MULTI_SZ) 읽어 PATH= 항목만 교체
  $existing = $svcKey.GetValue("Environment", $null, [Microsoft.Win32.RegistryValueOptions]::None)
  $list = @()
  if ($existing -is [string[]]) {
    # PATH= 로 시작하는 항목 제거
    $list = @($existing | Where-Object { $_ -and ($_ -notmatch '^(?i)PATH=') })
  }

  # 새 PATH= 추가 (string[] 로 캐스팅 중요)
  $newEnv = @($list + @("PATH=$mergedPath"))
  $svcKey.SetValue(
    "Environment",
    [string[]]$newEnv,
    [Microsoft.Win32.RegistryValueKind]::MultiString
  )
}


# --- Paths / Constants --------------------------------------------------------
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$SvcName  = 'DMs'
$Display  = '4DReplay DMs Agent'
$LogDir   = Join-Path $RepoRoot 'logs\DMS'
$ServicePy= Join-Path $PSScriptRoot 'dms_service.py'

# NEW: 서비스 실행에 필요한 PATH 엔트리
$ExtraPathEntries = @(
  (Join-Path $RepoRoot 'library\daemon'),
  (Join-Path $RepoRoot 'library\ffmpeg')
)

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
# ensure packages
New-Item -ItemType File -Force "$RepoRoot\service\__init__.py"     | Out-Null
New-Item -ItemType File -Force "$RepoRoot\service\DMs\__init__.py" | Out-Null

# --- Python ------------------------------------------------------------------
function Resolve-Python {
  param([string]$Prefer)
  if ($Prefer -and (Test-Path $Prefer)) { return (Resolve-Path $Prefer).Path }

  try {
    $out = & py -0p 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) {
      $line = ($out | Select-String -Pattern "3\.10").Line
      if (-not $line) { $line = $out.Split("`n")[0] }
      $guess = $line -replace '.*\s+', ''
      if (Test-Path $guess) { return (Resolve-Path $guess).Path }
    }
  } catch {}

  foreach($p in @('C:\Program Files\Python310\python.exe','C:\Python310\python.exe')){
    if(Test-Path $p){ return (Resolve-Path $p).Path }
  }
  throw "Python 3.10 not found. Use -PythonPath to specify."
}

function Ensure-Pywin32 {
  param([string]$Py)
  Write-Info "Using Python: $Py"
  & $Py -m pip install --upgrade --no-warn-script-location pywin32 | Out-Host
}

# --- Service ops -------------------------------------------------------------
function Service-Exists {
  $p = Start-Process -FilePath $SC -ArgumentList "query $SvcName" -NoNewWindow -Wait -PassThru
  return ($p.ExitCode -eq 0)
}
function Service-Stop-Delete {
  if (Service-Exists) {
    Write-Info "Stopping old service..."
    Start-Process -FilePath $SC -ArgumentList "stop $SvcName" -NoNewWindow -Wait | Out-Null
    Start-Sleep -Milliseconds 500
    Write-Info "Deleting old service..."
    Start-Process -FilePath $SC -ArgumentList "delete $SvcName" -NoNewWindow -Wait | Out-Null
    Start-Sleep -Milliseconds 500
  }
}

function Write-Parameters {
  param(
    [string]$PyExe
  )
  $svcKey = $RegHKLM64.CreateSubKey("SYSTEM\CurrentControlSet\Services\$SvcName")
  $parKey = $RegHKLM64.CreateSubKey("SYSTEM\CurrentControlSet\Services\$SvcName\Parameters")

  $parKey.SetValue("PythonClass",     "service.DMs.dms_service.DMsService", [Microsoft.Win32.RegistryValueKind]::String)
  $parKey.SetValue("PythonClassName", "service.DMs.dms_service.DMsService", [Microsoft.Win32.RegistryValueKind]::String)
  $parKey.SetValue("AppDirectory",    $RepoRoot,                             [Microsoft.Win32.RegistryValueKind]::String)
  $parKey.SetValue("PythonPath",      "$RepoRoot;$RepoRoot\service;$RepoRoot\service\DMs", [Microsoft.Win32.RegistryValueKind]::String)
  $parKey.SetValue("CommandLine",     "DMs",                                 [Microsoft.Win32.RegistryValueKind]::String)
  $parKey.SetValue("PythonExe",       $PyExe,                                [Microsoft.Win32.RegistryValueKind]::String)
  # (보수용) 루트에도 PythonClass 복제
  $svcKey.SetValue("PythonClass",     "service.DMs.dms_service.DMsService", [Microsoft.Win32.RegistryValueKind]::String)
}

function Install-ViaWin32ServiceUtil {
  param([string]$Py)

  # pywin32 보장
  Ensure-Pywin32 -Py $Py

  # 기존 서비스 정리
  Service-Stop-Delete

  # 설치 (startup: auto/demand/disabled)
  $startup = switch ($StartType) {
    "auto"         { "auto" }
    "delayed-auto" { "auto" }  # install은 auto, 레지스트리로 delayed 처리
    "demand"       { "manual" }
    "disabled"     { "disabled" }
  }

  Write-Info "Installing service via win32serviceutil (--startup $startup)..."
  & $Py $ServicePy --startup $startup install | Out-Host

  # 지연자동시작 처리
  if ($StartType -eq "delayed-auto") {
    Write-Info "Enable DelayedAutoStart..."
    reg add "HKLM\SYSTEM\CurrentControlSet\Services\$SvcName" /v DelayedAutoStart /t REG_DWORD /d 1 /f | Out-Null
  } else {
    reg add "HKLM\SYSTEM\CurrentControlSet\Services\$SvcName" /v DelayedAutoStart /t REG_DWORD /d 0 /f | Out-Null
  }

  # Parameters 보강 (환경 의존성 제거)
  Write-Parameters -PyExe $Py
  # PATH 보강 (시스템 PATH + 서비스 전용 PATH)
  Ensure-SystemPathIncludes -Entries $ExtraPathEntries
  Set-ServiceEnvironmentPath -ServiceName $SvcName -ExtraEntries $ExtraPathEntries

  # 실패시 자동 재시작 (3회, 5초 간격)
  Write-Info "Configure failure actions..."
  Sc-Line "failure $SvcName reset= 60 actions= restart/5000/restart/5000/restart/5000"
  Sc-Line "failureflag $SvcName 1"

  # Log dir ACL for SYSTEM
  icacls $LogDir /grant "NT AUTHORITY\SYSTEM:(OI)(CI)(M)" /T | Out-Null

  # 방화벽(선택)
  if ($OpenFirewall) {
    $rule = "DMs-51050"
    if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
      New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort 51050 | Out-Null
      Write-Info "Firewall rule '$rule' added."
    } else {
      Write-Info "Firewall rule '$rule' already exists."
    }
  }
}

function Start-And-Show {
  Write-Info "Starting service..."
  Start-Process -FilePath $SC -ArgumentList "start $SvcName" -NoNewWindow -Wait | Out-Null
  Start-Sleep -Milliseconds 300
  & $SC query $SvcName
}

function Uninstall-Service {
  Write-Info "Uninstalling service..."
  Service-Stop-Delete
  # 레지스트리 흔적 제거(선택)
  reg delete "HKLM\SYSTEM\CurrentControlSet\Services\$SvcName" /f 2>$null | Out-Null
  Write-Info "Service '$SvcName' removed (if it existed)."
}

# --- Main --------------------------------------------------------------------
Assert-Admin

if ($Uninstall) {
  Uninstall-Service
  return
}

$Py = Resolve-Python -Prefer $PythonPath

Write-Host "RepoRoot   : $RepoRoot"
Write-Host "ServicePy  : $ServicePy"
Write-Host "PythonPath : $Py"
Write-Host "LogDir     : $LogDir"

Install-ViaWin32ServiceUtil -Py $Py
Start-And-Show

Write-Host "`nOK: DMs Service installed and started."
Write-Host "Check :  sc.exe qc $SvcName"
Write-Host "Query :  sc.exe query $SvcName"
Write-Host "Debug :  `"$Py`" `"$ServicePy`" debug   (Ctrl+C to stop)"

