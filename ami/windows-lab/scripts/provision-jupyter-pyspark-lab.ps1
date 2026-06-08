param(
    [string]$LabRoot = "C:\LabFiles",
    [string]$TimeZone = "India Standard Time",
    [switch]$SkipSysprep
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$LogDir = "C:\ProgramData\UNext"
$LogFile = Join-Path $LogDir "jupyter-pyspark-ami-build.log"
New-Item -ItemType Directory -Force -Path $LogDir, $LabRoot, "$LabRoot\Notebooks", "C:\Users\Public\Desktop" | Out-Null
Start-Transcript -Path $LogFile -Append

function Step {
    param([string]$Name, [scriptblock]$Block)
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

function Download-File {
    param([string]$Uri, [string]$OutFile, [int]$TimeoutSeconds = 900)
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

function New-DesktopShortcut {
    param([string]$Name, [string]$Target, [string]$Arguments = "", [string]$WorkingDirectory = "")
    if (-not (Test-Path $Target)) { throw "Shortcut target not found: $Target" }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path "C:\Users\Public\Desktop" $Name))
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    if ($WorkingDirectory) { $shortcut.WorkingDirectory = $WorkingDirectory } else { $shortcut.WorkingDirectory = Split-Path $Target }
    $shortcut.Save()
}

Step "Windows defaults" {
    Set-ExecutionPolicy Bypass -Scope Process -Force
    Set-TimeZone -Id $TimeZone -ErrorAction SilentlyContinue
    Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server" fDenyTSConnections 0
    Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" UserAuthentication 1
    Enable-NetFirewallRule -DisplayGroup "Remote Desktop" -ErrorAction SilentlyContinue
    powercfg /hibernate off
    powercfg /setactive SCHEME_MAX
    powercfg /change monitor-timeout-ac 0
    powercfg /change standby-timeout-ac 0
    Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\ServerManager" DoNotOpenServerManagerAtLogon 1 -Type DWord -Force
    Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" LongPathsEnabled 1 -Type DWord -Force
}

Step "Install Chrome, VS Code, Python, and Java" {
    $chromeMsi = Join-Path $LogDir "googlechromestandaloneenterprise64.msi"
    Download-File -Uri "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi" -OutFile $chromeMsi -TimeoutSeconds 600
    $chromeInstall = Start-Process -FilePath msiexec.exe -ArgumentList @("/i", $chromeMsi, "/qn", "/norestart") -Wait -PassThru
    if ($chromeInstall.ExitCode -notin 0, 3010) { throw "Chrome MSI failed: $($chromeInstall.ExitCode)" }

    $codeInstaller = Join-Path $LogDir "VSCodeSetup-x64.exe"
    Download-File -Uri "https://update.code.visualstudio.com/latest/win32-x64/stable" -OutFile $codeInstaller -TimeoutSeconds 900
    $codeInstall = Start-Process -FilePath $codeInstaller -ArgumentList "/verysilent", "/norestart", "/mergetasks=!runcode,addcontextmenufiles,addcontextmenufolders,addtopath" -Wait -PassThru
    if ($codeInstall.ExitCode -notin 0, 3010) { throw "VS Code installer failed: $($codeInstall.ExitCode)" }

    $pythonInstaller = Join-Path $LogDir "python-3.12-amd64.exe"
    Download-File -Uri "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $pythonInstaller -TimeoutSeconds 900
    $pythonInstall = Start-Process -FilePath $pythonInstaller -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_launcher=1", "Include_pip=1", "Include_test=0", "TargetDir=C:\Python312" -Wait -PassThru
    if ($pythonInstall.ExitCode -notin 0, 3010) { throw "Python installer failed: $($pythonInstall.ExitCode)" }

    $javaMsi = Join-Path $LogDir "temurin-jdk17.msi"
    Download-File -Uri "https://api.adoptium.net/v3/installer/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse?project=jdk" -OutFile $javaMsi -TimeoutSeconds 900
    $javaInstall = Start-Process -FilePath msiexec.exe -ArgumentList @("/i", $javaMsi, "/qn", "/norestart") -Wait -PassThru
    if ($javaInstall.ExitCode -notin 0, 3010) { throw "Temurin JDK installer failed: $($javaInstall.ExitCode)" }

    Add-MachinePath "C:\Program Files\Google\Chrome\Application"
    Add-MachinePath "C:\Program Files\Microsoft VS Code\bin"
    Add-MachinePath "C:\Python312"
    Add-MachinePath "C:\Python312\Scripts"
}

Step "Configure Java, Python, Jupyter, and PySpark" {
    $javaHome = Get-ChildItem -Directory -Path "C:\Program Files\Eclipse Adoptium\jdk-17*" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if (-not $javaHome) { throw "JDK 17 install directory was not found" }
    [Environment]::SetEnvironmentVariable("JAVA_HOME", $javaHome.FullName, "Machine")
    Add-MachinePath (Join-Path $javaHome.FullName "bin")

    $env:Path = "$([Environment]::GetEnvironmentVariable("Path", "Machine"));$([Environment]::GetEnvironmentVariable("Path", "User"))"
    py -3.12 -m pip install --upgrade pip setuptools wheel
    py -3.12 -m pip install notebook jupyterlab pyspark findspark pandas numpy matplotlib pyarrow

    $pythonExe = "C:\Python312\python.exe"
    $sitePackages = (& $pythonExe -c "import site; print(site.getsitepackages()[0])").Trim()
    $sparkHome = Join-Path $sitePackages "pyspark"
    if (-not (Test-Path $sparkHome)) { throw "PySpark package path was not found: $sparkHome" }
    [Environment]::SetEnvironmentVariable("SPARK_HOME", $sparkHome, "Machine")
    [Environment]::SetEnvironmentVariable("PYSPARK_PYTHON", $pythonExe, "Machine")
    [Environment]::SetEnvironmentVariable("PYSPARK_DRIVER_PYTHON", $pythonExe, "Machine")
    Add-MachinePath (Join-Path $sparkHome "bin")

    @"
{
  "NotebookApp": {
    "notebook_dir": "$($LabRoot.Replace('\', '\\'))\\Notebooks",
    "ip": "127.0.0.1",
    "open_browser": true
  }
}
"@ | Set-Content -Path (Join-Path $LogDir "jupyter_notebook_config.json") -Encoding ASCII
}

Step "Create desktop shortcuts" {
    $chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
    $code = "C:\Program Files\Microsoft VS Code\Code.exe"
    $powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    New-DesktopShortcut "Google Chrome.lnk" $chrome
    New-DesktopShortcut "Google Colab.lnk" $chrome "https://colab.research.google.com/"
    New-DesktopShortcut "Visual Studio Code.lnk" $code "`"$LabRoot`"" $LabRoot
    New-DesktopShortcut "Jupyter Notebook.lnk" $powershell "-NoProfile -ExecutionPolicy Bypass -NoExit -Command `"cd '$LabRoot\Notebooks'; python -m notebook --ip=127.0.0.1 --notebook-dir='$LabRoot\Notebooks'`"" "$LabRoot\Notebooks"
    New-DesktopShortcut "PySpark Shell.lnk" $powershell "-NoProfile -NoExit -Command `"python -c 'from pyspark.sql import SparkSession; print(\"PySpark ready\")'; pyspark`"" $LabRoot

    $shell = New-Object -ComObject WScript.Shell
    $folderShortcut = $shell.CreateShortcut("C:\Users\Public\Desktop\Lab Files.lnk")
    $folderShortcut.TargetPath = $LabRoot
    $folderShortcut.Save()
}

Step "Validate Jupyter, VS Code, Colab shortcut, and PySpark" {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $env:Path = "$machinePath;$([Environment]::GetEnvironmentVariable("Path", "User"))"
    $env:JAVA_HOME = [Environment]::GetEnvironmentVariable("JAVA_HOME", "Machine")
    $env:SPARK_HOME = [Environment]::GetEnvironmentVariable("SPARK_HOME", "Machine")
    $env:PYSPARK_PYTHON = [Environment]::GetEnvironmentVariable("PYSPARK_PYTHON", "Machine")
    $env:PYSPARK_DRIVER_PYTHON = [Environment]::GetEnvironmentVariable("PYSPARK_DRIVER_PYTHON", "Machine")

    chrome --version
    code --version
    python --version
    pip --version
    java -version
    jupyter notebook --version
    python -c "import notebook, pyspark, findspark, pandas, numpy, pyarrow; print('python packages ok')"
    python -c "from pyspark.sql import SparkSession; spark = SparkSession.builder.master('local[1]').appName('UNextValidation').getOrCreate(); print(spark.range(3).count()); spark.stop()"

    @(
        "Google Chrome.lnk",
        "Google Colab.lnk",
        "Visual Studio Code.lnk",
        "Jupyter Notebook.lnk",
        "PySpark Shell.lnk",
        "Lab Files.lnk"
    ) | ForEach-Object {
        if (-not (Test-Path (Join-Path "C:\Users\Public\Desktop" $_))) { throw "Missing desktop shortcut: $_" }
    }

    foreach ($name in @("JAVA_HOME", "SPARK_HOME", "PYSPARK_PYTHON", "PYSPARK_DRIVER_PYTHON")) {
        if (-not [Environment]::GetEnvironmentVariable($name, "Machine")) { throw "Missing machine environment variable: $name" }
    }
}

Step "Cleanup before image capture" {
    py -3.12 -m pip cache purge
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "$env:TEMP\*", "C:\Windows\Temp\*"
    Clear-RecycleBin -Force -ErrorAction SilentlyContinue
}

Write-Host "UNEXT_JUPYTER_PYSPARK_AMI_READY"
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
