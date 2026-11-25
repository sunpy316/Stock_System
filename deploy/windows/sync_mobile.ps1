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
$MobileDir = Join-Path $RepoRoot "mobile"
$OutDir = "D:\Stocks\Antigravity"

# 1. Sync web/index.html -> mobile/www
$srcHtml = Join-Path $RepoRoot "stock_system\web\index.html"
$content = (Get-Content -Path $srcHtml -Raw -Encoding UTF8).Replace("const API = '';", "const API = 'http://47.104.78.144';")
Set-Content -Path (Join-Path $MobileDir "www\index.html") -Value $content -Encoding UTF8

# 2. Rebuild Android APK locally inside stocksystem/mobile
$env:JAVA_HOME = "D:\Android\jdk17"
$env:ANDROID_HOME = "D:\Android\Sdk"
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
Push-Location $MobileDir
npx cap copy android
Push-Location (Join-Path $MobileDir "android")
.\gradlew.bat assembleDebug --no-daemon
Pop-Location
Pop-Location
Copy-Item -Force (Join-Path $MobileDir "android\app\build\outputs\apk\debug\app-debug.apk") (Join-Path $OutDir "Stock_System_Android.apk")

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
    Get-ChildItem (Join-Path $OutDir "Stock_System_iOS_*.ipa") | ForEach-Object { $_.LastWriteTime = Get-Date }
    Write-Host "Updated Android APK & iOS IPA (Run $RunId) in $OutDir"
} else {
    Write-Host "Android APK updated. iOS IPA build still in progress or unavailable via unauthenticated API."
}
