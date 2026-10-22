param(
    [int]$ParentPid,
    [string]$ZipPath,
    [string]$AppDir,
    [string]$ExeName,
    [string]$Preserve = "",
    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"
if (-not $LogPath) {
    $LogPath = Join-Path $AppDir "update.log"
}

function Write-UpdateLog([string]$Message) {
    try {
        $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " " + $Message
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch {
    }
}

$extract = $null
$preserveFiles = @(
    "edu\config.json",
    "edu\*.json",
    "edu\runner.pid",
    "syjc\config.json",
    "syjc\*.json",
    "syjc\runner.pid"
)
$preserveDirs = @(
    "edu\logs",
    "edu\browser_profile",
    "syjc\logs",
    "syjc\browser_profile"
)

function Test-PreservedPath([string]$RelativePath) {
    $relative = $RelativePath.Replace("/", "\").TrimStart("\")
    foreach ($pattern in $preserveFiles) {
        if ($relative -like $pattern) {
            return $true
        }
    }
    foreach ($directory in $preserveDirs) {
        if ($relative -eq $directory -or $relative.StartsWith($directory + "\")) {
            return $true
        }
    }
    return $false
}

function Copy-UpdateTree([string]$SourceRoot, [string]$Source, [string]$Target) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        $relative = $item.FullName.Substring($SourceRoot.Length).TrimStart("\")
        if (Test-PreservedPath $relative) {
            continue
        }
        $destination = Join-Path $Target $item.Name
        if ($item.PSIsContainer) {
            Copy-UpdateTree $SourceRoot $item.FullName $destination
        } else {
            $parent = Split-Path -Parent $destination
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $destination -Force
        }
    }
}

try {
    Write-UpdateLog "update runner started, waiting for the old process to exit"
    $deadline = (Get-Date).AddMinutes(5)
    while (
        (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) -and
        (Get-Date) -lt $deadline
    ) {
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Milliseconds 1500

    $extract = Join-Path $env:TEMP ("app_update_" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $extract | Out-Null
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $extract)

    $exe = Get-ChildItem -LiteralPath $extract -Filter $ExeName -Recurse |
        Select-Object -First 1
    if ($exe) {
        # 整包：从启动文件所在目录开始覆盖
        $sourceDir = $exe.Directory.FullName
    } else {
        # 增量包：只含改动的文件，直接从解压根目录覆盖
        $sourceDir = $extract
    }
    Copy-UpdateTree $sourceDir $sourceDir $AppDir

    Write-UpdateLog "update completed, starting the new version"
    Start-Process -FilePath (Join-Path $AppDir $ExeName) `
        -WorkingDirectory $AppDir
} catch {
    Write-UpdateLog ("update failed: " + $_.Exception.Message)
} finally {
    if ($extract -and (Test-Path -LiteralPath $extract)) {
        Remove-Item -LiteralPath $extract -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
    }
}
