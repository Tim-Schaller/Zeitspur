# Baut Zeitspur komplett: venv -> Tests -> PyInstaller -> Tesseract-Bundle -> Inno-Setup-Installer.
#   .\build.ps1                    kompletter Build, Ergebnis dist\ZeitspurSetup.exe
#   .\build.ps1 -SkipTests         ohne Testlauf
#   .\build.ps1 -SkipInstaller     nur dist\Zeitspur\ erzeugen
#   .\build.ps1 -Release           Release-Ausgabe fuer andere: ohne unfertige Funktionen (derzeit
#                                  Standort-Historie/Karte), Ergebnis dist-release\ZeitspurSetup.exe.
#                                  Eigene Ordner, damit der normale Build daneben unberuehrt bleibt.
[CmdletBinding()]   # unbekannte Argumente sind ein Fehler, statt still ignoriert zu werden
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$Release
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$distName = if ($Release) { 'dist-release' } else { 'dist' }
$workName = if ($Release) { 'build-release' } else { 'build' }
$distDir = Join-Path $root $distName
$appDir = Join-Path $distDir 'Zeitspur'
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host '== venv anlegen und Abhaengigkeiten installieren'
    & py -3.13 -m venv .venv
    if ($LASTEXITCODE) { & python -m venv .venv }
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet -r requirements-dev.txt
}

if (-not (Test-Path (Join-Path $root 'installer\tesseract-portable\tesseract.exe'))) {
    Write-Host '== Tesseract-Bundle fehlt, wird geladen'
    & pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'tools\fetch_tesseract.ps1')
    if ($LASTEXITCODE) { throw 'fetch_tesseract.ps1 fehlgeschlagen' }
}

if (-not (Test-Path (Join-Path $root 'installer\webview2\MicrosoftEdgeWebview2Setup.exe'))) {
    Write-Host '== WebView2-Bootstrapper fehlt, wird geladen'
    & pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'tools\fetch_webview2.ps1')
    if ($LASTEXITCODE) { throw 'fetch_webview2.ps1 fehlgeschlagen' }
}

Write-Host '== Icon erzeugen'
& $py (Join-Path $root 'tools\make_icon.py')
if ($LASTEXITCODE) { throw 'make_icon.py fehlgeschlagen' }

if (-not $SkipTests) {
    Write-Host '== Tests'
    & $py -m pytest -q
    if ($LASTEXITCODE) { throw 'Tests fehlgeschlagen' }
}

Write-Host ('== PyInstaller' + $(if ($Release) { ' (Release-Ausgabe)' } else { '' }))
Remove-Item (Join-Path $root $workName), $appDir -Recurse -Force -ErrorAction SilentlyContinue
$env:ZEITSPUR_EDITION = if ($Release) { 'release' } else { '' }
try {
    & $py -m PyInstaller --noconfirm --clean --log-level WARN --distpath $distDir `
        --workpath (Join-Path $root $workName) (Join-Path $root 'zeitspur.spec')
    if ($LASTEXITCODE) { throw 'PyInstaller fehlgeschlagen' }
} finally {
    Remove-Item Env:\ZEITSPUR_EDITION -ErrorAction SilentlyContinue
}

$channel = Join-Path $appDir '_internal\zeitspur\release_channel.txt'
if ($Release) {
    # Waechter: Was im Release fehlen soll, darf nicht doch im Paket stecken (etwa weil ein neuer Import
    # das Modul hereinzieht). Dann lieber gar kein Release als ein falscher.
    Write-Host '== Release-Waechter: keine Standort-Historie, keine Kartenbibliothek im Paket?'
    $verboten = Get-ChildItem $appDir -Recurse -File | Where-Object { $_.Name -match '^(dawarich|leaflet)\.' }
    if ($verboten) { throw ('Im Release-Paket gefunden: ' + (($verboten | ForEach-Object Name) -join ', ')) }
    if (-not (Test-Path $channel)) { throw 'Release-Paket ohne release_channel.txt - es wuerde sich nie selbst aktualisieren' }
} elseif (Test-Path $channel) {
    # Der eigene Build (mit Standort-Historie) darf sich nie durch ein Release ersetzen
    throw 'Eigener Build enthaelt release_channel.txt - er wuerde sich durch Releases ersetzen'
}

Write-Host '== Virenscanner-Wächter: bleiben die EXEs 10 s nach dem Build erhalten?'
Start-Sleep -Seconds 10
$expected = @('Zeitspur.exe')
if ($env:ZEITSPUR_BUILD_MCP_EXE -eq '1') { $expected += 'ZeitspurMCP.exe' }
foreach ($exe in $expected) {
    if (-not (Test-Path (Join-Path $appDir $exe))) {
        throw "$exe ist nach dem Build verschwunden - vermutlich vom Virenscanner entfernt. Quarantaene pruefen bzw. Ausnahme eintragen lassen."
    }
}

Write-Host '== Tesseract-Bundle kopieren'
$tessDst = Join-Path $appDir 'tesseract'
Remove-Item $tessDst -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $root 'installer\tesseract-portable') $tessDst -Recurse

Write-Host '== Lizenzen (LICENSE.txt, THIRD-PARTY-NOTICES.txt)'
Copy-Item (Join-Path $root 'LICENSE') (Join-Path $appDir 'LICENSE.txt') -Force
$noticeArgs = @((Join-Path $root 'tools\third_party_notices.py'), '--out', (Join-Path $appDir 'THIRD-PARTY-NOTICES.txt'))
if ($Release) { $noticeArgs += '--release' }
& $py @noticeArgs
if ($LASTEXITCODE) { throw 'third_party_notices.py fehlgeschlagen' }

$size = (Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("== {0}\Zeitspur: {1:N0} MB" -f $distName, $size)

if (-not $SkipInstaller) {
    $iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
    if (-not (Test-Path $iscc)) { $iscc = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe' }
    if (-not (Test-Path $iscc)) { throw 'ISCC.exe nicht gefunden - tools\install_innosetup.ps1 ausfuehren' }
    # Einzige Quelle der Versionsnummer ist zeitspur/__init__.py - so tragen Setup und Programm dieselbe.
    $version = "$(& $py -c 'import zeitspur; print(zeitspur.__version__)')".Trim()
    if ($LASTEXITCODE -or -not $version) { throw 'Versionsnummer aus zeitspur/__init__.py nicht lesbar' }
    Write-Host "== Inno Setup (Version $version)"
    # Bis zu drei Versuche: Der Virenscanner haelt die frisch geschriebene Setup.exe gelegentlich noch fest,
    # wenn Inno Setup die Versionsinfo hineinschreibt (beobachtet am 03.10.2026, beim zweiten Lauf ging es).
    $ok = $false
    for ($versuch = 1; $versuch -le 3 -and -not $ok; $versuch++) {
        $ausgabe = & $iscc /Qp "/O$distDir" "/DDistDir=$appDir" "/DMyAppVersion=$version" (Join-Path $root 'installer\installer.iss') 2>&1
        if ($LASTEXITCODE -eq 0) { $ok = $true; continue }
        Write-Host "   ISCC-Versuch $versuch fehlgeschlagen (Exitcode $LASTEXITCODE):"
        $ausgabe | Select-Object -Last 5 | ForEach-Object { Write-Host "     $_" }
        Start-Sleep -Seconds 5
    }
    if (-not $ok) { throw 'ISCC fehlgeschlagen (Meldungen siehe oben)' }
    $setup = Join-Path $distDir 'ZeitspurSetup.exe'
    Write-Host ("== Fertig: {0} ({1:N1} MB, Version {2})" -f $setup, ((Get-Item $setup).Length / 1MB), $version)
}
