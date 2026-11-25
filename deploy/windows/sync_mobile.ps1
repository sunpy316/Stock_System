<#
.SYNOPSIS
Sync web UI into mobile/ & stocksystem_android/, rebuild local Android APK,
and download the latest iOS IPA built by GitHub Actions.
#>
param(
    [string]$RunId = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = "D:\Stocks\Antigravity\stocksystem"
$AndroidWork = "D:\Stocks\Antigravity\stocksystem_android"
$OutDir = "D:\Stocks\Antigravity"

# 1. Sync web/index.html -> mobile/www & stocksystem_android/www
$srcHtml = Join-Path $RepoRoot "stock_system\web\index.html"
$content = (Get-Content -Path $srcHtml -Raw -Encoding UTF8).Replace("const API = '';", "const API = 'http://47.104.78.144';")
Set-Content -Path (Join-Path $RepoRoot "mobile\www\index.html") -Value $content -Encoding UTF8
Set-Content -Path (Join-Path $AndroidWork "www\index.html") -Value $content -Encoding UTF8

# 2. Rebuild Android APK locally
$env:JAVA_HOME = "D:\Android\jdk17"
$env:ANDROID_HOME = "D:\Android\Sdk"
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
Push-Location $AndroidWork
npx cap copy android
Push-Location (Join-Path $AndroidWork "android")
.\gradlew.bat assembleDebug --no-daemon
Pop-Location
Pop-Location
Copy-Item -Force (Join-Path $AndroidWork "android\app\build\outputs\apk\debug\app-debug.apk") (Join-Path $OutDir "Stock_System_Android.apk")

# 3. Download latest iOS IPA artifact via nightly.link if RunId is specified or fetched from API
if (-not $RunId) {
    $runs = Invoke-RestMethod -Uri "https://api.github.com/repos/sunpy316/Stock_System/actions/runs?per_page=1" -Headers @{"User-Agent"="StockSystemCI"}
    $latest = $runs.workflow_runs | Select-Object -First 1
    if ($latest -and $latest.status -eq "completed" -and $latest.conclusion -eq "success") {
        $RunId = $latest.id
    }
}

if ($RunId) {
    $zipPath = Join-Path $OutDir "Stock_System_iOS_IPA.zip"
    Invoke-WebRequest -Uri "https://nightly.link/sunpy316/Stock_System/actions/runs/$RunId/Stock_System_iOS_IPA.zip" -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $OutDir -Force
    Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
    Write-Host "Updated Android APK & iOS IPA (Run $RunId) in $OutDir"
} else {
    Write-Host "Android APK updated. iOS IPA build still in progress or unavailable via unauthenticated API."
}
