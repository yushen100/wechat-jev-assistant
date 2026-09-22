param(
    [Parameter(Mandatory = $false)]
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DistDir = Join-Path $ProjectDir "dist"
$StageDir = Join-Path $DistDir "wechat-jev-assistant-$Version"
$ZipPath = Join-Path $DistDir "wechat-jev-assistant-$Version-windows.zip"
$ChecksumPath = "$ZipPath.sha256"

if (Test-Path -LiteralPath $StageDir) {
    Remove-Item -LiteralPath $StageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

$IncludeFiles = @(
    ".gitignore",
    "README.md",
    "requirements.txt",
    "setup.ps1",
    "main.pyw",
    "0921_启动微信Jev助手_v1.cmd",
    "0921_运行测试_v1.cmd",
    "0921_使用说明_v1.md"
)
$IncludeDirs = @("assets", "content", "src", "tests", "integrations\wechatauto_readonly")

foreach ($RelativePath in $IncludeFiles) {
    Copy-Item -LiteralPath (Join-Path $ProjectDir $RelativePath) -Destination $StageDir
}
foreach ($RelativePath in $IncludeDirs) {
    $Source = Join-Path $ProjectDir $RelativePath
    $Destination = Join-Path $StageDir $RelativePath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse
}

$Forbidden = @(
    "data",
    "logs",
    ".venv",
    "runtime",
    "integrations\wechatauto_readonly\source",
    "integrations\wechatauto_readonly\runtime"
)
foreach ($RelativePath in $Forbidden) {
    $Candidate = Join-Path $StageDir $RelativePath
    if (Test-Path -LiteralPath $Candidate) {
        Remove-Item -LiteralPath $Candidate -Recurse -Force
    }
}

Get-ChildItem -LiteralPath $StageDir -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $StageDir -Recurse -File -Include "*.pyc", "*.pyo" |
    Remove-Item -Force

if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $ZipPath -CompressionLevel Optimal

$Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $ChecksumPath -Value "$Hash  $(Split-Path -Leaf $ZipPath)" -Encoding utf8NoBOM

Write-Host "已生成：$ZipPath" -ForegroundColor Green
Write-Host "校验值：$ChecksumPath" -ForegroundColor Green
