$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonCommand = Get-Command py -ErrorAction SilentlyContinue
$Python = if ($PythonCommand) { $PythonCommand.Source } else { (Get-Command python -ErrorAction Stop).Source }
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$UpstreamDir = Join-Path $ProjectDir "integrations\wechatauto_readonly\source"
$PinnedCommit = (Get-Content (Join-Path $ProjectDir "integrations\wechatauto_readonly\PINNED_COMMIT.txt") -Raw).Trim()

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if ($PythonCommand) {
        & $Python -3.12 -m venv (Join-Path $ProjectDir ".venv")
    } else {
        & $Python -m venv (Join-Path $ProjectDir ".venv")
    }
}

& $VenvPython -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
& $VenvPython -m pip install -r (Join-Path $ProjectDir "requirements.txt") -i https://pypi.tuna.tsinghua.edu.cn/simple

if (-not (Test-Path -LiteralPath (Join-Path $UpstreamDir "wechatauto\db.py"))) {
    $Git = (Get-Command git -ErrorAction Stop).Source
    & $Git clone --filter=blob:none --no-checkout https://github.com/fanyuantaier/wechatauto-replica.git $UpstreamDir
    & $Git -C $UpstreamDir checkout $PinnedCommit
}

Write-Host "依赖安装完成。" -ForegroundColor Green
if (-not [Environment]::GetEnvironmentVariable("TYPESAFE_API_KEY", "User")) {
    Write-Warning "用户环境变量 TYPESAFE_API_KEY 不存在，启动后将无法调用 TypeSafe。"
}
Write-Host "运行方式：双击 0921_启动微信Jev助手_v1.cmd" -ForegroundColor Cyan
