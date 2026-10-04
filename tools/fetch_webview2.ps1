# Laedt den WebView2-Runtime-Bootstrapper von Microsoft (rund 2 MB) nach installer\webview2\.
# Der Installer fuehrt ihn aus, wenn auf dem Zielrechner keine WebView2-Runtime gefunden wird (beobachtet in der
# Windows Sandbox). Mitliefern ist laut Microsoft ausdruecklich vorgesehen:
#   https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution
# Statt einer festen Pruefsumme - Microsoft aktualisiert die Datei regelmaessig - wird die Signatur geprueft:
# gueltig und von Microsoft Corporation, sonst Abbruch.
# Aufruf: pwsh -NoProfile -ExecutionPolicy Bypass -File tools\fetch_webview2.ps1
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot
$ZielDir = Join-Path $Root 'installer\webview2'
$Ziel = Join-Path $ZielDir 'MicrosoftEdgeWebview2Setup.exe'
$Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'   # Evergreen Bootstrapper ("Get the Link")

New-Item -ItemType Directory -Force $ZielDir | Out-Null
$tmp = Join-Path $ZielDir 'download.tmp'
Write-Host "Lade WebView2-Bootstrapper: $Url"
Invoke-WebRequest -Uri $Url -OutFile $tmp -MaximumRedirection 5

$sig = Get-AuthenticodeSignature $tmp
if ($sig.Status -ne 'Valid') {
    Remove-Item $tmp -Force
    throw "Signatur des Bootstrappers ungueltig: $($sig.Status) - $($sig.StatusMessage)"
}
if ($sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
    Remove-Item $tmp -Force
    throw "Bootstrapper nicht von Microsoft signiert: $($sig.SignerCertificate.Subject)"
}
Move-Item $tmp $Ziel -Force
$datei = Get-Item $Ziel
Write-Host ("  {0} {1}, {2:N1} MB, signiert von Microsoft Corporation" -f `
    $datei.VersionInfo.ProductName, $datei.VersionInfo.FileVersion, ($datei.Length / 1MB))
