param(
    [string]$SubmissionShare = "",
    [string]$AssessmentPaperPath = "",
    [string]$DatasetPath = "",
    [string]$DriveLetter = "U",
    [switch]$PostReboot,
    [switch]$ShutdownWhenDone
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$LabRoot = "C:\LabSetup"
$DownloadDir = Join-Path $LabRoot "Downloads"
$PublicDesktop = "C:\Users\Public\Desktop"
$AssessmentDir = Join-Path $PublicDesktop "PowerBI_Assessment_Materials"
$LocalSubmissionDir = "C:\LabSubmission"
$LogFile = Join-Path $LabRoot "setup-powerbi-assessment.log"
$ValidationFile = Join-Path $LabRoot "validation.json"
$ScriptPath = $PSCommandPath

New-Item -ItemType Directory -Force -Path $LabRoot, $DownloadDir, $PublicDesktop, $AssessmentDir, $LocalSubmissionDir | Out-Null
Start-Transcript -Path $LogFile -Append

function Step {
    param([string]$Name, [scriptblock]$Block)
    Write-Host "===== $Name ====="
    & $Block
    Write-Host "DONE: $Name"
}

function Download-File {
    param([string[]]$Uris, [string]$OutFile, [int64]$MinBytes = 1MB, [int]$TimeoutSeconds = 1800)
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    $errors = @()
    foreach ($uri in ($Uris | Where-Object { $_ } | Select-Object -Unique)) {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                Remove-Item -Force -ErrorAction SilentlyContinue $OutFile
                Write-Host "Downloading $uri (attempt $attempt/3)"
                if (Test-Path $curl) {
                    & $curl -L --fail --silent --show-error --connect-timeout 30 --retry 3 --retry-delay 5 --max-time $TimeoutSeconds -o $OutFile $uri
                    if ($LASTEXITCODE -ne 0) { throw "curl exit code $LASTEXITCODE" }
                } else {
                    Invoke-WebRequest -UseBasicParsing -Uri $uri -MaximumRedirection 10 -OutFile $OutFile -TimeoutSec $TimeoutSeconds
                }
                if ((Test-Path $OutFile) -and ((Get-Item $OutFile).Length -ge $MinBytes)) {
                    return $uri
                }
                throw "downloaded file is missing or too small"
            } catch {
                $errors += "$uri attempt ${attempt}: $($_.Exception.Message)"
                Start-Sleep -Seconds (5 * $attempt)
            }
        }
    }
    throw "All download sources failed for $OutFile. $($errors -join ' | ')"
}

function First-ExistingPath {
    param([string[]]$Paths)
    $Paths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function Add-MachinePath {
    param([string]$PathItem)
    if (-not (Test-Path $PathItem)) { return }
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($machinePath -notlike "*$PathItem*") {
        [Environment]::SetEnvironmentVariable("Path", "$machinePath;$PathItem", "Machine")
    }
}

function New-DesktopShortcut {
    param([string]$Name, [string]$Target, [string]$Arguments = "")
    if (-not (Test-Path $Target)) { throw "Shortcut target not found: $Target" }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $PublicDesktop $Name))
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = Split-Path $Target
    $shortcut.Save()
}

function Set-DefaultUserDword {
    param([string]$SubKey, [string]$Name, [int]$Value)
    $hivePath = "C:\Users\Default\NTUSER.DAT"
    $mounted = $false
    try {
        if (-not (Test-Path "Registry::HKEY_USERS\DefaultUser")) {
            & reg.exe load HKU\DefaultUser $hivePath | Out-Null
            $mounted = $true
        }
        $path = "Registry::HKEY_USERS\DefaultUser\$SubKey"
        New-Item -Force -Path $path | Out-Null
        New-ItemProperty -Force -Path $path -Name $Name -PropertyType DWord -Value $Value | Out-Null
    } finally {
        if ($mounted) { & reg.exe unload HKU\DefaultUser | Out-Null }
    }
}

function Install-Chrome {
    $chromeMsi = Join-Path $DownloadDir "GoogleChromeStandaloneEnterprise64.msi"
    Download-File -Uris @(
        "https://dl.google.com/chrome/install/GoogleChromeStandaloneEnterprise64.msi",
        "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi"
    ) -OutFile $chromeMsi -MinBytes 50MB -TimeoutSeconds 1200 | Out-Null
    $proc = Start-Process msiexec.exe -ArgumentList "/i", "`"$chromeMsi`"", "/quiet", "/norestart" -Wait -PassThru
    if ($proc.ExitCode -notin 0, 3010) { throw "Chrome install failed: $($proc.ExitCode)" }
    $chrome = First-ExistingPath @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    )
    if (-not $chrome) { throw "Chrome executable not found after install" }
    Add-MachinePath (Split-Path $chrome)

    $assocXml = Join-Path $LabRoot "DefaultAppAssociations.xml"
    @'
<?xml version="1.0" encoding="UTF-8"?>
<DefaultAssociations>
  <Association Identifier=".htm" ProgId="ChromeHTML" ApplicationName="Google Chrome" />
  <Association Identifier=".html" ProgId="ChromeHTML" ApplicationName="Google Chrome" />
  <Association Identifier=".pdf" ProgId="ChromeHTML" ApplicationName="Google Chrome" />
  <Association Identifier="http" ProgId="ChromeHTML" ApplicationName="Google Chrome" />
  <Association Identifier="https" ProgId="ChromeHTML" ApplicationName="Google Chrome" />
</DefaultAssociations>
'@ | Set-Content -Encoding UTF8 -Path $assocXml
    & dism.exe /Online /Import-DefaultAppAssociations:$assocXml | Out-Null
    New-Item -Force "HKLM:\SOFTWARE\Policies\Microsoft\Windows\System" | Out-Null
    New-ItemProperty -Force "HKLM:\SOFTWARE\Policies\Microsoft\Windows\System" DefaultAssociationsConfiguration -PropertyType String -Value $assocXml | Out-Null
}

function Install-PowerBI {
    $webView2 = Join-Path $DownloadDir "MicrosoftEdgeWebView2Setup.exe"
    Download-File -Uris @("https://go.microsoft.com/fwlink/p/?LinkId=2124703") -OutFile $webView2 -MinBytes 1MB -TimeoutSeconds 600 | Out-Null
    $wv = Start-Process $webView2 -ArgumentList "/silent", "/install" -Wait -PassThru
    if ($wv.ExitCode -notin 0, 1638) { throw "WebView2 install failed: $($wv.ExitCode)" }

    $powerBiExe = Join-Path $DownloadDir "PBIDesktopSetup_x64.exe"
    Download-File -Uris @(
        "https://download.microsoft.com/download/8/8/0/880BCA75-79DD-466A-927D-1ABF1F5454B0/PBIDesktopSetup_x64.exe"
    ) -OutFile $powerBiExe -MinBytes 200MB -TimeoutSeconds 3600 | Out-Null
    $installDir = "C:\Program Files\Microsoft Power BI Desktop"
    $args = @(
        "-quiet",
        "-norestart",
        "-log", "`"$LabRoot\powerbi-install.log`"",
        "ACCEPT_EULA=1",
        "INSTALLDESKTOPSHORTCUT=1",
        "DISABLE_UPDATE_NOTIFICATION=1",
        "ENABLECXP=0",
        "INSTALLLOCATION=`"$installDir`""
    )
    $pbi = Start-Process $powerBiExe -ArgumentList $args -Wait -PassThru
    if ($pbi.ExitCode -notin 0, 3010) { throw "Power BI Desktop install failed: $($pbi.ExitCode)" }
    if (-not (Test-Path "$installDir\bin\PBIDesktop.exe")) { throw "PBIDesktop.exe not found after install" }
    [Environment]::SetEnvironmentVariable("POWERBI_DESKTOP_HOME", $installDir, "Machine")
    Add-MachinePath "$installDir\bin"

    New-Item -Force "HKCU:\Software\Microsoft\Microsoft Power BI Desktop" | Out-Null
    New-ItemProperty -Force "HKCU:\Software\Microsoft\Microsoft Power BI Desktop" DisableSignIn -PropertyType DWord -Value 1 | Out-Null
    Set-DefaultUserDword -SubKey "Software\Microsoft\Microsoft Power BI Desktop" -Name "DisableSignIn" -Value 1
    New-Item -Force "HKLM:\SOFTWARE\Policies\Microsoft\Microsoft Power BI Desktop" | Out-Null
    New-ItemProperty -Force "HKLM:\SOFTWARE\Policies\Microsoft\Microsoft Power BI Desktop" DisableUpdateNotification -PropertyType DWord -Value 1 | Out-Null
}

function Install-SupportingTools {
    $libreOffice = Join-Path $DownloadDir "LibreOffice_26.2.4_Win_x86-64.msi"
    Download-File -Uris @(
        "https://download.documentfoundation.org/libreoffice/stable/26.2.4/win/x86_64/LibreOffice_26.2.4_Win_x86-64.msi"
    ) -OutFile $libreOffice -MinBytes 200MB -TimeoutSeconds 2400 | Out-Null
    $lo = Start-Process msiexec.exe -ArgumentList "/i", "`"$libreOffice`"", "/quiet", "/norestart" -Wait -PassThru
    if ($lo.ExitCode -notin 0, 3010) { throw "LibreOffice install failed: $($lo.ExitCode)" }

    $sevenZip = Join-Path $DownloadDir "7z2500-x64.exe"
    Download-File -Uris @(
        "https://www.7-zip.org/a/7z2500-x64.exe",
        "https://www.7-zip.org/a/7z2409-x64.exe"
    ) -OutFile $sevenZip -MinBytes 1MB -TimeoutSeconds 600 | Out-Null
    $zip = Start-Process $sevenZip -ArgumentList "/S" -Wait -PassThru
    if ($zip.ExitCode -ne 0) { throw "7-Zip install failed: $($zip.ExitCode)" }
    Add-MachinePath "C:\Program Files\7-Zip"
}

function Set-LabDesktop {
    Get-ChildItem -LiteralPath $AssessmentDir -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    if ($AssessmentPaperPath -and (Test-Path $AssessmentPaperPath)) {
        Copy-Item -LiteralPath $AssessmentPaperPath -Destination (Join-Path $AssessmentDir (Split-Path $AssessmentPaperPath -Leaf)) -Force
    } else {
        @'
Assessment paper placeholder.

Replace this file with the final assessment paper before the learner lab goes live.
'@ | Set-Content -Encoding UTF8 -Path (Join-Path $AssessmentDir "Assessment_Paper_PLACEHOLDER.txt")
    }

    if ($DatasetPath -and (Test-Path $DatasetPath)) {
        Copy-Item -LiteralPath $DatasetPath -Destination (Join-Path $AssessmentDir (Split-Path $DatasetPath -Leaf)) -Force
    } else {
        @"
Category,Region,Sales,Quantity
Furniture,South,12000,8
Technology,West,22000,11
Office Supplies,North,8000,20
"@ | Set-Content -Encoding UTF8 -Path (Join-Path $AssessmentDir "Dataset_PLACEHOLDER.csv")
    }

    @"
POWER BI ASSESSMENT - START HERE

1. Open the Desktop folder: PowerBI_Assessment_Materials
2. Read the assessment paper carefully.
3. Open Power BI Desktop.
4. Import the dataset file from the assessment folder.
5. Build your report as instructed in the paper.
6. Save your file as: PowerBI_Solution_<YourName>_<EMPID>.pbix
7. Open This PC and go to the $DriveLetter`: drive.
8. Upload your .pbix file to the $DriveLetter`: drive only.
9. Confirm your file is visible in the $DriveLetter`: drive before logging out.

Do not submit to Desktop, Downloads, or Documents.
Only the $DriveLetter`: drive submission will be evaluated.
"@ | Set-Content -Encoding UTF8 -Path (Join-Path $PublicDesktop "READ_ME_FIRST.txt")

    & icacls.exe $AssessmentDir /inheritance:r | Out-Null
    & icacls.exe $AssessmentDir /grant:r "Administrators:(OI)(CI)(F)" "SYSTEM:(OI)(CI)(F)" "Users:(OI)(CI)(RX)" "Authenticated Users:(OI)(CI)(RX)" | Out-Null

    $chrome = First-ExistingPath @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    )
    New-DesktopShortcut "Google Chrome.lnk" $chrome
    New-DesktopShortcut "Power BI Desktop.lnk" "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe"
    $writer = Get-ChildItem "C:\Program Files\LibreOffice*\program\swriter.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($writer) { New-DesktopShortcut "LibreOffice Writer.lnk" $writer.FullName }
    New-DesktopShortcut "7-Zip File Manager.lnk" "C:\Program Files\7-Zip\7zFM.exe"

    $allowed = @(
        "PowerBI_Assessment_Materials",
        "READ_ME_FIRST.txt",
        "Google Chrome.lnk",
        "Power BI Desktop.lnk",
        "LibreOffice Writer.lnk",
        "7-Zip File Manager.lnk"
    )
    Get-ChildItem -LiteralPath $PublicDesktop -Force | Where-Object { $allowed -notcontains $_.Name } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

function Set-DriveMapping {
    $mapScript = Join-Path $LabRoot "map_submission_drive.ps1"
    $shareLiteral = $SubmissionShare.Replace("'", "''")
    @"
`$ErrorActionPreference = 'SilentlyContinue'
`$drive = '$DriveLetter`:'
`$share = '$shareLiteral'
if (Test-Path `$drive) { return }
if (`$share) {
    for (`$i = 0; `$i -lt 5; `$i++) {
        net use `$drive `$share /persistent:yes 2>`$null
        if (Test-Path `$drive) { return }
        Start-Sleep -Seconds 10
    }
} else {
    New-Item -ItemType Directory -Force -Path '$LocalSubmissionDir' | Out-Null
    subst `$drive '$LocalSubmissionDir'
}
"@ | Set-Content -Encoding ASCII -Path $mapScript

    & schtasks.exe /create /tn "MapPowerBISubmissionDrive" /tr "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$mapScript`"" /sc onstart /ru SYSTEM /rl HIGHEST /f | Out-Null
    $startupDir = "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"
    New-Item -ItemType Directory -Force -Path $startupDir | Out-Null
    "@powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$mapScript`"" |
        Set-Content -Encoding ASCII -Path (Join-Path $startupDir "MapPowerBISubmissionDrive.cmd")
    & powershell.exe -ExecutionPolicy Bypass -File $mapScript
}

function Set-Stability {
    powercfg /hibernate off
    powercfg /change monitor-timeout-ac 0
    powercfg /change standby-timeout-ac 0
    powercfg /change disk-timeout-ac 0
    New-Item -Force "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" | Out-Null
    New-ItemProperty -Force "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" NoAutoUpdate -PropertyType DWord -Value 1 | Out-Null
    Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
    Set-Service wuauserv -StartupType Disabled -ErrorAction SilentlyContinue
    New-Item -Force "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Notifications\Settings" | Out-Null
    New-ItemProperty -Force "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Notifications\Settings" NOC_GLOBAL_SETTING_TOASTS_ENABLED -PropertyType DWord -Value 0 | Out-Null
    Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\ServerManager" DoNotOpenServerManagerAtLogon 1 -Type DWord -Force -ErrorAction SilentlyContinue
}

function Test-LabReady {
    $checks = New-Object System.Collections.Generic.List[object]
    function Add-Check([string]$Name, [bool]$Passed, [string]$Detail = "") {
        $checks.Add([pscustomobject]@{ name = $Name; passed = $Passed; detail = $Detail })
    }

    $chrome = First-ExistingPath @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    )
    Add-Check "Chrome installed" ([bool]$chrome) $chrome
    Add-Check "Power BI installed" (Test-Path "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe") "PBIDesktop.exe"
    Add-Check "LibreOffice installed" ([bool](Get-ChildItem "C:\Program Files\LibreOffice*\program\swriter.exe" -ErrorAction SilentlyContinue | Select-Object -First 1)) "swriter.exe"
    Add-Check "7-Zip installed" (Test-Path "C:\Program Files\7-Zip\7z.exe") "7z.exe"
    Add-Check "Assessment folder exists" (Test-Path $AssessmentDir) $AssessmentDir
    Add-Check "Instructions exist" (Test-Path (Join-Path $PublicDesktop "READ_ME_FIRST.txt")) "READ_ME_FIRST.txt"
    Add-Check "Submission drive connected" (Test-Path "$DriveLetter`:") "$DriveLetter`:"
    try {
        $testFile = "$DriveLetter`:\submission_write_test.tmp"
        "ok" | Set-Content -Encoding ASCII -Path $testFile
        Remove-Item -Force $testFile
        Add-Check "Submission drive writable" $true "$DriveLetter`:"
    } catch {
        Add-Check "Submission drive writable" $false $_.Exception.Message
    }
    $mapTaskOutput = (schtasks.exe /query /tn "MapPowerBISubmissionDrive" 2>$null) -join "`n"
    Add-Check "Map task exists" ([bool]($mapTaskOutput -match "MapPowerBISubmissionDrive")) "scheduled task"

    $result = [pscustomobject]@{
        generated_at = (Get-Date).ToString("s")
        ready = -not ($checks | Where-Object { -not $_.passed })
        checks = $checks
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path $ValidationFile
    if (-not $result.ready) {
        $checks | Where-Object { -not $_.passed } | Format-Table -AutoSize | Out-String | Write-Host
        throw "Validation failed. See $ValidationFile"
    }
    Write-Host "POWERBI_ASSESSMENT_VM_READY"
}

if ($PostReboot) {
    Step "Post-reboot validation" {
        Set-DriveMapping
        Test-LabReady
        & schtasks.exe /delete /tn "PowerBIAssessmentPostRebootValidation" /f | Out-Null
    }
    Stop-Transcript
    if ($ShutdownWhenDone) {
        Stop-Computer -Force
    }
    exit 0
}

Step "Install Chrome" { Install-Chrome }
Step "Install Power BI Desktop" { Install-PowerBI }
Step "Install LibreOffice and 7-Zip" { Install-SupportingTools }
Step "Configure learner desktop" { Set-LabDesktop }
Step "Configure submission drive" { Set-DriveMapping }
Step "Disable interruptions" { Set-Stability }
Step "Pre-reboot validation" { Test-LabReady }
Step "Schedule post-reboot validation" {
    $postArgs = "powershell -ExecutionPolicy Bypass -File `"$ScriptPath`" -PostReboot -SubmissionShare `"$SubmissionShare`" -DriveLetter `"$DriveLetter`""
    if ($ShutdownWhenDone) { $postArgs += " -ShutdownWhenDone" }
    & schtasks.exe /create /tn "PowerBIAssessmentPostRebootValidation" /tr $postArgs /sc onstart /ru SYSTEM /rl HIGHEST /f | Out-Null
}

Stop-Transcript
Restart-Computer -Force
