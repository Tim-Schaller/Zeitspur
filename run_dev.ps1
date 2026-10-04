# Startet Zeitspur aus dem Quelltext (Entwicklung).
#   .\run_dev.ps1                 -> normaler Start (Tray, Fenster versteckt bzw. Ersteinrichtung)
#   .\run_dev.ps1 -Show -Debug    -> Fenster sofort anzeigen, Debug-Logging + DevTools
#   .\run_dev.ps1 -DataDir "$env:TEMP\ZeitspurDemo"   -> separater Datenordner (z. B. mit tools\seed_demo_db.py gefuellt)
param(
    [switch]$Show,
    [switch]$Debug,
    [switch]$Quit,
    [string]$DataDir
)
$ErrorActionPreference = 'Stop'
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "venv fehlt: python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt" }
if ($DataDir) { $env:ZEITSPUR_DATA_DIR = $DataDir }
$args = @('-m', 'zeitspur.service_main')
if ($Show) { $args += '--show' }
if ($Debug) { $args += '--debug' }
if ($Quit) { $args += '--quit' }
& $py @args
