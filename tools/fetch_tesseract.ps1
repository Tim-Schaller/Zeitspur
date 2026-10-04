# Builds installer\tesseract-portable\ from the official Tesseract Windows installer (UB Mannheim build)
# WITHOUT admin rights: 7-Zip is obtained via an administrative MSI extraction (msiexec /a, no registry),
# the NSIS installer is unpacked with 7-Zip, and language data is fetched from tessdata_fast.
# Usage: pwsh -NoProfile -ExecutionPolicy Bypass -File tools\fetch_tesseract.ps1
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot
$Downloads = Join-Path $PSScriptRoot 'downloads'
$SevenZipDir = Join-Path $PSScriptRoot '7zip'
$Portable = Join-Path $Root 'installer\tesseract-portable'
New-Item -ItemType Directory -Force $Downloads | Out-Null

$TessVersion = '5.5.3.20260724'
$TessSetupName = "tesseract-ocr-w64-setup-$TessVersion.exe"
$TessUrl = "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/$TessSetupName"
$SevenZipMsiName = '7z2301-x64.msi'
$SevenZipMsiUrl = "https://www.7-zip.org/a/$SevenZipMsiName"
$TessdataUrl = 'https://github.com/tesseract-ocr/tessdata_fast/raw/main'
$Languages = @('eng', 'deu')
# SHA256 pins (verified 2026-09-10); empty string = accept and print
$Pins = @{
    $TessSetupName    = 'BEE9E3434BD94FD65387D9BE28CD467A41F61B1275383B55B0F59A1331270AE4'
    $SevenZipMsiName  = '0BA639B6DACDF573D847C911BD147C6384381A54DAC082B1E8C77BC73D58958B'
    'eng.traineddata' = '7D4322BD2A7749724879683FC3912CB542F19906C83BCC1A52132556427170B2'
    'deu.traineddata' = '19D219BBB6672C869D20A9636C6816A81EB9A71796CB93EBE0CB1530E2CDB22D'
}

function Get-Download([string]$Url, [string]$Name) {
    $path = Join-Path $Downloads $Name
    if (-not (Test-Path $path)) {
        Write-Host "Downloading $Url"
        Invoke-WebRequest -Uri $Url -OutFile $path -MaximumRedirection 5
    }
    $hash = (Get-FileHash $path -Algorithm SHA256).Hash
    $expected = $Pins[$Name]
    if ($expected) {
        if ($hash -ne $expected) { throw "SHA256 mismatch for $Name : $hash (expected $expected)" }
    } else {
        Write-Host "  SHA256 $Name = $hash"
    }
    return $path
}

# 1) 7-Zip (7z.exe + 7z.dll) via administrative MSI extraction
$SevenZip = Get-ChildItem -Path $SevenZipDir -Recurse -Filter 7z.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $SevenZip) {
    $msi = Get-Download $SevenZipMsiUrl $SevenZipMsiName
    New-Item -ItemType Directory -Force $SevenZipDir | Out-Null
    $p = Start-Process -FilePath msiexec.exe -ArgumentList @('/a', "`"$msi`"", '/qn', "TARGETDIR=`"$SevenZipDir`"") -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "msiexec /a failed with exit code $($p.ExitCode)" }
    $SevenZip = Get-ChildItem -Path $SevenZipDir -Recurse -Filter 7z.exe | Select-Object -First 1
    if (-not $SevenZip) { throw '7z.exe not found after MSI extraction' }
}
Write-Host "7-Zip: $($SevenZip.FullName)"

# 2) Tesseract installer -> unpack
$setup = Get-Download $TessUrl $TessSetupName
$extract = Join-Path $Downloads 'tesseract-extract'
if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
& $SevenZip.FullName x "$setup" "-o$extract" -y | Out-Null
if ($LASTEXITCODE -ne 0) { throw "7z extraction failed with exit code $LASTEXITCODE" }
$exe = Get-ChildItem -Path $extract -Recurse -Filter tesseract.exe | Select-Object -First 1
if (-not $exe) { throw 'tesseract.exe not found in extracted installer' }
$src = $exe.DirectoryName
Write-Host "Extracted to $src"

# 3) Assemble the portable folder (binary, DLLs, configs, license)
if (Test-Path $Portable) { Remove-Item -Recurse -Force $Portable }
New-Item -ItemType Directory -Force (Join-Path $Portable 'tessdata') | Out-Null
New-Item -ItemType Directory -Force (Join-Path $Portable 'doc') | Out-Null
Copy-Item $exe.FullName $Portable
Get-ChildItem -Path $src -Filter *.dll | Copy-Item -Destination $Portable
foreach ($sub in @('tessdata\configs', 'tessdata\tessconfigs')) {
    $s = Join-Path $src $sub
    if (Test-Path $s) { Copy-Item -Recurse -Force $s (Join-Path $Portable $sub) }
}
$pdfttf = Join-Path $src 'tessdata\pdf.ttf'
if (Test-Path $pdfttf) { Copy-Item $pdfttf (Join-Path $Portable 'tessdata') }
foreach ($d in @('LICENSE', 'AUTHORS', 'README.md')) {
    $f = Join-Path $src "doc\$d"
    if (Test-Path $f) { Copy-Item $f (Join-Path $Portable 'doc') }
}

# 4) Language data (tessdata_fast) - not contained in the installer
foreach ($lang in $Languages) {
    $f = Get-Download "$TessdataUrl/$lang.traineddata" "$lang.traineddata"
    Copy-Item $f (Join-Path $Portable "tessdata\$lang.traineddata")
}
Remove-Item -Recurse -Force $extract

# 5) Smoke test
$tess = Join-Path $Portable 'tesseract.exe'
& $tess --version 2>&1 | Select-Object -First 2
& $tess --list-langs --tessdata-dir (Join-Path $Portable 'tessdata') 2>&1
$size = (Get-ChildItem $Portable -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Portable bundle: {0} files, {1:N1} MB" -f (Get-ChildItem $Portable -Recurse -File).Count, $size)

# 6) Optional: remove DLLs that tesseract.exe does not import (training-tool libraries)
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $venvPy) { & $venvPy (Join-Path $PSScriptRoot 'trim_tesseract.py') }
