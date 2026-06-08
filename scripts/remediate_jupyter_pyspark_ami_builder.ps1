$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Start-Transcript -Path "C:\ProgramData\UNext\jupyter-pyspark-remediate.log" -Append

$LabRoot = "C:\LabFiles"
New-Item -ItemType Directory -Force -Path $LabRoot, "$LabRoot\Notebooks", "C:\Users\Public\Desktop" | Out-Null

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
foreach ($pathItem in @(
    "C:\Program Files\Google\Chrome\Application",
    "C:\Program Files\Microsoft VS Code\bin",
    "C:\Python312",
    "C:\Python312\Scripts"
)) {
    if ((Test-Path $pathItem) -and $machinePath -notlike "*$pathItem*") {
        $machinePath = "$machinePath;$pathItem"
    }
}

$javaHome = Get-ChildItem -Directory -Path "C:\Program Files\Eclipse Adoptium\jdk-17*" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $javaHome) { throw "JDK 17 directory not found" }
[Environment]::SetEnvironmentVariable("JAVA_HOME", $javaHome.FullName, "Machine")
$javaBin = Join-Path $javaHome.FullName "bin"
if ($machinePath -notlike "*$javaBin*") { $machinePath = "$machinePath;$javaBin" }

$pythonExe = "C:\Python312\python.exe"
$sparkHome = & $pythonExe -c "import pathlib, pyspark; print(pathlib.Path(pyspark.__file__).resolve().parent)"
$sparkHome = ($sparkHome | Select-Object -Last 1).Trim()
if (-not (Test-Path $sparkHome)) { throw "PySpark path not found: $sparkHome" }
[Environment]::SetEnvironmentVariable("SPARK_HOME", $sparkHome, "Machine")
[Environment]::SetEnvironmentVariable("PYSPARK_PYTHON", $pythonExe, "Machine")
[Environment]::SetEnvironmentVariable("PYSPARK_DRIVER_PYTHON", $pythonExe, "Machine")
$sparkBin = Join-Path $sparkHome "bin"
if ((Test-Path $sparkBin) -and $machinePath -notlike "*$sparkBin*") { $machinePath = "$machinePath;$sparkBin" }
[Environment]::SetEnvironmentVariable("Path", $machinePath, "Machine")

$env:Path = "$machinePath;$([Environment]::GetEnvironmentVariable("Path", "User"))"
$env:JAVA_HOME = $javaHome.FullName
$env:SPARK_HOME = $sparkHome
$env:PYSPARK_PYTHON = $pythonExe
$env:PYSPARK_DRIVER_PYTHON = $pythonExe

$desktop = "C:\Users\Public\Desktop"
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut {
    param(
        [string]$Name,
        [string]$Target,
        [string]$Arguments = "",
        [string]$WorkingDirectory = ""
    )
    if (-not (Test-Path $Target)) { throw "Missing shortcut target: $Target" }
    $shortcut = $shell.CreateShortcut((Join-Path $desktop $Name))
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = if ($WorkingDirectory) { $WorkingDirectory } else { Split-Path $Target }
    $shortcut.Save()
}

New-Shortcut "Google Chrome.lnk" "C:\Program Files\Google\Chrome\Application\chrome.exe"
New-Shortcut "Google Colab.lnk" "C:\Program Files\Google\Chrome\Application\chrome.exe" "https://colab.research.google.com/"
New-Shortcut "Visual Studio Code.lnk" "C:\Program Files\Microsoft VS Code\Code.exe" "`"$LabRoot`"" $LabRoot
New-Shortcut "Jupyter Notebook.lnk" "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" "-NoProfile -ExecutionPolicy Bypass -NoExit -Command `"cd '$LabRoot\Notebooks'; python -m notebook --ip=127.0.0.1 --notebook-dir='$LabRoot\Notebooks'`"" "$LabRoot\Notebooks"
New-Shortcut "PySpark Shell.lnk" "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" "-NoProfile -NoExit -Command `"python -c 'from pyspark.sql import SparkSession; print(`"PySpark ready`")'; pyspark`"" $LabRoot

$folderShortcut = $shell.CreateShortcut((Join-Path $desktop "Lab Files.lnk"))
$folderShortcut.TargetPath = $LabRoot
$folderShortcut.Save()

chrome --version
code --version
python --version
pip --version
java -version
jupyter notebook --version
python -c "import notebook, pyspark, findspark, pandas, numpy, pyarrow; print('python packages ok')"
python -c "from pyspark.sql import SparkSession; spark=SparkSession.builder.master('local[1]').appName('UNextValidation').getOrCreate(); print(spark.range(3).count()); spark.stop()"

foreach ($shortcut in @(
    "Google Chrome.lnk",
    "Google Colab.lnk",
    "Visual Studio Code.lnk",
    "Jupyter Notebook.lnk",
    "PySpark Shell.lnk",
    "Lab Files.lnk"
)) {
    if (-not (Test-Path (Join-Path $desktop $shortcut))) { throw "Missing shortcut: $shortcut" }
}

foreach ($name in @("JAVA_HOME", "SPARK_HOME", "PYSPARK_PYTHON", "PYSPARK_DRIVER_PYTHON")) {
    if (-not [Environment]::GetEnvironmentVariable($name, "Machine")) { throw "Missing machine environment variable: $name" }
}

py -3.12 -m pip cache purge
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "C:\Windows\Temp\*", "C:\Users\Administrator\AppData\Local\Temp\*"
Write-Host "UNEXT_JUPYTER_PYSPARK_AMI_READY"
Stop-Transcript
& "C:\Program Files\Amazon\EC2Launch\EC2Launch.exe" sysprep --shutdown=true
