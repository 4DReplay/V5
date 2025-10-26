# -----------------------------------------------------------------------------
# install_dms.ps1 (final, PowerShell-only, ASCII-safe)
# date: 2025-10-26
# owner: 4DReplay
# Usage:
#   powershell -ExecutionPolicy Bypass -File service\DMs\install_dms.ps1 -OpenFirewall
#   powershell -ExecutionPolicy Bypass -File service\DMs\install_dms.ps1 -Uninstall
# Options:
#   -PythonPath "C:\Program Files\Python310\python.exe"
#   -StartType auto|delayed-auto|demand|disabled  (default: delayed-auto)
#   -OpenFirewall  (allow TCP 51050)
#   -Uninstall     (remove the service)
# Requires: Administrator
# -----------------------------------------------------------------------------

param(
  [string]$PythonPath = "",
  [ValidateSet("auto","delayed-auto","demand","disabled")]
  [string]$StartType = "delayed-auto",
  [switch]$OpenFirewall = $false,
  [switch]$Uninstall = $false
)

$ErrorActionPreference = "Stop"

function Write-Info($m){ Write-Host $m -ForegroundColor Cyan }
function Write-Warn($m){ Write-Host $m -ForegroundColor Yellow }
function Write-Err ($m){ Write-Host $m -ForegroundColor Red   }

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $pr = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run as Administrator."
  }
}

# Always call real sc.exe (avoid 'sc' alias = Set-Content)
$SC = Join-Path $env:SystemRoot "System32\sc.exe"
if (-not (Test-Path $SC)) { throw "sc.exe not found: $SC" }
function Invoke-Sc { param([string[]]$Args) & $SC @Args }

# --- Paths -------------------------------------------------------------------
$RepoRoot  = (Resolve-Path "$PSScriptRoot\..\..").Path
$ServicePy = Join-Path $PSScriptRoot 'dms_service.py'
$SvcName   = 'DMs'
$Display   = '4DReplay DMs Agent'
$LogDir    = Join-Path $RepoRoot 'logs\DMS'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType File -Force "$RepoRoot\service\__init__.py"     | Out-Null
New-Item -ItemType File -Force "$RepoRoot\service\DMs\__init__.py" | Out-Null

# --- Resolve Python ----------------------------------------------------------
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

# --- Ensure pywin32 and locate/copy required files ---------------------------
function Ensure-Pywin32 {
  param([string]$Py)

  Write-Info "Using Python: $Py"
  & $Py -m pip install --upgrade --no-warn-script-location pywin32 | Out-Host

  # Resolve site-packages root
  $siteRoot = ""
  try { $siteRoot = (& $Py -c "import site; print(site.getsitepackages()[0])").Trim() } catch {}
  if (-not $siteRoot) { try { $siteRoot = (& $Py -c "import sysconfig; print(sysconfig.get_paths()['platlib'])").Trim() } catch {} }
  if (-not $siteRoot) { try { $siteRoot = (& $Py -c "from distutils.sysconfig import get_python_lib; print(get_python_lib())").Trim() } catch {} }
  if (-not $siteRoot -or -not (Test-Path $siteRoot)) {
    throw "Failed to resolve site-packages path."
  }

  # Fix case: siteRoot returns Python home (e.g., C:\Program Files\Python310)
  $pyHome = Split-Path $Py
  if ((Split-Path $siteRoot -Leaf) -ieq (Split-Path $pyHome -Leaf)) {
    $alt = Join-Path $pyHome 'Lib\site-packages'
    if (Test-Path $alt) { $siteRoot = $alt }
  }

  # Try user site-packages as fallback for DLLs
  $userSite = ""
  try { $userSite = (& $Py -c "import site; print(site.getusersitepackages())").Trim() } catch {}

  # Locate PythonService.exe (several strategies)
  $byModule1 = ""; $byModule2 = ""
  try { $byModule1 = (& $Py -c "import pathlib, win32serviceutil; print(pathlib.Path(win32serviceutil.__file__).with_name('PythonService.exe'))").Trim() } catch {}
  try { $byModule2 = (& $Py -c "import pathlib, win32api; print(pathlib.Path(win32api.__file__).parent / 'PythonService.exe')").Trim() } catch {}

  $candidates = @()
  if ($byModule1) { $candidates += $byModule1 }
  if ($byModule2) { $candidates += $byModule2 }
  $candidates += @(
    (Join-Path $siteRoot 'win32\PythonService.exe'),
    (Join-Path $siteRoot 'win32\pythonservice.exe'),
    (Join-Path $siteRoot 'pywin32_system32\PythonService.exe'),
    (Join-Path $siteRoot 'pywin32_system32\pythonservice.exe')
  )

  $found = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  if (-not $found) {
    throw "PythonService.exe not found. Tried: $($candidates -join ', ')"
  }
  Write-Info "PythonService resolved: $found"

  # Ensure required DLLs (copy to Python home if missing)
  $srcDLLDir = Join-Path $siteRoot 'pywin32_system32'
  if (-not (Test-Path $srcDLLDir) -and $userSite) {
    $cand = Join-Path $userSite 'pywin32_system32'
    if (Test-Path $cand) { $srcDLLDir = $cand }
  }

  $pyw = Join-Path $pyHome 'pywintypes310.dll'
  $pcom = Join-Path $pyHome 'pythoncom310.dll'
  if (-not (Test-Path $pyw) -or -not (Test-Path $pcom)) {
    if (-not (Test-Path $srcDLLDir)) {
      throw "pywin32_system32 not found under site-packages. Looked in: $srcDLLDir"
    }
    $srcPwy = Join-Path $srcDLLDir 'pywintypes310.dll'
    $srcPcm = Join-Path $srcDLLDir 'pythoncom310.dll'
    if (-not (Test-Path $srcPwy) -or -not (Test-Path $srcPcm)) {
      throw "Required DLLs not found in $srcDLLDir"
    }
    Copy-Item $srcPwy $pyw -Force
    Copy-Item $srcPcm $pcom -Force
    Write-Info "Copied DLLs to Python home: $pyHome"
  }

  return (Resolve-Path $found).Path
}

# --- Install Service (SCM + Parameters) --------------------------------------
function Install-Service {
  param([string]$Py,[string]$SvcExe)

  Write-Info "Installing service via SCM..."

  Invoke-Sc @("stop",   $SvcName) 2>$null | Out-Null
  Invoke-Sc @("delete", $SvcName) 2>$null | Out-Null
  Start-Sleep -Seconds 1
  $svcKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$SvcName"
  if (Test-Path $svcKey) { try { Remove-Item $svcKey -Recurse -Force -ErrorAction SilentlyContinue } catch {} }

  $startFlag = switch ($StartType) {
    "auto"         { "auto" }
    "delayed-auto" { "auto" }  # set DelayedAutoStart=1 below
    "demand"       { "demand" }
    "disabled"     { "disabled" }
  }

  $quotedBin = "`"$SvcExe`""
  # sc.exe requires 'type= own' and 'binPath= "<exe>"' with spaces kept together.
  $createCmd = "create `"$SvcName`" type= own binPath= $quotedBin start= $startFlag DisplayName= `"$Display`""
  $createOut = & "$env:SystemRoot\System32\cmd.exe" /c "`"$SC`" $createCmd"
  if ($LASTEXITCODE -ne 0 -or ($createOut -match 'FAILED|USAGE|DESCRIPTION')) {
    throw "sc create failed: $createOut"
  }

  Invoke-Sc @("description", $SvcName, "Process supervisor + HTTP control for 4DReplay") | Out-Null

  # Keep ImagePath quoted
  Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$SvcName" -Name ImagePath -Type ExpandString -Value $quotedBin

  # Parameters for pywin32 host
  $base = Join-Path $svcKey 'Parameters'
  New-Item -Path $base -Force | Out-Null
  $PyPath = "$RepoRoot;$RepoRoot\service;$RepoRoot\service\DMs"
  New-ItemProperty -Path $base -Name PythonClass     -Value 'service.DMs.dms_service.DMsService' -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $base -Name PythonClassName -Value 'service.DMs.dms_service.DMsService' -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $base -Name AppDirectory    -Value $RepoRoot -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $base -Name PythonPath      -Value $PyPath   -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $base -Name CommandLine     -Value 'DMs'     -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $base -Name PythonExe       -Value $Py       -PropertyType String -Force | Out-Null

  if ($StartType -eq "delayed-auto") {
    $cur = Get-ItemProperty -Path $svcKey -Name DelayedAutoStart -ErrorAction SilentlyContinue
    if ($null -eq $cur) {
      New-ItemProperty -Path $svcKey -Name DelayedAutoStart -PropertyType DWord -Value 1 -Force | Out-Null
    } else {
      Set-ItemProperty -Path $svcKey -Name DelayedAutoStart -Value 1
    }
  } else {
    try { Set-ItemProperty -Path $svcKey -Name DelayedAutoStart -Value 0 -ErrorAction SilentlyContinue } catch {}
  }

  # Give SCM more time for pywin32 bootstrap (optional)
  New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control' -Name 'ServicesPipeTimeout' -PropertyType DWord -Value 120000 -Force | Out-Null

  # Log dir ACL for SYSTEM
  icacls $LogDir /grant "NT AUTHORITY\SYSTEM:(OI)(CI)(M)" /T | Out-Null

  if ($OpenFirewall) {
    $rule = "DMs-51050"
    if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
      New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort 51050 | Out-Null
      Write-Info "Firewall rule '$rule' added."
    } else {
      Write-Info "Firewall rule '$rule' already exists."
    }
  }

  try { Invoke-Sc @("start", $SvcName) | Out-Null } catch { Write-Warn "sc start failed: $($_.Exception.Message)" }
}

# --- Uninstall ---------------------------------------------------------------
function Uninstall-Service {
  Write-Info "Removing service '$SvcName'..."
  Invoke-Sc @("stop",   $SvcName) 2>$null | Out-Null
  Invoke-Sc @("delete", $SvcName) 2>$null | Out-Null
  $svcKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$SvcName"
  if (Test-Path $svcKey) { try { Remove-Item $svcKey -Recurse -Force -ErrorAction SilentlyContinue } catch {} }
  Write-Info "Service '$SvcName' removed (if it existed)."
}

# --- Main --------------------------------------------------------------------
Assert-Admin

if ($Uninstall) { Uninstall-Service; return }

$Py     = Resolve-Python -Prefer $PythonPath
$SvcExe = Ensure-Pywin32 -Py $Py

Write-Host "RepoRoot   : $RepoRoot"
Write-Host "ServicePy  : $ServicePy"
Write-Host "PythonPath : $Py"
Write-Host "LogDir     : $LogDir"
Write-Host "SvcBinary  : $SvcExe"

Install-Service -Py $Py -SvcExe $SvcExe

Write-Host "`nOK: DMs Service installed and started."
Write-Host "Check:  & `"$env:SystemRoot\System32\sc.exe`" qc DMs"
Write-Host "Open :  http://localhost:51050/status"
