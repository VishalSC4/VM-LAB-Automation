param(
    [string]$LabRoot = "C:\LabFiles",
    [string]$ChatGptPackageId = "9NT1R1C2HH7J",
    [string]$ClaudeMsixUrl = "https://claude.ai/api/desktop/win32/x64/msix/latest/redirect"
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$LogDir = "C:\ProgramData\CloudLab"
$PublicDesktop = "C:\Users\Public\Desktop"
$BinDir = "C:\ProgramData\chocolatey\bin"
New-Item -ItemType Directory -Force -Path $LogDir,$PublicDesktop,$LabRoot,$BinDir | Out-Null
Start-Transcript -Path (Join-Path $LogDir "DesktopAppsRepair.log") -Append

function Write-Step([string]$Message) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format o), $Message)
}

function Add-MachinePath([string]$PathItem) {
    if (-not $PathItem -or -not (Test-Path $PathItem)) { return }
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($machinePath -notlike "*$PathItem*") {
        [Environment]::SetEnvironmentVariable("Path", "$machinePath;$PathItem", "Machine")
    }
}

function First-Path([string[]]$Paths) {
    return $Paths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function New-DesktopShortcut(
    [string]$Name,
    [string]$Target,
    [string]$Arguments = "",
    [string]$WorkingDirectory = "",
    [string]$IconLocation = ""
) {
    if (-not $Target) { return }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $PublicDesktop $Name))
    $shortcut.TargetPath = $Target
    if ($Arguments) { $shortcut.Arguments = $Arguments }
    if ($WorkingDirectory) { $shortcut.WorkingDirectory = $WorkingDirectory }
    if ($IconLocation) { $shortcut.IconLocation = $IconLocation }
    $shortcut.Save()
}

function Ensure-Chocolatey {
    if (Get-Command choco.exe -ErrorAction SilentlyContinue) { return $true }
    Write-Step "Installing Chocolatey"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
        Invoke-Expression ((New-Object Net.WebClient).DownloadString("https://community.chocolatey.org/install.ps1"))
        Add-MachinePath "C:\ProgramData\chocolatey\bin"
        $env:Path = "$([Environment]::GetEnvironmentVariable("Path", "Machine"));$([Environment]::GetEnvironmentVariable("Path", "User"))"
    } catch {
        Write-Step "Chocolatey install failed: $($_.Exception.Message)"
    }
    return [bool](Get-Command choco.exe -ErrorAction SilentlyContinue)
}

function Ensure-Winget {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) { return $winget.Source }

    $knownWinget = Get-ChildItem "C:\Program Files\WindowsApps" -Recurse -Filter winget.exe -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($knownWinget) {
        Add-MachinePath (Split-Path $knownWinget.FullName)
        $env:Path = "$([Environment]::GetEnvironmentVariable("Path", "Machine"));$([Environment]::GetEnvironmentVariable("Path", "User"))"
        return $knownWinget.FullName
    }

    Write-Step "Installing App Installer for winget"
    $installer = Join-Path $env:TEMP "Microsoft.DesktopAppInstaller.msixbundle"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "https://aka.ms/getwinget" -OutFile $installer -TimeoutSec 180
        Add-AppxPackage -Path $installer -ErrorAction Stop
    } catch {
        Write-Step "winget install failed: $($_.Exception.Message)"
    }

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) { return $winget.Source }
    $knownWinget = Get-ChildItem "C:\Program Files\WindowsApps" -Recurse -Filter winget.exe -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($knownWinget) { return $knownWinget.FullName }
    return $null
}

function Install-Java {
    Write-Step "Installing Java 21 JDK and configuring JAVA_HOME"
    if (Ensure-Chocolatey) {
        choco install temurin21 --yes --no-progress --limit-output
        if ($LASTEXITCODE -ne 0) {
            choco install microsoft-openjdk21 --yes --no-progress --limit-output
        }
    }

    $jdkHome = Get-ChildItem -Directory "C:\Program Files\Eclipse Adoptium","C:\Program Files\Microsoft" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "jdk-?21|jdk.*21|OpenJDK.*21" } |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if (-not $jdkHome) {
        $jdkHome = Get-ChildItem -Directory "C:\Program Files\Java","C:\Program Files" -ErrorAction SilentlyContinue |
            Where-Object { Test-Path (Join-Path $_.FullName "bin\java.exe") } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
    }
    if ($jdkHome) {
        [Environment]::SetEnvironmentVariable("JAVA_HOME", $jdkHome.FullName, "Machine")
        Add-MachinePath (Join-Path $jdkHome.FullName "bin")
        $env:JAVA_HOME = $jdkHome.FullName
        $env:Path = "$([Environment]::GetEnvironmentVariable("Path", "Machine"));$([Environment]::GetEnvironmentVariable("Path", "User"))"
    }
}

function Install-ChatGPT {
    Write-Step "Installing ChatGPT Windows app"
    $winget = Ensure-Winget
    if ($winget) {
        & $winget install --id=$ChatGptPackageId --source=msstore --accept-package-agreements --accept-source-agreements --silent
    }
}

function Install-ClaudeDesktop {
    Write-Step "Refreshing Claude Desktop provisioned package"
    $known = First-Path @(
        "C:\Users\Administrator\AppData\Local\Programs\Claude\Claude.exe",
        "C:\Program Files\Claude\Claude.exe",
        "C:\Program Files\AnthropicClaude\Claude.exe"
    )
    $packaged = Get-ChildItem "C:\Program Files\WindowsApps" -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($known -or $packaged) { return }

    $msix = Join-Path $env:TEMP "Claude.msix"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $ClaudeMsixUrl -MaximumRedirection 10 -OutFile $msix -TimeoutSec 180
        $dism = Join-Path $env:SystemRoot "System32\dism.exe"
        & $dism /Online /Add-ProvisionedAppxPackage /PackagePath:$msix /SkipLicense /Region:all | Out-Null
    } catch {
        Write-Step "Claude Desktop refresh failed: $($_.Exception.Message)"
    }
}

function Get-StartAppId([string]$Pattern) {
    try {
        $app = Get-StartApps | Where-Object { $_.Name -match $Pattern } | Select-Object -First 1
        if ($app) { return $app.AppID }
    } catch { }
    return ""
}

function Repair-Desktop {
    Write-Step "Repairing desktop shortcuts"
    $desktopPaths = @($PublicDesktop, "C:\Users\Administrator\Desktop")
    Get-ChildItem -Path $desktopPaths -Filter "*.lnk" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match "Node|npm|Python|pip|React Developer|MongoDB Service|Shell|Terminal|PowerShell|Command Prompt|EC2|Amazon|Guide|Feedback|Readme|Install Claude"
        } |
        Remove-Item -Force -ErrorAction SilentlyContinue

    $chrome = First-Path @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    )
    $code = First-Path @(
        "C:\Program Files\Microsoft VS Code\Code.exe",
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe"
    )
    $powershell = First-Path @(
        "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe",
        "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    )

    New-DesktopShortcut "Terminal.lnk" $powershell "-NoProfile -NoExit -Command `"cd '$LabRoot'; Write-Host 'Lab terminal ready. Java, Python, Node, npm, MongoDB and Git are available from here.'`"" $LabRoot
    New-DesktopShortcut "Google Chrome.lnk" $chrome
    New-DesktopShortcut "Visual Studio Code.lnk" $code "`"$LabRoot`"" $LabRoot

    $chatGptAppId = Get-StartAppId "ChatGPT"
    if ($chatGptAppId) {
        New-DesktopShortcut "ChatGPT.lnk" "explorer.exe" "shell:AppsFolder\$chatGptAppId"
    } elseif ($chrome) {
        New-DesktopShortcut "ChatGPT.lnk" $chrome "--app=https://chatgpt.com/"
    }

    $claudeExe = First-Path @(
        "C:\Users\Administrator\AppData\Local\Programs\Claude\Claude.exe",
        "C:\Program Files\Claude\Claude.exe",
        "C:\Program Files\AnthropicClaude\Claude.exe"
    )
    $claudeAppId = Get-StartAppId "Claude"
    if ($claudeExe) {
        New-DesktopShortcut "Claude Desktop.lnk" $claudeExe
    } elseif ($claudeAppId) {
        New-DesktopShortcut "Claude Desktop.lnk" "explorer.exe" "shell:AppsFolder\$claudeAppId"
    } elseif ($chrome) {
        New-DesktopShortcut "Claude Desktop.lnk" $chrome "https://claude.ai/"
    }

    $folderShortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $PublicDesktop "Lab Files.lnk"))
    $folderShortcut.TargetPath = $LabRoot
    $folderShortcut.Save()
}

function Validate-Repair {
    Write-Step "Validating repair"
    $checks = New-Object System.Collections.Generic.List[string]
    foreach ($cmd in @("java -version", "javac -version", "python --version", "node --version", "npm --version")) {
        $outFile = Join-Path $env:TEMP ("cloudlab-check-{0}.txt" -f ([guid]::NewGuid().ToString("N")))
        cmd.exe /c "$cmd > `"$outFile`" 2>&1"
        $exitCode = $LASTEXITCODE
        if (Test-Path $outFile) {
            Get-Content $outFile | Select-Object -First 2 | ForEach-Object { Write-Host $_ }
            Remove-Item -Force -ErrorAction SilentlyContinue $outFile
        }
        if ($exitCode -ne 0) { $checks.Add($cmd) | Out-Null }
    }
    if (-not (Test-Path (Join-Path $PublicDesktop "Terminal.lnk"))) { $checks.Add("Terminal shortcut") | Out-Null }
    if (-not (Test-Path (Join-Path $PublicDesktop "ChatGPT.lnk"))) { $checks.Add("ChatGPT shortcut") | Out-Null }
    if (-not (Test-Path (Join-Path $PublicDesktop "Claude Desktop.lnk"))) { $checks.Add("Claude Desktop shortcut") | Out-Null }
    if ($checks.Count -gt 0) {
        Set-Content -Path (Join-Path $LogDir "DesktopAppsRepairFailed.txt") -Value ($checks -join "`n") -Encoding ASCII
        Write-Host "CLOUDLAB_DESKTOP_APPS_REPAIR_FAILED: $($checks -join ', ')"
        exit 1
    }
    New-Item -ItemType File -Force -Path (Join-Path $LogDir "DesktopAppsRepairReady.txt") | Out-Null
    Write-Host "CLOUDLAB_DESKTOP_APPS_REPAIR_READY"
}

Install-Java
Install-ChatGPT
Install-ClaudeDesktop
Repair-Desktop
Validate-Repair

Stop-Transcript
