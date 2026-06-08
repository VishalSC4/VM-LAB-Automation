param(
    [string]$LabRoot = "C:\LabFiles",
    [string]$TimeZone = "India Standard Time",
    [switch]$IncludePowerBI,
    [switch]$SkipSysprep
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$LogDir = "C:\ProgramData\UNext"
$LogFile = Join-Path $LogDir "golden-ami-build.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Start-Transcript -Path $LogFile -Append

function Step {
    param(
        [string]$Name,
        [scriptblock]$Block
    )
    Write-Host "===== $Name ====="
    & $Block
    Write-Host "DONE: $Name"
}

function Add-MachinePath {
    param([string]$PathItem)
    if (-not (Test-Path $PathItem)) { return }
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($machinePath -notlike "*$PathItem*") {
        [Environment]::SetEnvironmentVariable("Path", "$machinePath;$PathItem", "Machine")
    }
}

function Resolve-FirstPath {
    param([string[]]$Paths)
    return $Paths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function New-LabShortcut {
    param(
        [string]$Name,
        [string]$Target,
        [string]$Arguments = "",
        [string]$WorkingDirectory = ""
    )
    if (-not $Target -or -not (Test-Path $Target)) { throw "Shortcut target not found for ${Name}: $Target" }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path "C:\Users\Public\Desktop" $Name))
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    if ($WorkingDirectory) { $shortcut.WorkingDirectory = $WorkingDirectory }
    $shortcut.Save()
}

function Download-File {
    param(
        [string]$Uri,
        [string]$OutFile,
        [int]$TimeoutSeconds = 900
    )
    Remove-Item -Force -ErrorAction SilentlyContinue $OutFile
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (Test-Path $curl) {
        & $curl -L --fail --silent --show-error --connect-timeout 20 --max-time $TimeoutSeconds -o $OutFile $Uri
        if ($LASTEXITCODE -ne 0) { throw "curl.exe failed for $Uri with exit code $LASTEXITCODE" }
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -MaximumRedirection 10 -OutFile $OutFile -TimeoutSec $TimeoutSeconds
    }
    if (-not (Test-Path $OutFile) -or ((Get-Item $OutFile).Length -lt 1MB)) {
        throw "Download did not produce a usable file: $OutFile"
    }
}

Step "Windows performance and RDP defaults" {
    Set-ExecutionPolicy Bypass -Scope Process -Force
    Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server" fDenyTSConnections 0
    Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" UserAuthentication 1
    Enable-NetFirewallRule -DisplayGroup "Remote Desktop" -ErrorAction SilentlyContinue
    powercfg /hibernate off
    powercfg /setactive SCHEME_MAX
    powercfg /change monitor-timeout-ac 0
    powercfg /change standby-timeout-ac 0
    Set-TimeZone -Id $TimeZone -ErrorAction SilentlyContinue
    Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\ServerManager" DoNotOpenServerManagerAtLogon 1 -Type DWord -Force
    Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" LongPathsEnabled 1 -Type DWord -Force
    New-Item -ItemType Directory -Force -Path `
        $LabRoot, `
        "$LabRoot\Python", `
        "$LabRoot\Node", `
        "$LabRoot\React", `
        "$LabRoot\MongoDB\data", `
        "$LabRoot\MongoDB\log", `
        "C:\Users\Public\Desktop" | Out-Null
}

Step "Install Chocolatey and required software" {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
    if (-not (Get-Command choco.exe -ErrorAction SilentlyContinue)) {
        Invoke-Expression ((New-Object Net.WebClient).DownloadString("https://community.chocolatey.org/install.ps1"))
    }
    $env:Path = "$([Environment]::GetEnvironmentVariable("Path", "Machine"));$([Environment]::GetEnvironmentVariable("Path", "User"))"
    choco feature enable -n allowGlobalConfirmation
    choco install googlechrome vscode python nodejs-lts mongodb mongosh git --yes --no-progress --limit-output
}

Step "Remove excluded and classroom-unwanted software" {
    choco uninstall eclipse jupyter jupyterlab anaconda3 miniconda3 --yes --no-progress --limit-output
    Get-ChildItem -Path "C:\Users\Public\Desktop","C:\Users\Administrator\Desktop" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "Eclipse|Jupyter|Notebook|Anaconda|Conda|Server Manager|EC2|Amazon|Guide|Feedback|Readme" } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
        "C:\eclipse", `
        "C:\tools\eclipse", `
        "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Eclipse*", `
        "$LabRoot\Notebooks", `
        "$LabRoot\launch-jupyter-notebook.ps1", `
        "$LabRoot\launch-jupyter-notebook.vbs"
    py -3 -m pip uninstall -y notebook jupyter jupyterlab jupyterlab-server jupyter-server jupyter-client jupyter-core ipykernel nbclassic nbclient nbconvert nbformat qtconsole voila
}

Step "Configure machine PATH and developer defaults" {
    @(
        "C:\Program Files\Google\Chrome\Application",
        "C:\Program Files\Microsoft VS Code\bin",
        "C:\Python312",
        "C:\Python312\Scripts",
        "C:\Python313",
        "C:\Python313\Scripts",
        "C:\Program Files\nodejs",
        "C:\Program Files\MongoDB\Server\7.0\bin",
        "C:\Program Files\MongoDB\Server\8.0\bin",
        "C:\Program Files\mongosh",
        "C:\Program Files\Git\cmd",
        "C:\ProgramData\chocolatey\bin"
    ) | ForEach-Object { Add-MachinePath $_ }
    Get-ChildItem -Directory -Path "C:\Python*" -ErrorAction SilentlyContinue | ForEach-Object {
        Add-MachinePath $_.FullName
        Add-MachinePath (Join-Path $_.FullName "Scripts")
    }
    Get-ChildItem -Directory -Path "C:\Program Files\MongoDB\Server\*" -ErrorAction SilentlyContinue | ForEach-Object {
        Add-MachinePath (Join-Path $_.FullName "bin")
    }

    $env:Path = "$([Environment]::GetEnvironmentVariable("Path", "Machine"));$([Environment]::GetEnvironmentVariable("Path", "User"))"
    py -3 -m pip install --upgrade pip setuptools wheel
    npm config set fund false --global
    npm config set audit false --global
    npm install --global create-react-app vite yarn
}

if ($IncludePowerBI) {
    Step "Install and configure Microsoft Power BI Desktop" {
        $powerBiUrl = $env:POWERBI_DOWNLOAD_URL
        if (-not $powerBiUrl) {
            $powerBiUrl = "https://download.microsoft.com/download/8/8/0/880BCA75-79DD-466A-927D-1ABF1F5454B0/PBIDesktopSetup_x64.exe"
        }
        $webView2Url = $env:WEBVIEW2_DOWNLOAD_URL
        if (-not $webView2Url) {
            $webView2Url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
        }

        $installDir = "C:\Program Files\Microsoft Power BI Desktop"
        $powerBiInstaller = Join-Path $LogDir "PBIDesktopSetup_x64.exe"
        $webView2Installer = Join-Path $LogDir "MicrosoftEdgeWebView2Setup.exe"
        Download-File -Uri $webView2Url -OutFile $webView2Installer -TimeoutSeconds 300
        $webView2Process = Start-Process -FilePath $webView2Installer -ArgumentList "/silent", "/install" -Wait -PassThru
        if ($webView2Process.ExitCode -notin 0, 1638) {
            throw "WebView2 installer failed with exit code $($webView2Process.ExitCode)"
        }

        Download-File -Uri $powerBiUrl -OutFile $powerBiInstaller -TimeoutSeconds 1800
        $powerBiArgs = @(
            "-quiet",
            "-norestart",
            "-log", "`"$LogDir\powerbi-install.log`"",
            "ACCEPT_EULA=1",
            "INSTALLDESKTOPSHORTCUT=1",
            "DISABLE_UPDATE_NOTIFICATION=1",
            "INSTALLLOCATION=`"$installDir`""
        )
        $powerBiProcess = Start-Process -FilePath $powerBiInstaller -ArgumentList $powerBiArgs -Wait -PassThru
        if ($powerBiProcess.ExitCode -notin 0, 3010) {
            throw "Power BI Desktop installer failed with exit code $($powerBiProcess.ExitCode)"
        }

        $powerBiExe = Join-Path $installDir "bin\PBIDesktop.exe"
        if (-not (Test-Path $powerBiExe)) { throw "PBIDesktop.exe was not found at $powerBiExe" }

        [Environment]::SetEnvironmentVariable("POWERBI_DESKTOP_HOME", $installDir, "Machine")
        Add-MachinePath (Join-Path $installDir "bin")

        New-Item -Path "HKLM:\SOFTWARE\Microsoft\Microsoft Power BI Desktop" -Force | Out-Null
        Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Microsoft Power BI Desktop" -Name DisableUpdateNotification -Value 1 -Type DWord -Force

        New-Item -Path "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A7-37EF-4b3f-8CFC-4F3A74704073}" -Force | Out-Null
        Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A7-37EF-4b3f-8CFC-4F3A74704073}" -Name IsInstalled -Value 0 -Type DWord -Force
        New-Item -Path "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A8-37EF-4b3f-8CFC-4F3A74704073}" -Force | Out-Null
        Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A8-37EF-4b3f-8CFC-4F3A74704073}" -Name IsInstalled -Value 0 -Type DWord -Force

        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut("C:\Users\Public\Desktop\Power BI Desktop.lnk")
        $shortcut.TargetPath = $powerBiExe
        $shortcut.WorkingDirectory = Split-Path $powerBiExe
        $shortcut.Save()
    }
}

Step "Configure MongoDB service" {
    $mongoCfg = Join-Path $LogDir "mongod.cfg"
    @"
systemLog:
  destination: file
  path: C:\LabFiles\MongoDB\log\mongod.log
  logAppend: true
storage:
  dbPath: C:\LabFiles\MongoDB\data
net:
  bindIp: 127.0.0.1
  port: 27017
"@ | Set-Content -Path $mongoCfg -Encoding ASCII

    $mongod = @(
        "C:\Program Files\MongoDB\Server\*\bin\mongod.exe",
        "C:\ProgramData\chocolatey\lib\mongodb\tools\*\bin\mongod.exe"
    ) | ForEach-Object { Get-ChildItem $_ -File -ErrorAction SilentlyContinue } |
        Sort-Object FullName -Descending |
        Select-Object -First 1

    if (-not $mongod) { throw "mongod.exe was not found after MongoDB install" }
    if (-not (Get-Service MongoDB -ErrorAction SilentlyContinue)) {
        & $mongod.FullName --config $mongoCfg --install --serviceName MongoDB
    }
    Set-Service MongoDB -StartupType Automatic
    Start-Service MongoDB
}

Step "Create React sample project" {
    $reactDir = Join-Path $LabRoot "React\sample-react-app"
    if (-not (Test-Path (Join-Path $reactDir "package.json"))) {
        Push-Location (Join-Path $LabRoot "React")
        npm create vite@latest sample-react-app -- --template react
        Pop-Location
    }
    Push-Location $reactDir
    npm install
    npm run build -- --emptyOutDir
    Pop-Location
}

Step "Create public desktop shortcuts" {
    $desktop = "C:\Users\Public\Desktop"
    New-Item -ItemType Directory -Force -Path $desktop | Out-Null
    Get-ChildItem -Path $desktop,"C:\Users\Administrator\Desktop" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "Eclipse|Jupyter|Notebook|Anaconda|Conda|Server Manager|EC2|Amazon|Guide|Feedback|Readme" } |
        Remove-Item -Force -ErrorAction SilentlyContinue

    $chrome = @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    $code = @(
        "C:\Program Files\Microsoft VS Code\Code.exe",
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    $powershell = Resolve-FirstPath @(
        "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe",
        "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    if (-not $powershell) { throw "powershell.exe was not found" }

    New-LabShortcut "Google Chrome.lnk" $chrome
    New-LabShortcut "Google Colab.lnk" $chrome "https://colab.research.google.com/"
    New-LabShortcut "Visual Studio Code.lnk" $code "`"$LabRoot`"" $LabRoot
    New-LabShortcut "Node.js Terminal.lnk" $powershell "-NoProfile -NoExit -Command `"node --version; cd '$LabRoot\Node'`"" "$LabRoot\Node"
    New-LabShortcut "npm Terminal.lnk" $powershell "-NoProfile -NoExit -Command `"npm --version; cd '$LabRoot\Node'`"" "$LabRoot\Node"
    New-LabShortcut "Python 3 Shell.lnk" $powershell "-NoProfile -NoExit -Command `"python --version; cd '$LabRoot\Python'; python`"" "$LabRoot\Python"
    New-LabShortcut "pip Terminal.lnk" $powershell "-NoProfile -NoExit -Command `"pip --version; cd '$LabRoot\Python'`"" "$LabRoot\Python"
    New-LabShortcut "React Sample App.lnk" $powershell "-NoProfile -ExecutionPolicy Bypass -NoExit -Command `"cd '$LabRoot\React\sample-react-app'; npm run dev -- --host 127.0.0.1`"" "$LabRoot\React\sample-react-app"
    New-LabShortcut "React Developer Terminal.lnk" $powershell "-NoProfile -NoExit -Command `"vite --version; cd '$LabRoot\React'`"" "$LabRoot\React"
    New-LabShortcut "MongoDB Shell.lnk" $powershell "-NoProfile -NoExit -Command `"Start-Service MongoDB -ErrorAction SilentlyContinue; mongosh`"" "$LabRoot\MongoDB"
    New-LabShortcut "MongoDB Service Status.lnk" $powershell "-NoProfile -NoExit -Command `"Get-Service MongoDB; mongosh --quiet --eval 'db.runCommand({ ping: 1 })'`"" "$LabRoot\MongoDB"
    if ($IncludePowerBI) {
        $powerBi = "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe"
        New-LabShortcut "Power BI Desktop.lnk" $powerBi
    }

    $shell = New-Object -ComObject WScript.Shell
    $folderShortcut = $shell.CreateShortcut((Join-Path $desktop "Lab Files.lnk"))
    $folderShortcut.TargetPath = $LabRoot
    $folderShortcut.Save()
}

Step "Run validation" {
    $validationArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\ProgramData\UNext\validate-windows-lab.ps1", "-LabRoot", $LabRoot)
    if ($IncludePowerBI) { $validationArgs += "-IncludePowerBI" }
    $validation = Start-Process -FilePath "powershell.exe" -ArgumentList $validationArgs -Wait -PassThru
    if ($validation.ExitCode -ne 0) { throw "Validation failed with exit code $($validation.ExitCode)" }
}

Step "Cleanup before image capture" {
    choco clean --yes -ErrorAction SilentlyContinue
    npm cache clean --force
    py -3 -m pip cache purge
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "$env:TEMP\*", "C:\Windows\Temp\*"
    Clear-RecycleBin -Force -ErrorAction SilentlyContinue
}

Write-Host "UNEXT_GOLDEN_AMI_READY"
Stop-Transcript

if ($SkipSysprep) {
    Stop-Computer -Force
}

$ec2Launch = "C:\Program Files\Amazon\EC2Launch\EC2Launch.exe"
if (Test-Path $ec2Launch) {
    & $ec2Launch sysprep --shutdown=true
} else {
    Stop-Computer -Force
}
