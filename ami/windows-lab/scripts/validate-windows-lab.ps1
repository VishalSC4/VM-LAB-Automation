param(
    [string]$LabRoot = "C:\LabFiles",
    [switch]$IncludePowerBI
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$ResultDir = "C:\ProgramData\UNext"
$ResultFile = Join-Path $ResultDir "golden-ami-validation.json"
$FailedFile = Join-Path $ResultDir "GOLDEN_AMI_FAILED.txt"
$ReadyFile = Join-Path $ResultDir "GOLDEN_AMI_READY.txt"
New-Item -ItemType Directory -Force -Path $ResultDir | Out-Null

$results = New-Object System.Collections.Generic.List[object]

function Add-Check {
    param(
        [string]$Name,
        [bool]$Passed,
        [string]$Detail = ""
    )
    $results.Add([pscustomobject]@{
        name = $Name
        passed = $Passed
        detail = $Detail
    }) | Out-Null
    if ($Passed) {
        Write-Host "PASS: $Name $Detail"
    } else {
        Write-Host "FAIL: $Name $Detail"
    }
}

function Test-Command {
    param(
        [string]$Name,
        [string]$Command
    )
    try {
        $output = cmd.exe /c "$Command 2>&1"
        Add-Check $Name ($LASTEXITCODE -eq 0) (($output | Select-Object -First 2) -join " ")
    } catch {
        Add-Check $Name $false $_.Exception.Message
    }
}

function Test-PathExists {
    param(
        [string]$Name,
        [string[]]$Paths
    )
    $match = $Paths | Where-Object { Test-Path $_ } | Select-Object -First 1
    Add-Check $Name ([bool]$match) ($match ?? "not found")
}

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$env:Path = "$machinePath;$([Environment]::GetEnvironmentVariable("Path", "User"))"

Test-PathExists "Chrome executable" @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)
Test-PathExists "VS Code executable" @(
    "C:\Program Files\Microsoft VS Code\Code.exe",
    "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe"
)

Test-Command "Chrome CLI" "chrome --version"
Test-Command "VS Code CLI" "code --version"
Test-Command "Python 3" "python --version"
Test-Command "pip" "pip --version"
Test-Command "Node.js" "node --version"
Test-Command "npm" "npm --version"
Test-Command "Vite" "vite --version"
Test-Command "Create React App" "create-react-app --version"
Test-Command "MongoDB shell" "mongosh --version"

if ($IncludePowerBI) {
    Test-PathExists "Power BI Desktop executable" @(
        "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe"
    )
    Test-PathExists "Power BI Desktop model engine" @(
        "C:\Program Files\Microsoft Power BI Desktop\bin\msmdsrv.exe"
    )
    Test-PathExists "Microsoft Edge WebView2 runtime" @(
        "C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
        "C:\Program Files\Microsoft\EdgeWebView\Application"
    )
    $powerBiHome = [Environment]::GetEnvironmentVariable("POWERBI_DESKTOP_HOME", "Machine")
    Add-Check "POWERBI_DESKTOP_HOME environment variable" ($powerBiHome -eq "C:\Program Files\Microsoft Power BI Desktop") $powerBiHome
    Add-Check "Power BI bin in machine PATH" ($machinePath -like "*C:\Program Files\Microsoft Power BI Desktop\bin*") "machine PATH"
    try {
        $powerBiExe = "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe"
        $version = (Get-Item $powerBiExe).VersionInfo.ProductVersion
        Add-Check "Power BI Desktop version readable" ([bool]$version) $version
    } catch {
        Add-Check "Power BI Desktop version readable" $false $_.Exception.Message
    }
}

try {
    $service = Get-Service MongoDB -ErrorAction Stop
    if ($service.Status -ne "Running") {
        Start-Service MongoDB -ErrorAction Stop
        $service = Get-Service MongoDB
    }
    Add-Check "MongoDB service running" ($service.Status -eq "Running") $service.Status
} catch {
    Add-Check "MongoDB service running" $false $_.Exception.Message
}

try {
    $mongoOutput = mongosh --quiet --eval "db.runCommand({ ping: 1 }).ok" 2>&1
    Add-Check "MongoDB ping" ($LASTEXITCODE -eq 0 -and ($mongoOutput -join " ") -match "1") (($mongoOutput | Select-Object -First 2) -join " ")
} catch {
    Add-Check "MongoDB ping" $false $_.Exception.Message
}

try {
    $reactDir = Join-Path $LabRoot "React\sample-react-app"
    if (Test-Path (Join-Path $reactDir "package.json")) {
        Push-Location $reactDir
        npm run build -- --emptyOutDir 2>&1 | Out-String | Out-Null
        Add-Check "React sample build" ($LASTEXITCODE -eq 0) $reactDir
        Pop-Location
    } else {
        Add-Check "React sample build" $false "package.json missing"
    }
} catch {
    Add-Check "React sample build" $false $_.Exception.Message
    try { Pop-Location } catch {}
}

try {
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.google.com/generate_204" -TimeoutSec 20 | Out-Null
    Add-Check "Internet connectivity" $true "google.com"
} catch {
    Add-Check "Internet connectivity" $false $_.Exception.Message
}

try {
    Invoke-WebRequest -UseBasicParsing -Uri "https://colab.research.google.com/" -TimeoutSec 30 | Out-Null
    Add-Check "Google Colab accessibility" $true "colab.research.google.com"
} catch {
    Add-Check "Google Colab accessibility" $false $_.Exception.Message
}

$desktop = "C:\Users\Public\Desktop"
@(
    "Google Chrome.lnk",
    "Google Colab.lnk",
    "Visual Studio Code.lnk",
    "Node.js Terminal.lnk",
    "npm Terminal.lnk",
    "Python 3 Shell.lnk",
    "pip Terminal.lnk",
    "React Sample App.lnk",
    "React Developer Terminal.lnk",
    "MongoDB Shell.lnk",
    "MongoDB Service Status.lnk",
    $(if ($IncludePowerBI) { "Power BI Desktop.lnk" }),
    "Lab Files.lnk"
) | Where-Object { $_ } | ForEach-Object {
    Add-Check "Desktop shortcut $_" (Test-Path (Join-Path $desktop $_)) $_
}

Add-Check "No Eclipse executable" (-not (Get-Command eclipse.exe -ErrorAction SilentlyContinue)) "eclipse.exe"
Add-Check "No Jupyter executable" (-not (Get-Command jupyter.exe -ErrorAction SilentlyContinue)) "jupyter.exe"

$failed = @($results | Where-Object { -not $_.passed })
$results | ConvertTo-Json -Depth 5 | Set-Content -Path $ResultFile -Encoding UTF8

Remove-Item -Force -ErrorAction SilentlyContinue $ReadyFile, $FailedFile
if ($failed.Count -gt 0) {
    $failed | ForEach-Object { "$($_.name): $($_.detail)" } | Set-Content -Path $FailedFile -Encoding UTF8
    Write-Host "UNEXT_GOLDEN_AMI_FAILED: $($failed.Count) validation check(s) failed"
    exit 1
}

New-Item -ItemType File -Force -Path $ReadyFile | Out-Null
Write-Host "UNEXT_GOLDEN_AMI_READY"
exit 0
