# Spielt die Installation von Zeitspur in einer Windows Sandbox durch - so, wie ein Kollege sie erlebt:
# frisches Windows, kein Python, keine Daten. Die Sandbox wird beim Schliessen vollstaendig verworfen.
#   .\tools\sandbox_test.ps1               Release-Ausgabe (dist-release\ZeitspurSetup.exe) - das, was auf GitHub liegt
#   .\tools\sandbox_test.ps1 -Dev          eigener Build mit allen Funktionen (dist\ZeitspurSetup.exe)
#   .\tools\sandbox_test.ps1 -UpdateTest   zusaetzlich das automatische Update: baut eine Testversion <Version>.1,
#                                          signiert sie und legt sie in einen Update-Kanal im Sandbox-Ordner
#   .\tools\sandbox_test.ps1 -UpdateTest -FromSetup <Setup>
#                                          echter Update-Weg: installiert ein aelteres, veroeffentlichtes Setup und
#                                          legt die aktuelle Version in den Kanal (z. B. das Update von der
#                                          zuletzt veroeffentlichten Version pruefen)
#   .\tools\sandbox_test.ps1 -NoStart      nur vorbereiten, nicht starten (es darf nur eine Sandbox laufen)
# Voraussetzung: Windows-Feature "Windows-Sandbox" - einmalig mit Adminrechten einschalten, dann neu starten.
#   -Stage <Ordner> -Title <Name>          eigener Sandbox-Ordner und eigene .wsb-Datei, etwa fuer einen zweiten Test
#                                          neben einem schon vorbereiteten (Standard: sandbox, Installationstest)
param([switch]$Dev, [switch]$NoStart, [switch]$UpdateTest, [string]$FromSetup, [string]$Stage = 'sandbox',
      [string]$Title = 'Installationstest')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$py = Join-Path $root '.venv\Scripts\python.exe'
$utf8 = [Text.UTF8Encoding]::new($false)
$distName = if ($Dev) { 'dist' } else { 'dist-release' }
$built = Join-Path $root "$distName\ZeitspurSetup.exe"
if ($FromSetup -and -not $UpdateTest) { throw '-FromSetup gehoert zu -UpdateTest' }
if ($FromSetup -and -not (Test-Path $FromSetup)) { throw "Setup nicht gefunden: $FromSetup" }

# Nur dieser Ordner wird in die Sandbox eingebunden, und zwar schreibgeschuetzt. Alles andere auf diesem PC -
# auch die eigene Zeitspur-Datenbank - bleibt fuer die Sandbox unsichtbar.
$stage = Join-Path $root "$distName\$Stage"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
$channel = Join-Path $stage 'channel'

function Sign-Into-Channel([string]$setupFile, [string]$version, [string]$notesText) {
    New-Item -ItemType Directory -Force $channel | Out-Null
    $target = Join-Path $channel "ZeitspurSetup-$version.exe"
    Copy-Item $setupFile $target
    $notes = Join-Path $channel 'changes.md'
    [IO.File]::WriteAllText($notes, $notesText, $utf8)
    & $py (Join-Path $root 'tools\release_key.py') manifest --setup $target --version $version `
        --notes-file $notes --out (Join-Path $channel 'latest.json')
    if ($LASTEXITCODE) { throw 'latest.json liess sich nicht signieren' }
    Remove-Item $notes
}

if ($UpdateTest) {
    if ($Dev) { throw '-UpdateTest gibt es nur fuer die Release-Ausgabe - nur sie aktualisiert sich selbst' }
    $init = Join-Path $root 'zeitspur\__init__.py'
    $original = [IO.File]::ReadAllText($init, $utf8)
    $version = [regex]::Match($original, '__version__ = "([0-9.]+)"').Groups[1].Value
    if ($FromSetup) {
        Write-Host "== Update-Test: installiert wird $(Split-Path $FromSetup -Leaf), im Kanal liegt $version"
        & (Join-Path $root 'build.ps1') -Release -SkipTests
        Sign-Into-Channel $built $version '- Update-Test in der Windows Sandbox'
    } else {
        $testVersion = "$version.1"   # vierstellig: neuer als $version, aber nie eine echte Release-Nummer
        Write-Host "== Update-Test: zuerst Testversion $testVersion fuer den Kanal, danach $version zum Installieren"
        try {
            [IO.File]::WriteAllText($init, $original.Replace("__version__ = `"$version`"", "__version__ = `"$testVersion`""), $utf8)
            & (Join-Path $root 'build.ps1') -Release -SkipTests
        } finally {
            [IO.File]::WriteAllText($init, $original, $utf8)   # Quelltext bleibt unveraendert
        }
        Sign-Into-Channel $built $testVersion '- Testversion fuer den Update-Test in der Windows Sandbox'
        & (Join-Path $root 'build.ps1') -Release -SkipTests   # dist-release wieder auf die echte Version
    }
}

$installer = if ($FromSetup) { $FromSetup } else { $built }
if (-not (Test-Path $installer)) {
    throw "Kein Installer unter $installer - zuerst .\build.ps1$(if (-not $Dev) { ' -Release' }) ausfuehren"
}
Copy-Item $installer (Join-Path $stage 'Setup.exe') -Force
Copy-Item (Join-Path $PSScriptRoot 'sandbox\demo.ps1') $stage -Force

$wsb = Join-Path $stage "Zeitspur-$Title.wsb"
@"
<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$stage</HostFolder>
      <SandboxFolder>C:\ZeitspurTest</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoExit -ExecutionPolicy Bypass -File C:\ZeitspurTest\demo.ps1</Command>
  </LogonCommand>
</Configuration>
"@ | Set-Content -Path $wsb -Encoding UTF8

$sandboxExe = Join-Path $env:windir 'System32\WindowsSandbox.exe'
if ($NoStart) {
    Write-Host "Vorbereitet: $wsb$(if ($UpdateTest) { ' (mit Update-Kanal)' })"
    exit 0
}
if (-not (Test-Path $sandboxExe)) {
    Write-Host "Vorbereitet: $wsb"
    Write-Host ''
    Write-Host 'Windows Sandbox ist auf diesem PC nicht eingeschaltet. Einmalig mit Adminrechten:'
    Write-Host '  Start -> "Windows-Features aktivieren oder deaktivieren" -> "Windows-Sandbox" anhaken -> neu starten'
    Write-Host 'Danach dieses Skript erneut ausfuehren oder die .wsb-Datei doppelklicken.'
    exit 2
}
Write-Host "Starte Windows Sandbox mit $(Split-Path $installer -Leaf) ..."
Start-Process $sandboxExe -ArgumentList "`"$wsb`""
