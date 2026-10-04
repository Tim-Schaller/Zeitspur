# Laeuft IN der Windows Sandbox: installiert Zeitspur so, wie ein Kollege es tun wuerde, und startet es.
# Erzeugt und gestartet von tools\sandbox_test.ps1 - nicht direkt auf dem eigenen PC ausfuehren.
# Bewusst ohne Umlaute: Windows PowerShell 5.1 liest Skripte ohne BOM als ANSI.
$ErrorActionPreference = 'Continue'
$Host.UI.RawUI.WindowTitle = 'Zeitspur - Installationstest in der Windows Sandbox'
$app = Join-Path $env:LOCALAPPDATA 'Programs\Zeitspur'
$wvGuid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$manifest = 'C:\ZeitspurTest\channel\latest.json'
$updateTest = Test-Path $manifest
if ($updateTest) {
    # Update-Test (sandbox_test.ps1 -UpdateTest): Updates aus dem Ordner channel statt von GitHub, erste Pruefung
    # nach 20 s, den Leerlauf bestimmt die Einstellung. Die Variablen gelten fuer das Setup, das daraus gestartete
    # Programm und - dauerhaft fuer dieses Sandbox-Konto - auch fuer Neustarts ueber Startmenue oder Autostart.
    $testKanal = @{ ZEITSPUR_UPDATE_URL = 'file:///C:/ZeitspurTest/channel/latest.json'; ZEITSPUR_UPDATE_TIMING = '20,60' }
    foreach ($name in $testKanal.Keys) {
        Set-Item "env:$name" $testKanal[$name]
        [Environment]::SetEnvironmentVariable($name, $testKanal[$name], 'User')
    }
}

function Schritt([string]$text) {
    Write-Host ''
    Write-Host "== $text" -ForegroundColor Cyan
    Start-Sleep -Seconds 2   # Zeit zum Mitlesen
}
function Ok([string]$text) { Write-Host "   [ok] $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "   [!]  $text" -ForegroundColor Yellow }
function Hinweis([string]$text) { Write-Host "   $text" }
function WebView2Version {
    # Wie Zeitspur selbst: pro Rechner oder - so installiert sie das Setup ohne Adminrechte - pro Benutzer
    foreach ($pfad in "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$wvGuid",
                      "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$wvGuid") {
        $pv = (Get-ItemProperty $pfad -ErrorAction SilentlyContinue).pv
        if ($pv -and $pv -ne '0.0.0.0') { return $pv }
    }
    return $null
}
function Programmordner {
    if (Test-Path (Join-Path $app 'Zeitspur.exe')) { return $app }
    return $null
}

Write-Host 'Zeitspur - Installationstest' -ForegroundColor White
Write-Host 'Diese Sandbox ist ein frisches Windows und wird beim Schliessen vollstaendig verworfen.'

Schritt 'Ausgangslage pruefen'
Hinweis "Benutzer: $env:USERNAME (Standardbenutzer der Sandbox)"
if (Programmordner) { Warn 'Zeitspur ist bereits installiert?' } else { Ok 'Zeitspur ist nicht installiert' }
$wvVorher = WebView2Version
if ($wvVorher) { Ok "WebView2-Runtime vorhanden ($wvVorher)" }
else { Warn 'WebView2-Runtime fehlt - das Setup laedt sie mit eigener Fortschrittsseite nach' }
if ($updateTest) { Ok 'Update-Test: Updates kommen aus C:\ZeitspurTest\channel statt von GitHub' }
if (Get-Command python -ErrorAction SilentlyContinue) { Hinweis 'Python ist vorhanden (wird nicht gebraucht)' }
else { Ok 'Kein Python installiert - Zeitspur bringt alles selbst mit' }

Schritt 'Installation - bitte selbst durchklicken, genau wie ein Kollege'
$setup = Get-Item 'C:\ZeitspurTest\Setup.exe'
$setupVersion = $setup.VersionInfo.ProductVersion
Hinweis ("Setup Version {0}, {1} MB" -f $setupVersion, [math]::Round($setup.Length / 1MB))
Hinweis 'Das Setup oeffnet sich gleich: Weiter -> Installieren -> Fertigstellen.'
Hinweis 'Auf der letzten Seite "... jetzt starten" angehakt lassen.'
$sw = [Diagnostics.Stopwatch]::StartNew()
# Nicht "Start-Process -Wait": Das wartet auch auf Kindprozesse - also auf das von der letzten Setup-Seite
# gestartete Programm, das weiterlaeuft. Gewartet wird nur auf das Setup selbst.
$p = Start-Process $setup.FullName -PassThru
$null = $p.Handle   # Handle festhalten, sonst ist ExitCode nach dem Ende leer
$p.WaitForExit()
if ($p.ExitCode -eq 0) { Ok ("Setup abgeschlossen nach {0:N0} s" -f $sw.Elapsed.TotalSeconds) }
else { Warn "Setup beendet mit Code $($p.ExitCode) (abgebrochen?)" }

Schritt 'Was wurde eingerichtet?'
$ordner = Programmordner
if ($ordner) {
    $mb = (Get-ChildItem $ordner -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
    Ok ("Programm: {0} ({1:N0} MB), Version {2}" -f $ordner, $mb, $setupVersion)
} else { Warn 'Programmordner fehlt' }
$wvNachher = WebView2Version
if ($wvNachher -and -not $wvVorher) { Ok "WebView2-Runtime vom Setup nachinstalliert ($wvNachher)" }
elseif ($wvNachher) { Ok "WebView2-Runtime vorhanden ($wvNachher)" }
else { Warn 'WebView2-Runtime fehlt weiterhin - Zeitspur bietet beim Start den Download an' }
$fremd = Get-ChildItem $ordner -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^(dawarich|leaflet)\.' }
if ($fremd) { Warn ('Im Paket gefunden: ' + (($fremd | ForEach-Object Name) -join ', ')) }
else { Ok 'Keine Standort-Historie im Paket (Release-Ausgabe)' }

Schritt 'Erster Start'
Hinweis 'Zuerst erscheint ein kleines Startfenster mit laufendem Balken, danach das Hauptfenster mit der'
Hinweis 'Ersteinrichtung. "Speichern und starten" legt Schluessel und Datenbank an, danach laeuft die Aufnahme.'

if ($updateTest) {
    $neu = (Get-Content $manifest -Raw | ConvertFrom-Json).payload.version
    Schritt "Update-Test: Version $neu liegt bereit"
    Hinweis 'In der Ersteinrichtung unter "Updates" den Leerlauf auf 1 Minute stellen (Standard 5), dann'
    Hinweis '"Speichern und starten". Nach etwa 20 Sekunden wird die neue Version gefunden, geladen und gemeldet.'
    Hinweis 'Danach Maus und Tastatur so lange nicht anfassen (Maus aus dem Sandbox-Fenster nehmen): Ein kleines'
    Hinweis 'Fenster zeigt den Fortschritt, danach meldet sich Zeitspur mit dem Startfenster zurueck.'
    Hinweis 'Jeder Schritt erscheint hier, sobald er passiert.'
    $logs = @(Join-Path $env:LOCALAPPDATA 'Zeitspur\logs\service.log')
    $etappen = [ordered]@{
        'Update verfuegbar'             = "Version $neu gefunden"
        'geladen und geprueft'          = 'geladen, Signatur und Pruefsumme stimmen - wartet auf den Leerlauf'
        'Installiere Update'            = 'Leerlauf erkannt - Fortschrittsfenster, gleich geht es mit der neuen Version weiter'
        "Update auf $neu abgeschlossen" = "Zeitspur laeuft jetzt mit Version $neu"
    }
    $gesehen = @{}
    $ende = (Get-Date).AddMinutes(30)
    while ($gesehen.Count -lt $etappen.Count -and (Get-Date) -lt $ende) {
        $text = ($logs | Where-Object { Test-Path $_ } | ForEach-Object { Get-Content $_ -Raw -ErrorAction SilentlyContinue }) -join "`n"
        foreach ($k in $etappen.Keys) {
            if (-not $gesehen[$k] -and $text -and $text.Contains($k)) { $gesehen[$k] = $true; Ok $etappen[$k] }
        }
        Start-Sleep -Seconds 2
    }
    if ($gesehen.Count -lt $etappen.Count) { Warn ("Nicht alle Schritte innerhalb von 30 Minuten gesehen - Protokoll: " + ($logs -join ' bzw. ')) }

    Schritt 'Nach dem Update: ist alles an seinem Platz?'
    Start-Sleep -Seconds 10   # Startmenue und Autostart schreibt das Setup
    if (Test-Path (Join-Path $app 'Zeitspur.exe')) { Ok "Programm: $app" } else { Warn "Zeitspur.exe fehlt in $app" }
    $runKey = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
    if ($runKey.Zeitspur) { Ok 'Autostart: Zeitspur' } else { Hinweis 'Autostart nicht eingetragen' }
    $menu = Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs" -Recurse -Filter '*Zeitspur*' -ErrorAction SilentlyContinue
    if ($menu) { Ok ("Startmenue: " + (($menu | ForEach-Object BaseName | Select-Object -Unique) -join ', ')) } else { Warn 'Kein Startmenue-Eintrag' }
    if (Test-Path (Join-Path $env:LOCALAPPDATA 'Zeitspur\zeitspur.db')) { Ok 'Datenordner: Zeitspur (Datenbank vorhanden)' }
    else { Warn 'Datenbank nicht im Datenordner Zeitspur' }
}
Write-Host ''
Write-Host 'Fertig. Zum Beenden einfach das Sandbox-Fenster schliessen - alles darin wird verworfen.' -ForegroundColor White
