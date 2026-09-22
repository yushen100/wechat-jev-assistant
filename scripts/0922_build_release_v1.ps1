param([string]$Version = "0.3.0")
$ErrorActionPreference = "Stop"
if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?$') { throw "版本格式无效" }
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DistDir = Join-Path $ProjectDir "dist"
$StageDir = Join-Path $DistDir "wechat-jev-assistant-$Version"
$ZipPath = Join-Path $DistDir "wechat-jev-assistant-$Version-windows.zip"
if ((Test-Path -LiteralPath $StageDir) -or (Test-Path -LiteralPath $ZipPath)) {
    throw "该版本构建产物已存在，请先确认后另行清理"
}
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
# 仅打包 Git 跟踪且通过审查的文件，避免复制本机数据后再删除。
$Files = git -C $ProjectDir -c core.quotepath=false ls-files
if ($LASTEXITCODE -ne 0) { throw "无法读取发布文件清单" }
foreach ($RelativePath in $Files) {
    if ($RelativePath -match '^(?:assets|content|src|tests)/' -or
        $RelativePath -match '^integrations/wechatauto_readonly/(?:PINNED_COMMIT.txt|README.md|readonly_bridge.py)$' -or
        $RelativePath -match '^(?:README.md|requirements.txt|setup.ps1|main.pyw|\.gitignore|0921_.*\.(?:cmd|md))$') {
        $Destination = Join-Path $StageDir $RelativePath
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Copy-Item -LiteralPath (Join-Path $ProjectDir $RelativePath) -Destination $Destination
    }
}
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $ZipPath -CompressionLevel Optimal
$Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$ZipPath.sha256" -Value "$Hash  $(Split-Path -Leaf $ZipPath)" -Encoding utf8NoBOM
Write-Host "已生成 v$Version Windows 包及 SHA-256"
