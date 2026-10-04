# Stiller Installations- und Deinstallationstest fuer dist\ZeitspurSetup.exe (pro Benutzer, ohne Admin).
#   pwsh -File tools\installer_smoke.ps1            installiert ohne Tasks, prueft, deinstalliert wieder
#   pwsh -File tools\installer_smoke.ps1 -KeepInstalled   laesst die Installation stehen (Autostart-Task aktiv)
param([switch]$KeepInstalled)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$setup = Join-Path $root 'dist\ZeitspurSetup.exe'
if (-not (Test-Path $setup)) { throw "Setup fehlt: $setup" }
$app = Join-Path $env:LOCALAPPDATA 'Programs\Zeitspur'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$uninstKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B1F0C3E2-6A5D-4F0B-9C0E-7D3A2F1E8C44}_is1'
$ok = $true
function Check($cond, $msg) { if ($cond) { Write-Host "  OK   $msg" } else { Write-Host "  FEHL $msg"; $script:ok = $false } }

Write-Host "== Installation (silent) nach $app"
$tasks = if ($KeepInstalled) { 'autostart' } else { '' }
$p = Start-Process -FilePath $setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/TASKS=`"$tasks`"", '/LOG=' + (Join-Path $env:TEMP 'ZeitspurSetup.log')) -Wait -PassThru
Check ($p.ExitCode -eq 0) "Setup Exit-Code $($p.ExitCode)"
Check (Test-Path (Join-Path $app 'Zeitspur.exe')) 'Zeitspur.exe installiert'
Check (Test-Path (Join-Path $app '_internal')) '_internal vorhanden'
Check (Test-Path (Join-Path $app 'tesseract\tesseract.exe')) 'tesseract.exe vorhanden'
Check (Test-Path (Join-Path $app 'unins000.exe')) 'Uninstaller vorhanden'
Check (Test-Path $uninstKey) 'Uninstall-Registrierung (HKCU) vorhanden'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Zeitspur'
Check (Test-Path (Join-Path $startMenu 'Zeitspur.lnk')) 'Startmenue-Verknuepfung vorhanden'
$run = (Get-ItemProperty $runKey -ErrorAction SilentlyContinue).Zeitspur
if ($KeepInstalled) { Check ($run -like '*Zeitspur.exe" --autostart') "Autostart-Eintrag: $run" }
else { Check (-not $run) 'kein Autostart-Eintrag (Task nicht gewaehlt)' }
Start-Sleep -Seconds 5
Check (Test-Path (Join-Path $app 'Zeitspur.exe')) 'EXE nach 5 s weiterhin vorhanden (Virenscanner)'

Write-Host '== MCP-Modus der installierten EXE (--mcp --print-config in Konsole)'
$cfg = & (Join-Path $app 'Zeitspur.exe') --mcp --print-config --quiet 2>&1 | Out-String
Check ($cfg -match 'Zeitspur.exe' -and $cfg -match '--mcp') 'print-config nennt EXE mit --mcp'

if ($KeepInstalled) { Write-Host '== Installation bleibt bestehen'; if (-not $ok) { exit 1 } else { exit 0 } }

Write-Host '== Deinstallation (silent, Daten behalten)'
$u = Start-Process -FilePath (Join-Path $app 'unins000.exe') -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
Check ($u.ExitCode -eq 0) "Uninstaller Exit-Code $($u.ExitCode)"
Start-Sleep -Seconds 2
Check (-not (Test-Path (Join-Path $app 'Zeitspur.exe'))) 'Programmdateien entfernt'
Check (-not (Test-Path $uninstKey)) 'Uninstall-Registrierung entfernt'
Check (-not (Get-ItemProperty $runKey -ErrorAction SilentlyContinue).Zeitspur) 'Autostart-Eintrag entfernt'
Check (-not (Test-Path $startMenu)) 'Startmenue-Ordner entfernt'
$data = Join-Path $env:LOCALAPPDATA 'Zeitspur'
Write-Host ("  INFO Datenordner existiert: {0} (Standardantwort 'Nein' -> Daten bleiben)" -f (Test-Path $data))
if ($ok) { Write-Host 'ERGEBNIS: OK' } else { Write-Host 'ERGEBNIS: FEHLER'; exit 1 }
