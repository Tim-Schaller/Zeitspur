# Veroeffentlicht einen Release auf GitHub: baut die Release-Ausgabe, signiert latest.json und laedt beides hoch.
# Installierte Release-Ausgaben holen sich das Update danach selbst (zeitspur/updater.py).
#   pwsh -File tools\publish_release.ps1            kompletter Ablauf (mit Tests)
#   pwsh -File tools\publish_release.ps1 -DryRun    nur bauen, signieren und pruefen - nichts hochladen
# Voraussetzungen: Release-Schluessel (python tools\release_key.py init), gh angemeldet, alles committet und
# gepusht, Version in zeitspur/__init__.py erhoeht und ein Abschnitt "## <Version>" in CHANGELOG.md.
param([switch]$DryRun, [switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) { $gh = Join-Path $env:LOCALAPPDATA 'Programs\gh\bin\gh.exe' }
if (-not (Test-Path $gh)) { throw 'gh (GitHub CLI) nicht gefunden' }
$utf8 = [Text.UTF8Encoding]::new($false)

$version = "$(& $py -c 'import zeitspur; print(zeitspur.__version__)')".Trim()
$repo = "$(& $py -c 'from zeitspur import updater; print(updater.REPO)')".Trim()
$tag = "v$version"
$setupName = "ZeitspurSetup-$version.exe"
Write-Host "== Release $tag fuer $repo"

# Was ist neu? - aus CHANGELOG.md, fuer GitHub und fuer die App
$changelog = [IO.File]::ReadAllText((Join-Path $root 'CHANGELOG.md'), $utf8)
$m = [regex]::Match($changelog, "(?ms)^## $([regex]::Escape($version))[ \t]*\r?\n(.*?)(?=^## |\z)")
if (-not $m.Success -or -not $m.Groups[1].Value.Trim()) { throw "CHANGELOG.md hat keinen Abschnitt '## $version'" }
$changes = $m.Groups[1].Value.Trim()

if (-not $DryRun) {
    if (git status --porcelain) { throw 'Es gibt nicht committete Aenderungen - erst committen und pushen' }
    git fetch origin --quiet
    if ((git rev-parse HEAD) -ne (git rev-parse '@{u}')) { throw 'HEAD und origin unterscheiden sich - erst abgleichen' }
    if ((git remote get-url origin) -notmatch [regex]::Escape($repo)) { throw "origin ist nicht $repo" }
    & $gh release view $tag --repo $repo *> $null
    if ($LASTEXITCODE -eq 0) { throw "Release $tag gibt es schon - Version in zeitspur/__init__.py erhoehen" }
}

# Schalter als Hashtable uebergeben: Ein Array wie @('-Release') landete in build.ps1 still in $args - es wurde
# der normale Build gebaut und ein alter Release-Setup veroeffentlicht (beim ersten Release am 04.10.2026 bemerkt).
$buildStart = Get-Date
$buildArgs = @{ Release = $true }
if ($SkipTests) { $buildArgs['SkipTests'] = $true }
& (Join-Path $root 'build.ps1') @buildArgs

# Nur veroeffentlichen, was gerade eben als Release-Ausgabe dieser Version gebaut wurde
$built = Get-Item (Join-Path $root 'dist-release\ZeitspurSetup.exe') -ErrorAction SilentlyContinue
if (-not $built -or $built.LastWriteTime -lt $buildStart) { throw 'dist-release\ZeitspurSetup.exe wurde nicht neu gebaut' }
if ($built.VersionInfo.ProductVersion.Trim() -ne $version) { throw "Setup hat Version $($built.VersionInfo.ProductVersion), erwartet $version" }
$package = Join-Path $root 'dist-release\Zeitspur'
if (-not (Test-Path (Join-Path $package '_internal\zeitspur\release_channel.txt'))) { throw 'Keine Release-Ausgabe (Update-Kanal fehlt)' }
foreach ($teil in 'zeitspur\location.pyc', 'zeitspur\dawarich.pyc', 'zeitspur\windows_location.pyc', 'zeitspur\timeline_ui\vendor\leaflet.js') {
    if (-not (Test-Path (Join-Path $package "_internal\$teil"))) { throw "Release-Paket unvollstaendig: $teil fehlt" }
}

$out = Join-Path $root 'dist-release\publish'
Remove-Item $out -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $out | Out-Null
$setup = Join-Path $out $setupName
Copy-Item (Join-Path $root 'dist-release\ZeitspurSetup.exe') $setup
$notesFile = Join-Path $out 'changes.md'
[IO.File]::WriteAllText($notesFile, $changes, $utf8)
$manifest = Join-Path $out 'latest.json'

Write-Host '== latest.json signieren und wie ein Client pruefen'
& $py (Join-Path $root 'tools\release_key.py') manifest --setup $setup --version $version --notes-file $notesFile `
    --url "https://github.com/$repo/releases/download/$tag/$setupName" --out $manifest
if ($LASTEXITCODE) { throw 'Signieren fehlgeschlagen' }
& $py (Join-Path $root 'tools\release_key.py') verify --manifest $manifest --setup $setup
if ($LASTEXITCODE) { throw 'Pruefung wie ein Client fehlgeschlagen' }
$sha = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()

$fence = '```'
$body = @(
    'Zeitspur: verschlüsselter, lokaler Aktivitätsverlauf für Windows mit Zeitstrahl und MCP-Server für Claude.',
    '',
    '## Installation',
    '',
    "``$setupName`` herunterladen und ausführen. Keine Administratorrechte nötig, installiert nur für das eigene " +
    'Benutzerkonto. Eine vorhandene Installation wird aktualisiert; Datenbank, Schlüssel und Einstellungen bleiben ' +
    'erhalten. Danach hält sich Zeitspur selbst aktuell (abschaltbar unter Einstellungen → Updates).',
    '',
    '- Fehlt die Microsoft Edge WebView2-Runtime, lädt das Setup sie automatisch von Microsoft nach',
    '- Die EXE ist **nicht signiert** – Windows SmartScreen kann warnen („Weitere Informationen“ → „Trotzdem ausführen“)',
    '- Nach einem Update **Claude Desktop neu starten**, falls der Zeitspur-Connector dort genutzt wird',
    '',
    "## Neu in $version",
    '',
    $changes,
    '',
    '## Prüfsumme',
    '',
    "SHA-256 ``$setupName``:",
    $fence,
    $sha,
    $fence,
    '`latest.json` ist die signierte Update-Information, aus der sich installierte Versionen aktualisieren.'
) -join "`n"
$bodyFile = Join-Path $out 'release-notes.md'
[IO.File]::WriteAllText($bodyFile, $body, $utf8)

if ($DryRun) {
    Write-Host "== Probelauf fertig: $out (nichts hochgeladen)"
    exit 0
}

Write-Host "== GitHub-Release $tag anlegen"
& $gh release create $tag $setup $manifest --repo $repo --target (git rev-parse HEAD) --title "Zeitspur $version" `
    --notes-file $bodyFile --latest
if ($LASTEXITCODE) { throw 'gh release create fehlgeschlagen' }

$digest = (& $gh release view $tag --repo $repo --json assets --jq ".assets[] | select(.name == `"$setupName`") | .digest").Trim()
if ($digest -ne "sha256:$sha") { throw "GitHub meldet eine andere Pruefsumme fuer das Setup: $digest" }
Write-Host '== Kontrolle: oeffentlich abrufbar und gueltig, so wie ein installiertes Zeitspur es sieht'
& $py -c "from zeitspur import updater; p = updater.verify_manifest(updater.fetch_bytes(updater.MANIFEST_URL)); print('latest.json:', p['version']); assert p['version'] == '$version'"
if ($LASTEXITCODE) { throw 'latest.json ist (noch) nicht oeffentlich abrufbar oder ungueltig' }
Write-Host "== Fertig: https://github.com/$repo/releases/tag/$tag"
