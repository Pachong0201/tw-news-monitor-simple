param(
    [string]$OutputDir = "dist\tainan-assessment-deployment",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $BundleRoot = $OutputDir
} else {
    $BundleRoot = Join-Path $ProjectDir $OutputDir
}

if (Test-Path $BundleRoot) {
    if (-not $Force) {
        throw "输出目录已存在：$BundleRoot；如需重建请使用 -Force"
    }
    $resolved = [System.IO.Path]::GetFullPath($BundleRoot)
    $forbidden = @([System.IO.Path]::GetPathRoot($resolved))
    foreach ($candidate in @($env:USERPROFILE, $env:TEMP, $env:TMP, $env:WINDIR)) {
        if ($candidate) { $forbidden += [System.IO.Path]::GetFullPath($candidate) }
    }
    if ($forbidden -contains $resolved) {
        throw "拒绝删除高风险目录：$resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $BundleRoot | Out-Null

function Copy-Tree($Source, $Dest) {
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Copy-Item -Path (Join-Path $Source "*") -Destination $Dest -Recurse -Force
}

Copy-Tree (Join-Path $ProjectDir "app") (Join-Path $BundleRoot "app")
Copy-Tree (Join-Path $ProjectDir "scripts") (Join-Path $BundleRoot "scripts")

# 只复制正式种子必需文件 + 唯一 ready 覆盖目录（不包含历史备份/preview/tmp）
$SeedSrc = Join-Path $ProjectDir "data\election_seed\tainan_2026"
$SeedDst = Join-Path $BundleRoot "data\election_seed\tainan_2026"
New-Item -ItemType Directory -Force -Path $SeedDst | Out-Null
foreach ($name in @(
    "events.jsonl",
    "sources.jsonl",
    "polls.jsonl",
    "initial_snapshot.json",
    "snapshot_history.jsonl",
    "election.json",
    "poll_sources.jsonl",
    "poll_source_links.jsonl",
    "poll_question_comparability.jsonl",
    "poll_import_plan.json",
    "poll_post_release_reconciliation.json",
    "poll_release_acceptance_rules.yaml",
    "poll_schema.json"
)) {
    $src = Join-Path $SeedSrc $name
    if (Test-Path $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $SeedDst $name) -Force
    }
}
$coverage = Get-ChildItem -Path $SeedSrc -Directory -Filter "fact_coverage_*" |
    Where-Object {
        (Test-Path (Join-Path $_.FullName "coverage_preflight.json")) -and
        (Test-Path (Join-Path $_.FullName "coverage_validation.json"))
    } |
    Sort-Object Name |
    Select-Object -Last 1
if (-not $coverage) { throw "未找到 ready 覆盖目录" }
Copy-Tree $coverage.FullName (Join-Path $SeedDst $coverage.Name)

foreach ($rel in @(
    "config\election_assessment.yaml",
    "config\llm_pricing.yaml",
    "config\election_assessment_deployment.example.yaml",
    "config\feishu_delivery.example.yaml",
    "requirements.txt",
    "README_DEPLOYMENT.md",
    "VERSION",
    "data\election_context.db"
)) {
    $src = Join-Path $ProjectDir $rel
    if (-not (Test-Path $src)) { throw "缺失文件：$rel" }
    $dest = Join-Path $BundleRoot $rel
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
    Copy-Item -LiteralPath $src -Destination $dest -Force
}

# 清理缓存与临时文件
foreach ($filter in @("__pycache__", ".pytest_cache", ".git")) {
    $dirs = Get-ChildItem -Path $BundleRoot -Recurse -Directory -Filter $filter -ErrorAction SilentlyContinue
    if ($dirs) { $dirs | Remove-Item -Recurse -Force }
}
$files = Get-ChildItem -Path $BundleRoot -Recurse -File -Include "*.pyc", "*.log", "*.tmp" -ErrorAction SilentlyContinue
if ($files) { $files | Remove-Item -Force }

# MANIFEST + SHA256SUMS
$files = Get-ChildItem -Path $BundleRoot -Recurse -File
$manifest = [ordered]@{
    bundle_name = "tainan-assessment-deployment"
    version = (Get-Content (Join-Path $BundleRoot "VERSION") -Encoding UTF8 | Select-Object -First 1).Trim()
    created_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    file_count = $files.Count
    files = [ordered]@{}
}
$shaLines = @()
foreach ($file in $files) {
    $rel = $file.FullName.Substring($BundleRoot.Length).TrimStart("\", "/").Replace("\", "/")
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest.files[$rel] = $hash
    $shaLines += "$hash *$rel"
}
$manifestJson = $manifest | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText((Join-Path $BundleRoot "MANIFEST.json"), $manifestJson, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllLines((Join-Path $BundleRoot "SHA256SUMS"), $shaLines, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "部署包已生成：$BundleRoot"
Write-Host "文件数：$($files.Count)"
Write-Host "验证命令：scripts\validate_tainan_assessment_deployment.ps1 -BundleDir `"$OutputDir`""
