# Installs Inno Setup 6 per-user (no admin rights) into %LOCALAPPDATA%\Programs\Inno Setup 6.
# Usage: pwsh -NoProfile -ExecutionPolicy Bypass -File tools\install_innosetup.ps1
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Version = '6.7.3'
$Urls = @("https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-$Version.exe", "https://files.jrsoftware.org/is/6/innosetup-$Version.exe")
$ExpectedSha256 = '9C73C3BAE7ED48D44112A0F48E66742C00090BDB5BEF71D9D3C056C66E97B732'
$Downloads = Join-Path $PSScriptRoot 'downloads'
New-Item -ItemType Directory -Force $Downloads | Out-Null
$Exe = Join-Path $Downloads "innosetup-$Version.exe"
$Target = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6'
$Iscc = Join-Path $Target 'ISCC.exe'

if (Test-Path $Iscc) {
    Write-Host "Inno Setup already installed: $Iscc"
    exit 0
}
if (-not (Test-Path $Exe)) {
    foreach ($u in $Urls) {
        try { Write-Host "Downloading $u"; Invoke-WebRequest -Uri $u -OutFile $Exe -MaximumRedirection 5; break }
        catch { Write-Warning "Download failed from $u : $($_.Exception.Message)" }
    }
    if (-not (Test-Path $Exe)) { throw 'Inno Setup download failed' }
}
$magic = [System.IO.File]::ReadAllBytes($Exe)[0..1]
if (-not ($magic[0] -eq 0x4D -and $magic[1] -eq 0x5A)) { Remove-Item $Exe; throw 'Downloaded file is not a Windows executable (MZ header missing) - probably an HTML page' }
$hash = (Get-FileHash $Exe -Algorithm SHA256).Hash
Write-Host "SHA256 innosetup-$Version.exe = $hash"
if ($ExpectedSha256 -and $hash -ne $ExpectedSha256) { throw 'SHA256 mismatch for Inno Setup installer' }

Write-Host "Installing per-user to $Target"
$args = @('/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', "/DIR=`"$Target`"")
$p = Start-Process -FilePath $Exe -ArgumentList $args -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "Inno Setup installer exit code $($p.ExitCode)" }
if (-not (Test-Path $Iscc)) { throw 'ISCC.exe not found after installation' }
Write-Host "OK: $Iscc"
