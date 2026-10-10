$ErrorActionPreference = 'Stop'

if (-not $env:CONDA_PREFIX) {
    throw 'No active Conda environment. Run: conda activate pdf-batch-ocr'
}

$source = Join-Path $env:CONDA_PREFIX 'Library\share\tessdata\configs'
$targetRoot = Join-Path $env:CONDA_PREFIX 'share\tessdata'
$target = Join-Path $targetRoot 'configs'

if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "Tesseract config directory not found: $source"
}

New-Item -ItemType Directory -Force -Path $target | Out-Null
Get-ChildItem -LiteralPath $source -File | Copy-Item -Destination $target -Force

$required = @('hocr', 'txt')
foreach ($name in $required) {
    $path = Join-Path $target $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required Tesseract config was not installed: $path"
    }
}

Write-Host "Tesseract configs are ready in: $target"
