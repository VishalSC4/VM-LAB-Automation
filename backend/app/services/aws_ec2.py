import asyncio
import base64
from dataclasses import dataclass
import gzip
from html import escape
import re

import boto3
from botocore.exceptions import ClientError, WaiterError
from botocore.config import Config
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import get_settings

log = structlog.get_logger()


AWS_RETRY_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 3, "mode": "standard"},
)


@dataclass
class InstanceResult:
    instance_id: str
    private_ip: str | None
    public_ip: str | None
    windows_hostname: str | None = None
    instance_type: str | None = None
    market: str = "on-demand"
    spot_instance_request_id: str | None = None


def _is_instance_not_found(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in {
        "InvalidInstanceID.NotFound",
        "InvalidInstanceID.Malformed",
    }


def _is_run_instances_throttle(exc: BaseException) -> bool:
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    message = exc.response.get("Error", {}).get("Message", "").lower()
    return code in {
        "RequestLimitExceeded",
        "RequestThrottled",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    } or "request rate limit" in message


def windows_user_data(
    username: str,
    password: str,
    idle_timeout_minutes: int,
    admin_username: str = "Administrator",
    *,
    lab_type: str = "windows",
    claude_profile_id: str | None = None,
    claude_profile_bucket: str | None = None,
    claude_profile_prefix: str = "",
    claude_profile_archive_suffix: str = ".zip",
    claude_account_email: str = "",
    claude_profile_download_url: str = "",
) -> str:
    def ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    claude_msix_url = "https://claude.ai/api/desktop/win32/x64/msix/latest/redirect"
    script = f"""
$ErrorActionPreference = 'Continue'
$Username = {ps_literal(username)}
$AdminUsername = {ps_literal(admin_username)}
$PlainPassword = {ps_literal(password)}
$IdleLimitMinutes = {idle_timeout_minutes}
$LabType = {ps_literal(lab_type)}
$ClaudeProfileId = {ps_literal(claude_profile_id or "")}
$ClaudeProfileBucket = {ps_literal(claude_profile_bucket or "")}
$ClaudeProfilePrefix = {ps_literal(claude_profile_prefix or "")}
$ClaudeProfileArchiveSuffix = {ps_literal(claude_profile_archive_suffix)}
$ClaudeAccountEmail = {ps_literal(claude_account_email or "")}
$ClaudeProfileDownloadUrl = {ps_literal(claude_profile_download_url or "")}
$ClaudeMsixUrl = {ps_literal(claude_msix_url)}
$Password = ConvertTo-SecureString -String $PlainPassword -AsPlainText -Force

Start-Transcript -Path 'C:\\ProgramData\\Amazon\\EC2-Windows\\Launch\\Log\\cloudlab-bootstrap.log' -Append
net user $AdminUsername $PlainPassword /active:yes
Set-LocalUser -Name $AdminUsername -Password $Password -ErrorAction Continue
Enable-LocalUser -Name $AdminUsername -ErrorAction Continue
Add-LocalGroupMember -Group 'Remote Desktop Users' -Member $AdminUsername -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' -Name fDenyTSConnections -Value 0
Set-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp' -Name UserAuthentication -Value 1
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction Continue
Restart-Service TermService -Force -ErrorAction Continue
Set-Service -Name WSearch -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name WSearch -ErrorAction SilentlyContinue
Set-Service -Name StateRepository -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name StateRepository -ErrorAction SilentlyContinue
Start-Service -Name AppXSvc -ErrorAction SilentlyContinue
Start-Service -Name Themes -ErrorAction SilentlyContinue
powercfg /hibernate off
powercfg /setactive SCHEME_MAX
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
Set-TimeZone -Id 'India Standard Time' -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\ServerManager' -Name DoNotOpenServerManagerAtLogon -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' -Name LongPathsEnabled -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' -Name EnableLUA -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' -Name FilterAdministratorToken -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' -Name ConsentPromptBehaviorAdmin -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' -Name PromptOnSecureDesktop -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Active Setup\\Installed Components\\{{A509B1A7-37EF-4b3f-8CFC-4F3A74704073}}' -Name IsInstalled -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Active Setup\\Installed Components\\{{A509B1A8-37EF-4b3f-8CFC-4F3A74704073}}' -Name IsInstalled -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
New-Item -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Windows Search' -Force | Out-Null
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Windows Search' -Name AllowCortana -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Windows Search' -Name DisableWebSearch -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Windows Search' -Name ConnectedSearchUseWeb -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Windows Search' -Name ConnectedSearchUseWebOverMeteredConnections -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue

function Repair-StartAndSearch {{
    $ControlPath = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control'
    try {{
        $Acl = Get-Acl $ControlPath
        $Acl.SetAccessRuleProtection($false, $true)
        $Rule = New-Object System.Security.AccessControl.RegistryAccessRule(
            'ALL APPLICATION PACKAGES',
            'ReadKey',
            'ContainerInherit,ObjectInherit',
            'None',
            'Allow'
        )
        $Acl.SetAccessRule($Rule)
        Set-Acl -Path $ControlPath -AclObject $Acl
    }} catch {{ }}
    New-Item -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Search' -Force | Out-Null
    Set-ItemProperty -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Search' -Name BingSearchEnabled -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Search' -Name CortanaConsent -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Search' -Name AllowSearchToUseLocation -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Search' -Name ImmersiveSearch -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Search' -Name SearchboxTaskbarMode -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
    Get-Process -Name SearchHost,SearchIndexer,StartMenuExperienceHost,ShellExperienceHost,explorer -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Stop-Service -Name WSearch -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    $PackageNames = @(
        'Microsoft.Windows.StartMenuExperienceHost',
        'Microsoft.Windows.ShellExperienceHost',
        'Microsoft.Windows.Search',
        'MicrosoftWindows.Client.CBS'
    )
    foreach ($PackageName in $PackageNames) {{
        Get-Process -Name SearchHost,StartMenuExperienceHost,ShellExperienceHost -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Get-AppxPackage -AllUsers -Name $PackageName -ErrorAction SilentlyContinue | ForEach-Object {{
            $Manifest = Join-Path $_.InstallLocation 'AppxManifest.xml'
            if (Test-Path $Manifest) {{
                Add-AppxPackage -DisableDevelopmentMode -Register $Manifest -ErrorAction SilentlyContinue
            }}
        }}
    }}
    $SearchData = 'C:\\ProgramData\\Microsoft\\Search\\Data\\Applications\\Windows'
    if (Test-Path $SearchData) {{
        icacls $SearchData /grant 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' /T /C | Out-Null
    }}
    Get-ChildItem -Path "$env:LOCALAPPDATA\\Packages" -Directory -Filter 'Microsoft.Windows.Search*' -ErrorAction SilentlyContinue |
        ForEach-Object {{
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $_.FullName 'LocalState')
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $_.FullName 'TempState')
        }}
    Start-Service -Name StateRepository -ErrorAction SilentlyContinue
    Start-Service -Name AppXSvc -ErrorAction SilentlyContinue
    Start-Service -Name WSearch -ErrorAction SilentlyContinue
    $TdlRecover = Join-Path $env:SystemRoot 'System32\\tdlrecover.exe'
    if (Test-Path $TdlRecover) {{
        & $TdlRecover -reregister -resetlayout -resetcache
    }}
    Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\\ctfmon.exe') -ErrorAction SilentlyContinue
    Start-Process explorer.exe -ErrorAction SilentlyContinue
}}
Repair-StartAndSearch

function Ensure-OpenShellMenu {{
    $OpenShell = @(
        'C:\\Program Files\\Open-Shell\\StartMenu.exe',
        'C:\\Program Files\\Open-Shell\\ClassicStartMenu.exe',
        'C:\\Program Files\\Open-Shell\\OpenShellMenu.exe'
    ) | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
    if (-not $OpenShell -and (Get-Command choco.exe -ErrorAction SilentlyContinue)) {{
        choco install open-shell -y --no-progress --limit-output
        $OpenShell = @(
            'C:\\Program Files\\Open-Shell\\StartMenu.exe',
            'C:\\Program Files\\Open-Shell\\ClassicStartMenu.exe',
            'C:\\Program Files\\Open-Shell\\OpenShellMenu.exe'
        ) | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
    }}
    if ($OpenShell) {{
        $StartupDir = 'C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Startup'
        New-Item -ItemType Directory -Force -Path $StartupDir | Out-Null
        $Shell = New-Object -ComObject WScript.Shell
        $Shortcut = $Shell.CreateShortcut((Join-Path $StartupDir 'Open-Shell Menu.lnk'))
        $Shortcut.TargetPath = $OpenShell
        $Shortcut.Save()
        Start-Process -FilePath $OpenShell -ErrorAction SilentlyContinue
    }}
}}
Ensure-OpenShellMenu

$CloudLabDir = 'C:\\ProgramData\\CloudLab'
New-Item -ItemType Directory -Force -Path $CloudLabDir | Out-Null
$StartSearchRepairScript = Join-Path $CloudLabDir 'Repair-StartSearch.ps1'
(
    '$ErrorActionPreference = ''Continue''',
    "Start-Transcript -Path 'C:\\ProgramData\\CloudLab\\StartSearchRepair.log' -Append",
    ${{function:Repair-StartAndSearch}}.ToString(),
    "Stop-Transcript"
) -join [Environment]::NewLine | Set-Content -Path $StartSearchRepairScript -Encoding UTF8
$StartSearchTask = 'CloudLabRepairStartSearch'
$StartSearchTaskTime = (Get-Date).AddMinutes(1).ToString('HH:mm')
schtasks /Delete /TN $StartSearchTask /F 2>$null | Out-Null
schtasks /Create /TN $StartSearchTask /SC ONCE /ST $StartSearchTaskTime /RU $AdminUsername /RP $PlainPassword /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$StartSearchRepairScript`"" /F | Out-Null
schtasks /Run /TN $StartSearchTask | Out-Null
$StartSearchLogonTask = 'CloudLabRepairStartSearchOnLogon'
schtasks /Delete /TN $StartSearchLogonTask /F 2>$null | Out-Null
schtasks /Create /TN $StartSearchLogonTask /SC ONLOGON /RU $AdminUsername /RL HIGHEST /IT /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartSearchRepairScript`"" /F | Out-Null

$LabRoot = 'C:\\LabFiles'
$PublicDesktop = 'C:\\Users\\Public\\Desktop'
New-Item -ItemType Directory -Force -Path $LabRoot,'C:\\LabFiles\\Downloads','C:\\LabFiles\\Pictures','C:\\LabFiles\\Documents','C:\\LabFiles\\Notebooks','C:\\LabFiles\\Python','C:\\LabFiles\\Node','C:\\LabFiles\\React','C:\\LabFiles\\MongoDB',$PublicDesktop | Out-Null
Get-ChildItem -Path $PublicDesktop,"C:\\Users\\Administrator\\Desktop","C:\\Users\\$Username\\Desktop" -ErrorAction SilentlyContinue |
    Where-Object {{ $_.Name -match 'EC2|Amazon|Guide|Feedback|Readme|Server Manager' }} |
    Remove-Item -Force -ErrorAction SilentlyContinue

$Shell = New-Object -ComObject WScript.Shell
function New-LabShortcut([string]$Name, [string]$Target, [string]$Arguments = '', [string]$WorkingDirectory = '') {{
    if (-not $Target -or -not (Test-Path $Target)) {{ return }}
    $Shortcut = $Shell.CreateShortcut((Join-Path $PublicDesktop $Name))
    $Shortcut.TargetPath = $Target
    if ($Arguments) {{ $Shortcut.Arguments = $Arguments }}
    if ($WorkingDirectory) {{ $Shortcut.WorkingDirectory = $WorkingDirectory }}
    $Shortcut.Save()
}}

$ChromePath = @(
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'
) | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
$CodePath = @(
    'C:\\Program Files\\Microsoft VS Code\\Code.exe',
    "$env:LOCALAPPDATA\\Programs\\Microsoft VS Code\\Code.exe"
) | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
New-LabShortcut 'Google Chrome.lnk' $ChromePath
New-LabShortcut 'Visual Studio Code.lnk' $CodePath 'C:\\LabFiles' 'C:\\LabFiles'

$MachinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$ExtraPaths = @(
    'C:\\Program Files\\Google\\Chrome\\Application',
    'C:\\Program Files\\Microsoft VS Code\\bin',
    'C:\\Python312',
    'C:\\Python312\\Scripts',
    'C:\\Program Files\\nodejs',
    'C:\\Program Files\\MongoDB\\Server\\7.0\\bin',
    'C:\\Program Files\\MongoDB\\Server\\8.0\\bin',
    'C:\\Program Files\\mongosh',
    'C:\\Program Files\\Git\\cmd',
    'C:\\ProgramData\\chocolatey\\bin'
) | Where-Object {{ Test-Path $_ }}
foreach ($PathItem in $ExtraPaths) {{
    if ($MachinePath -notlike "*$PathItem*") {{ $MachinePath = "$MachinePath;$PathItem" }}
}}
[Environment]::SetEnvironmentVariable('Path', $MachinePath, 'Machine')

$FolderShortcut = $Shell.CreateShortcut((Join-Path $PublicDesktop 'Lab Files.lnk'))
$FolderShortcut.TargetPath = $LabRoot
$FolderShortcut.Save()

if ($ChromePath) {{
    $ColabShortcut = $Shell.CreateShortcut((Join-Path $PublicDesktop 'Google Colab.lnk'))
    $ColabShortcut.TargetPath = $ChromePath
    $ColabShortcut.Arguments = 'https://colab.research.google.com/'
    $ColabShortcut.Save()
}}

if (Test-Path 'C:\\LabFiles\\React\\sample-react-app\\package.json') {{
    $ReactShortcut = $Shell.CreateShortcut((Join-Path $PublicDesktop 'React Sample App.lnk'))
    $ReactShortcut.TargetPath = 'powershell.exe'
    $ReactShortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -NoExit -Command "cd C:\\LabFiles\\React\\sample-react-app; npm run dev -- --host 127.0.0.1"'
    $ReactShortcut.WorkingDirectory = 'C:\\LabFiles\\React\\sample-react-app'
    $ReactShortcut.Save()
}}

$MongoShortcut = $Shell.CreateShortcut((Join-Path $PublicDesktop 'MongoDB Shell.lnk'))
$MongoShortcut.TargetPath = 'powershell.exe'
$MongoShortcut.Arguments = '-NoProfile -NoExit -Command "mongosh"'
$MongoShortcut.WorkingDirectory = 'C:\\LabFiles\\MongoDB'
$MongoShortcut.Save()

Get-ChildItem -Path $PublicDesktop,"C:\\Users\\Administrator\\Desktop","C:\\Users\\$Username\\Desktop" -ErrorAction SilentlyContinue |
    Where-Object {{ $_.Name -match 'Eclipse|Jupyter|Notebook|Anaconda|Conda' }} |
    Remove-Item -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue 'C:\\LabFiles\\Notebooks','C:\\LabFiles\\launch-jupyter-notebook.ps1','C:\\LabFiles\\launch-jupyter-notebook.vbs'

if ($LabType -eq 'claude') {{
    $ClaudeScript = @'
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$LogPath = 'C:\\ProgramData\\CloudLab\\ClaudeBootstrap.log'
Start-Transcript -Path $LogPath -Append

function Write-Step([string]$Message) {{
    Write-Output ("[{{0}}] {{1}}" -f (Get-Date -Format o), $Message)
}}

function Retry-Step([scriptblock]$Block, [string]$Name, [int]$Attempts = 5) {{
    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {{
        try {{
            Write-Step "$Name attempt $Attempt"
            & $Block
            return
        }} catch {{
            Write-Step "$Name failed: $($_.Exception.Message)"
            if ($Attempt -eq $Attempts) {{ throw }}
            Start-Sleep -Seconds ([Math]::Min(30, 4 * $Attempt))
        }}
    }}
}}

function Download-File([string]$Uri, [string]$OutFile, [int]$TimeoutSeconds = 180) {{
    Remove-Item -Force -ErrorAction SilentlyContinue $OutFile
    $Curl = Join-Path $env:SystemRoot 'System32\\curl.exe'
    if (Test-Path $Curl) {{
        & $Curl -L --fail --silent --show-error --connect-timeout 10 --max-time $TimeoutSeconds -o $OutFile $Uri
        if ($LASTEXITCODE -ne 0) {{ throw "curl.exe failed with exit code $LASTEXITCODE" }}
    }} else {{
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -MaximumRedirection 10 -OutFile $OutFile -TimeoutSec $TimeoutSeconds
    }}
    if (-not (Test-Path $OutFile) -or ((Get-Item $OutFile).Length -lt 1024)) {{
        throw "Download did not produce a usable file: $OutFile"
    }}
}}

$Username = '__WINDOWS_USERNAME__'
$StudentUsername = '__STUDENT_USERNAME__'
$ProfileId = '__PROFILE_ID__'
$Bucket = '__BUCKET__'
$Prefix = '__PREFIX__'
$ArchiveSuffix = '__ARCHIVE_SUFFIX__'
$AccountEmail = '__ACCOUNT_EMAIL__'
$ProfileDownloadUrl = '__PROFILE_DOWNLOAD_URL__'
$ClaudeMsixUrl = '__CLAUDE_MSIX_URL__'
$ReadyMarker = 'C:\\ProgramData\\CloudLab\\ClaudeReady.marker'

if (-not $ProfileId -or -not $Bucket) {{ throw 'Claude profile id or bucket is missing' }}

$UserRoot = Join-Path 'C:\\Users' $Username
$Roaming = Join-Path $UserRoot 'AppData\\Roaming'
$Local = Join-Path $UserRoot 'AppData\\Local'
$ClaudeRoaming = Join-Path $Roaming 'Claude'
$ClaudeLocal = Join-Path $Local 'Claude'
$WorkRoot = 'C:\\ProgramData\\CloudLab\\ClaudeProfiles'
$ArchivePath = Join-Path $WorkRoot "$ProfileId$ArchiveSuffix"
$ExtractRoot = Join-Path $WorkRoot "extract-$ProfileId"
$S3Key = (($Prefix.TrimEnd('/')) + '/' + "$ProfileId$ArchiveSuffix").TrimStart('/')
$S3Uri = "s3://$Bucket/$S3Key"

New-Item -ItemType Directory -Force -Path $WorkRoot,$Roaming,$Local | Out-Null
Get-Process -Name 'Claude' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

function Install-ClaudeDesktop {{
    $KnownClaude = @(
        (Join-Path $Local 'Programs\\Claude\\Claude.exe'),
        'C:\\Program Files\\Claude\\Claude.exe',
        'C:\\Program Files\\AnthropicClaude\\Claude.exe'
    ) | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
    $PackagedClaude = Get-ChildItem 'C:\\Program Files\\WindowsApps' -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($KnownClaude -or $PackagedClaude) {{
        Write-Step 'Claude Desktop is already installed'
        return
    }}

    $MsixPath = Join-Path $WorkRoot 'Claude.msix'
    Retry-Step {{ Download-File $ClaudeMsixUrl $MsixPath 180 }} 'Download Claude Desktop MSIX' 3
    if (-not (Test-Path $MsixPath)) {{ throw 'Claude Desktop MSIX download failed' }}
    Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\AppModelUnlock' -Name AllowAllTrustedApps -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
    $Dism = Join-Path $env:SystemRoot 'System32\\dism.exe'
    $DismExitCode = $null
    for ($DismAttempt = 1; $DismAttempt -le 4; $DismAttempt++) {{
        Write-Step "Provision Claude Desktop with dism.exe attempt $DismAttempt"
        & $Dism /Online /Add-ProvisionedAppxPackage /PackagePath:$MsixPath /SkipLicense /Region:all | Out-Null
        $DismExitCode = $LASTEXITCODE
        if ($DismExitCode -in @(0, 3010)) {{ break }}
        if ($DismExitCode -eq 15609 -and $DismAttempt -lt 4) {{
            Start-Sleep -Seconds (10 * $DismAttempt)
            continue
        }}
        throw "dism.exe failed to provision Claude Desktop with exit code $DismExitCode"
    }}
    Write-Step 'Claude Desktop MSIX provisioned for all users'
}}

Install-ClaudeDesktop

$LaunchScript = 'C:\\ProgramData\\CloudLab\\Launch-Claude.ps1'
Set-Content -Path $LaunchScript -Encoding UTF8 -Value @"
Start-Sleep -Seconds 2
`$UserRoot = Join-Path 'C:\\Users' '$Username'
`$Local = Join-Path `$UserRoot 'AppData\\Local'
`$ClaudeExe = @(
    (Join-Path `$Local 'Programs\\Claude\\Claude.exe'),
    'C:\\Program Files\\Claude\\Claude.exe',
    'C:\\Program Files\\AnthropicClaude\\Claude.exe'
) | Where-Object {{ Test-Path `$_ }} | Select-Object -First 1
if (`$ClaudeExe) {{
    Start-Process -FilePath `$ClaudeExe
}} else {{
    `$ClaudeApp = `$null
    try {{ `$ClaudeApp = Get-StartApps | Where-Object {{ `$_.Name -match 'Claude' }} | Select-Object -First 1 }} catch {{ }}
    if (`$ClaudeApp) {{
        Start-Process explorer.exe ('shell:AppsFolder\\' + `$ClaudeApp.AppID)
    }} else {{
        Start-Process 'https://claude.ai/download'
    }}
}}
"@
schtasks /Create /TN 'CloudLabLaunchClaude' /SC ONLOGON /RU $Username /RL LIMITED /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$LaunchScript`"" /F | Out-Null

$CaptureScript = 'C:\\ProgramData\\CloudLab\\Save-ClaudeProfile.ps1'
Set-Content -Path $CaptureScript -Encoding UTF8 -Value @"
`$ErrorActionPreference = 'Stop'
`$ProfileId = '$ProfileId'
`$S3Uri = '$S3Uri'
`$Username = '$Username'
`$AwsCli = @(
    'C:\\Program Files\\Amazon\\AWSCLIV2\\aws.exe',
    'C:\\Program Files\\Amazon\\AWSCLI\\bin\\aws.exe',
    'aws.exe'
) | Where-Object {{ Get-Command `$_ -ErrorAction SilentlyContinue }} | Select-Object -First 1
if (-not `$AwsCli) {{ throw 'AWS CLI is required only for Save Claude Profile. User labs do not require AWS CLI.' }}
`$UserRoot = Join-Path 'C:\\Users' `$Username
`$RoamingClaude = Join-Path `$UserRoot 'AppData\\Roaming\\Claude'
`$LocalClaude = Join-Path `$UserRoot 'AppData\\Local\\Claude'
`$WorkRoot = 'C:\\ProgramData\\CloudLab\\ClaudeProfiles'
`$Stage = Join-Path `$WorkRoot ('capture-' + `$ProfileId)
`$Archive = Join-Path `$WorkRoot (`$ProfileId + '.zip')
`$ClaudeExe = @(
    (Join-Path (Join-Path `$UserRoot 'AppData\\Local') 'Programs\\Claude\\Claude.exe'),
    'C:\\Program Files\\Claude\\Claude.exe',
    'C:\\Program Files\\AnthropicClaude\\Claude.exe'
) | Where-Object {{ Test-Path `$_ }} | Select-Object -First 1
Start-Transcript -Path 'C:\\ProgramData\\CloudLab\\ClaudeProfileCapture.log' -Append
if (-not (Test-Path `$RoamingClaude)) {{ throw 'Claude profile folder was not found. Log in to Claude Desktop first, then run this shortcut again.' }}
Get-Process -Name 'Claude' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `$Stage,`$Archive
New-Item -ItemType Directory -Force -Path (Join-Path `$Stage 'Roaming') | Out-Null
Copy-Item -Path `$RoamingClaude -Destination (Join-Path `$Stage 'Roaming\\Claude') -Recurse -Force
if (Test-Path `$LocalClaude) {{
    New-Item -ItemType Directory -Force -Path (Join-Path `$Stage 'Local') | Out-Null
    Copy-Item -Path `$LocalClaude -Destination (Join-Path `$Stage 'Local\\Claude') -Recurse -Force
}}
Compress-Archive -Path (Join-Path `$Stage '*') -DestinationPath `$Archive -Force
& `$AwsCli s3 cp `$Archive `$S3Uri --only-show-errors
Write-Output ('Saved Claude profile to ' + `$S3Uri)
if (`$ClaudeExe) {{
    Start-Process -FilePath `$ClaudeExe
}} else {{
    `$ClaudeApp = `$null
    try {{ `$ClaudeApp = Get-StartApps | Where-Object {{ `$_.Name -match 'Claude' }} | Select-Object -First 1 }} catch {{ }}
    if (`$ClaudeApp) {{ Start-Process explorer.exe ('shell:AppsFolder\\' + `$ClaudeApp.AppID) }}
}}
Stop-Transcript
"@

$Shell = New-Object -ComObject WScript.Shell
$PublicDesktop = 'C:\\Users\\Public\\Desktop'
New-Item -ItemType Directory -Force -Path $PublicDesktop | Out-Null
$CaptureShortcut = $Shell.CreateShortcut((Join-Path $PublicDesktop 'Save Claude Profile.lnk'))
$CaptureShortcut.TargetPath = 'powershell.exe'
$CaptureShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$CaptureScript`""
$CaptureShortcut.Save()
$LaunchShortcut = $Shell.CreateShortcut((Join-Path $PublicDesktop 'Claude Desktop.lnk'))
$LaunchShortcut.TargetPath = 'powershell.exe'
$LaunchShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$LaunchScript`""
$LaunchShortcut.Save()
$InstallShortcut = $Shell.CreateShortcut((Join-Path $PublicDesktop 'Install Claude Desktop.lnk'))
$InstallShortcut.TargetPath = 'https://claude.ai/download'
$InstallShortcut.Save()
Set-Content -Path (Join-Path $PublicDesktop 'Claude Login Setup.txt') -Encoding UTF8 -Value @"
Claude profile slot: $ProfileId
Expected Claude account: $AccountEmail

Claude Desktop is installed automatically for this lab.
If Claude asks for login, complete login and OTP for $AccountEmail in Claude Desktop.
After Claude opens successfully, double-click "Save Claude Profile" on the desktop.
That uploads the reusable profile to:
$S3Uri
"@

$DownloadedProfile = $false
try {{
    if (-not $ProfileDownloadUrl) {{ throw 'Claude profile download URL is missing' }}
    Retry-Step {{ Download-File $ProfileDownloadUrl $ArchivePath 120 }} 'Download Claude profile archive' 3
    $DownloadedProfile = (Test-Path $ArchivePath) -and ((Get-Item $ArchivePath).Length -ge 1024)
}} catch {{
    Write-Step "No existing Claude profile archive found for $ProfileId. Manual login/enrollment mode will be used."
}}

if (-not $DownloadedProfile) {{
    Write-Step 'Claude launch task registered for manual login. Use the desktop Save Claude Profile shortcut after OTP login.'
    Stop-Transcript
    exit 0
}}

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ExtractRoot
New-Item -ItemType Directory -Force -Path $ExtractRoot | Out-Null
Retry-Step {{ Expand-Archive -Path $ArchivePath -DestinationPath $ExtractRoot -Force }} 'Extract Claude profile'

$SourceRoaming = @(
    (Join-Path $ExtractRoot 'Roaming\\Claude'),
    (Join-Path $ExtractRoot 'Claude'),
    (Join-Path $ExtractRoot 'AppData\\Roaming\\Claude')
) | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
$SourceLocal = @(
    (Join-Path $ExtractRoot 'Local\\Claude'),
    (Join-Path $ExtractRoot 'AppData\\Local\\Claude')
) | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
if (-not $SourceRoaming) {{ throw 'Archive must contain Claude profile data under Claude, Roaming\\Claude, or AppData\\Roaming\\Claude' }}

if (Test-Path $ClaudeRoaming) {{
    Rename-Item -Path $ClaudeRoaming -NewName ("Claude.backup.{{0}}" -f (Get-Date -Format 'yyyyMMddHHmmss')) -Force
}}
Copy-Item -Path $SourceRoaming -Destination $ClaudeRoaming -Recurse -Force
if ($SourceLocal) {{
    if (Test-Path $ClaudeLocal) {{ Remove-Item -Recurse -Force $ClaudeLocal }}
    Copy-Item -Path $SourceLocal -Destination $ClaudeLocal -Recurse -Force
}}

$AclPaths = @($ClaudeRoaming)
if (Test-Path $ClaudeLocal) {{ $AclPaths += $ClaudeLocal }}
foreach ($Path in $AclPaths) {{
    icacls $Path /inheritance:e /grant "${{Username}}:(OI)(CI)F" | Out-Null
}}

New-Item -ItemType File -Force -Path $ReadyMarker | Out-Null
Write-Step 'Claude profile injected and launch task registered'
Stop-Transcript
'@
    $ClaudeScript = $ClaudeScript.Replace('__WINDOWS_USERNAME__', $AdminUsername)
    $ClaudeScript = $ClaudeScript.Replace('__STUDENT_USERNAME__', $Username)
    $ClaudeScript = $ClaudeScript.Replace('__PROFILE_ID__', $ClaudeProfileId)
    $ClaudeScript = $ClaudeScript.Replace('__BUCKET__', $ClaudeProfileBucket)
    $ClaudeScript = $ClaudeScript.Replace('__PREFIX__', $ClaudeProfilePrefix)
    $ClaudeScript = $ClaudeScript.Replace('__ARCHIVE_SUFFIX__', $ClaudeProfileArchiveSuffix)
    $ClaudeScript = $ClaudeScript.Replace('__ACCOUNT_EMAIL__', $ClaudeAccountEmail)
    $ClaudeScript = $ClaudeScript.Replace('__PROFILE_DOWNLOAD_URL__', $ClaudeProfileDownloadUrl)
    $ClaudeScript = $ClaudeScript.Replace('__CLAUDE_MSIX_URL__', $ClaudeMsixUrl)

    $ClaudeScriptPath = 'C:\\ProgramData\\CloudLab\\Inject-ClaudeProfile.ps1'
    New-Item -ItemType Directory -Force -Path (Split-Path $ClaudeScriptPath) | Out-Null
    Set-Content -Path $ClaudeScriptPath -Value $ClaudeScript -Encoding UTF8
    schtasks /Create /TN 'CloudLabClaudeBootstrap' /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ClaudeScriptPath`"" /F | Out-Null
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$ClaudeScriptPath`"" -WindowStyle Hidden
}}

$IdleScript = @'
$ErrorActionPreference = 'SilentlyContinue'
$IdleLimitMinutes = __IDLE_LIMIT__

function Convert-IdleMinutes([string]$Value) {{
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -eq '.') {{ return 0 }}
    if ($Value -match '^(?<days>\\d+)\\+(?<hours>\\d+):(?<minutes>\\d+)$') {{
        return ([int]$Matches.days * 1440) + ([int]$Matches.hours * 60) + [int]$Matches.minutes
    }}
    if ($Value -match '^(?<hours>\\d+):(?<minutes>\\d+)$') {{
        return ([int]$Matches.hours * 60) + [int]$Matches.minutes
    }}
    if ($Value -match '^\\d+$') {{ return [int]$Value }}
    return 0
}}

$IdleValues = @()
$Lines = quser 2>$null | Select-Object -Skip 1
foreach ($Line in $Lines) {{
    $Parts = (($Line -replace '^\\s*>', '').Trim() -split '\\s+')
    if ($Parts.Count -ge 6) {{
        $IdleValues += Convert-IdleMinutes $Parts[-4]
    }}
}}

if ($IdleValues.Count -gt 0 -and ($IdleValues | Measure-Object -Minimum).Minimum -ge $IdleLimitMinutes) {{
    Stop-Computer -Force
}}
'@.Replace('__IDLE_LIMIT__', [string]$IdleLimitMinutes)
$IdleScriptPath = 'C:\\ProgramData\\CloudLab\\Stop-WhenIdle.ps1'
New-Item -ItemType Directory -Force -Path (Split-Path $IdleScriptPath) | Out-Null
Set-Content -Path $IdleScriptPath -Value $IdleScript -Encoding UTF8
schtasks /Create /TN 'CloudLabIdleStop' /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$IdleScriptPath`"" /F | Out-Null
Write-Output 'CLOUDLAB_BOOTSTRAP_DONE'
Stop-Transcript
"""
    encoded_script = base64.b64encode(gzip.compress(script.encode("utf-8"), compresslevel=9)).decode("ascii")
    wrapper = f"""
$ErrorActionPreference = 'Stop'
$BootstrapRoot = 'C:\\ProgramData\\CloudLab'
$BootstrapPath = Join-Path $BootstrapRoot 'Bootstrap-Main.ps1'
New-Item -ItemType Directory -Force -Path $BootstrapRoot | Out-Null
$Compressed = [Convert]::FromBase64String('{encoded_script}')
$InputStream = New-Object System.IO.MemoryStream(,$Compressed)
$GzipStream = New-Object System.IO.Compression.GzipStream($InputStream, [System.IO.Compression.CompressionMode]::Decompress)
$Reader = New-Object System.IO.StreamReader($GzipStream, [System.Text.Encoding]::UTF8)
$Script = $Reader.ReadToEnd()
$Reader.Close()
Set-Content -Path $BootstrapPath -Value $Script -Encoding UTF8
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $BootstrapPath
"""
    return f"<powershell>\n{escape(wrapper, quote=False)}\n</powershell>\n<persist>true</persist>"


class AwsEc2Service:
    def __init__(self, region: str):
        self.region = region
        self.settings = get_settings()
        self.client = boto3.client(
            "ec2",
            region_name=region,
            config=AWS_RETRY_CONFIG,
        )
        self.s3_client = boto3.client(
            "s3",
            region_name=region,
            config=AWS_RETRY_CONFIG,
        )

    @retry(
        retry=retry_if_exception(_is_run_instances_throttle),
        stop=stop_after_attempt(8),
        wait=wait_exponential(multiplier=3, min=5, max=90),
        reraise=True,
    )
    async def launch_windows_instance(
        self,
        *,
        ami_id: str,
        instance_type: str,
        username: str,
        password: str,
        display_name: str,
        batch_id: str,
        lab_id: str,
        budget_limit: float,
        idle_timeout_minutes: int,
        expiry_iso: str,
        instance_market: str | None = None,
        lab_type: str = "windows",
        claude_profile_id: str | None = None,
    ) -> InstanceResult:
        return await asyncio.to_thread(
            self._launch_sync,
            ami_id,
            instance_type,
            username,
            password,
            display_name,
            batch_id,
            lab_id,
            budget_limit,
            idle_timeout_minutes,
            expiry_iso,
            instance_market,
            lab_type,
            claude_profile_id,
        )

    def _spot_instance_types(self, requested_instance_type: str) -> list[str]:
        configured = self.settings.lab_spot_instance_types or ""
        types = [item.strip() for item in configured.split(",") if item.strip()]
        if not types:
            types = [requested_instance_type]
        if requested_instance_type not in types:
            types.insert(0, requested_instance_type)
        return list(dict.fromkeys(types))

    def _spot_enabled(self, instance_market: str | None = None) -> bool:
        market = (instance_market or self.settings.lab_instance_market).lower()
        return self.settings.lab_spot_enabled and market == "spot"

    def _lab_subnet_ids(self) -> list[str | None]:
        configured = self.settings.lab_subnet_ids or self.settings.lab_subnet_id or ""
        subnet_ids = [item.strip() for item in configured.split(",") if item.strip()]
        return list(dict.fromkeys(subnet_ids)) or [None]

    def _is_spot_capacity_error(self, exc: ClientError) -> bool:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", "").lower()
        capacity_codes = {
            "InsufficientInstanceCapacity",
            "InsufficientHostCapacity",
            "MaxSpotInstanceCountExceeded",
            "SpotInstanceLimitExceeded",
            "Unsupported",
        }
        capacity_markers = [
            "spot",
            "capacity",
            "not supported",
            "insufficient",
            "no spot",
            "max spot",
        ]
        return code in capacity_codes or ("spot" in message and any(marker in message for marker in capacity_markers))

    def _claude_profile_key(self, profile_id: str) -> str:
        prefix = self.settings.claude_profile_prefix.strip("/")
        filename = f"{profile_id}{self.settings.claude_profile_archive_suffix}"
        return f"{prefix}/{filename}" if prefix else filename

    def _claude_profile_download_url(self, lab_type: str, profile_id: str | None) -> str:
        if lab_type != "claude" or not profile_id:
            return ""
        bucket = self.settings.claude_profile_bucket
        if not bucket:
            return ""
        return self.s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": self._claude_profile_key(profile_id)},
            ExpiresIn=3600,
        )

    def _base_launch_params(
        self,
        ami_id,
        instance_type,
        username,
        password,
        display_name,
        batch_id,
        lab_id,
        budget_limit,
        idle_timeout_minutes,
        expiry_iso,
        market,
        lab_type,
        claude_profile_id,
        subnet_id=None,
    ):
        minimum_root_volume_size_gb = self._minimum_root_volume_size_gb(ami_id)
        root_volume_size_gb = max(int(self.settings.lab_root_volume_size_gb), minimum_root_volume_size_gb, 30)
        root_disk_tag = f"{root_volume_size_gb}GB-gp3"
        params = {
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "InstanceInitiatedShutdownBehavior": "stop",
            "UserData": windows_user_data(
                username,
                password,
                idle_timeout_minutes,
                self.settings.windows_admin_user,
                lab_type=lab_type,
                claude_profile_id=claude_profile_id,
                claude_profile_bucket=self.settings.claude_profile_bucket,
                claude_profile_prefix=self.settings.claude_profile_prefix,
                claude_profile_archive_suffix=self.settings.claude_profile_archive_suffix,
                claude_account_email=self.settings.claude_account_email,
                claude_profile_download_url=self._claude_profile_download_url(lab_type, claude_profile_id),
            ),
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": root_volume_size_gb,
                        "VolumeType": "gp3",
                        "Iops": 3000,
                        "Throughput": 125,
                        "DeleteOnTermination": True,
                    },
                }
            ],
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": display_name},
                        {"Key": "Project", "Value": "UNextCloudLab"},
                        {"Key": "Environment", "Value": self.settings.environment},
                        {"Key": "ManagedBy", "Value": "cloud-lab-platform"},
                        {"Key": "OS", "Value": "Windows"},
                        {"Key": "cloudlab:lab_type", "Value": lab_type},
                        {"Key": "cloudlab:ami_id", "Value": ami_id},
                        {"Key": "RAM", "Value": "8GB"},
                        {"Key": "vCPU", "Value": "4"},
                        {"Key": "Disk", "Value": root_disk_tag},
                        {"Key": "Software", "Value": "claude-desktop" if lab_type == "claude" else "vscode-nodejs-pip-python3-mongodb-react-chrome-colab"},
                        {"Key": "cloudlab:owner", "Value": username},
                        {"Key": "cloudlab:user_label", "Value": display_name},
                        {"Key": "cloudlab:batch_id", "Value": batch_id},
                        {"Key": "cloudlab:lab_id", "Value": lab_id},
                        {"Key": "cloudlab:expiry_time", "Value": expiry_iso},
                        {"Key": "cloudlab:budget_limit", "Value": str(budget_limit)},
                        {"Key": "cloudlab:instance_market", "Value": market},
                        {"Key": "cloudlab:claude_profile_id", "Value": claude_profile_id or ""},
                    ],
                },
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {"Key": "Name", "Value": f"{display_name}-root"},
                        {"Key": "Project", "Value": "UNextCloudLab"},
                        {"Key": "Environment", "Value": self.settings.environment},
                        {"Key": "ManagedBy", "Value": "cloud-lab-platform"},
                        {"Key": "Disk", "Value": root_disk_tag},
                        {"Key": "cloudlab:lab_id", "Value": lab_id},
                        {"Key": "cloudlab:batch_id", "Value": batch_id},
                        {"Key": "cloudlab:ami_id", "Value": ami_id},
                    ],
                },
            ],
        }
        if market == "spot":
            spot_options = {
                "SpotInstanceType": "persistent",
                "InstanceInterruptionBehavior": "stop",
            }
            if self.settings.lab_spot_max_price:
                spot_options["MaxPrice"] = self.settings.lab_spot_max_price
            params["InstanceMarketOptions"] = {
                "MarketType": "spot",
                "SpotOptions": spot_options,
            }
        if subnet_id:
            network_interface: dict = {
                "DeviceIndex": 0,
                "SubnetId": subnet_id,
                "AssociatePublicIpAddress": True,
            }
            if self.settings.lab_security_group_id:
                network_interface["Groups"] = [self.settings.lab_security_group_id]
            params["NetworkInterfaces"] = [network_interface]
        elif self.settings.lab_security_group_id:
            params["SecurityGroupIds"] = [self.settings.lab_security_group_id]
        if self.settings.lab_key_name:
            params["KeyName"] = self.settings.lab_key_name
        if self.settings.lab_iam_instance_profile:
            params["IamInstanceProfile"] = {"Name": self.settings.lab_iam_instance_profile}
        return params

    def _minimum_root_volume_size_gb(self, ami_id: str) -> int:
        try:
            image = self.client.describe_images(ImageIds=[ami_id])["Images"][0]
        except Exception:
            return 30
        root_device_name = image.get("RootDeviceName")
        for mapping in image.get("BlockDeviceMappings", []):
            if mapping.get("DeviceName") == root_device_name and "Ebs" in mapping:
                return int(mapping["Ebs"].get("VolumeSize") or 30)
        return 30

    def _run_and_wait(self, params: dict, market: str) -> InstanceResult:
        instance = self.client.run_instances(**params)["Instances"][0]
        instance_id = instance["InstanceId"]
        running_waiter = self.client.get_waiter("instance_running")
        try:
            running_waiter.wait(
                InstanceIds=[instance_id],
                WaiterConfig={"Delay": 15, "MaxAttempts": 20 if market == "spot" else 40},
            )
        except WaiterError:
            if market == "spot":
                try:
                    described = self.client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
                    spot_request_id = described.get("SpotInstanceRequestId")
                    if spot_request_id:
                        self.client.cancel_spot_instance_requests(SpotInstanceRequestIds=[spot_request_id])
                    state = described.get("State", {}).get("Name")
                    if state not in {"terminated", "shutting-down"}:
                        self.client.terminate_instances(InstanceIds=[instance_id])
                except Exception:
                    log.warning("lab_spot_pending_cleanup_failed", instance_id=instance_id)
            raise
        described = self.client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        windows_hostname = self._windows_hostname_from_console(instance_id)
        return InstanceResult(
            instance_id=instance_id,
            private_ip=described.get("PrivateIpAddress"),
            public_ip=described.get("PublicIpAddress"),
            windows_hostname=windows_hostname,
            instance_type=described.get("InstanceType") or params["InstanceType"],
            market=market,
            spot_instance_request_id=described.get("SpotInstanceRequestId"),
        )

    def _launch_sync(
        self,
        ami_id,
        instance_type,
        username,
        password,
        display_name,
        batch_id,
        lab_id,
        budget_limit,
        idle_timeout_minutes,
        expiry_iso,
        instance_market=None,
        lab_type="windows",
        claude_profile_id=None,
    ):
        if self._spot_enabled(instance_market):
            last_error: Exception | None = None
            for candidate_type in self._spot_instance_types(instance_type):
                for subnet_id in self._lab_subnet_ids():
                    params = self._base_launch_params(
                        ami_id,
                        candidate_type,
                        username,
                        password,
                        display_name,
                        batch_id,
                        lab_id,
                        budget_limit,
                        idle_timeout_minutes,
                        expiry_iso,
                        "spot",
                        lab_type,
                        claude_profile_id,
                        subnet_id,
                    )
                    try:
                        result = self._run_and_wait(params, "spot")
                        log.info("lab_instance_launched", lab_id=lab_id, instance_id=result.instance_id, market="spot", instance_type=result.instance_type, subnet_id=subnet_id)
                        return result
                    except ClientError as exc:
                        last_error = exc
                        if not self._is_spot_capacity_error(exc):
                            raise
                        log.warning("lab_spot_launch_failed", lab_id=lab_id, instance_type=candidate_type, subnet_id=subnet_id, error=str(exc))
                    except WaiterError as exc:
                        last_error = exc
                        log.warning("lab_spot_launch_timeout", lab_id=lab_id, instance_type=candidate_type, subnet_id=subnet_id, error=str(exc))

            if not self.settings.lab_spot_fallback_to_on_demand:
                raise last_error or RuntimeError("Spot launch failed")
            log.warning("lab_spot_fallback_to_on_demand", lab_id=lab_id, requested_instance_type=instance_type)

        last_error = None
        for candidate_type in self._spot_instance_types(instance_type):
            for subnet_id in self._lab_subnet_ids():
                params = self._base_launch_params(
                    ami_id,
                    candidate_type,
                    username,
                    password,
                    display_name,
                    batch_id,
                    lab_id,
                    budget_limit,
                    idle_timeout_minutes,
                    expiry_iso,
                    "on-demand",
                    lab_type,
                    claude_profile_id,
                    subnet_id,
                )
                try:
                    result = self._run_and_wait(params, "on-demand")
                    log.info("lab_instance_launched", lab_id=lab_id, instance_id=result.instance_id, market="on-demand", instance_type=result.instance_type, subnet_id=subnet_id)
                    return result
                except ClientError as exc:
                    last_error = exc
                    if not self._is_spot_capacity_error(exc):
                        raise
                    log.warning("lab_on_demand_launch_failed", lab_id=lab_id, instance_type=candidate_type, subnet_id=subnet_id, error=str(exc))
        raise last_error or RuntimeError("On-Demand launch failed")

    def _windows_hostname_from_console(self, instance_id: str) -> str | None:
        try:
            output = self.client.get_console_output(InstanceId=instance_id, Latest=True).get("Output") or ""
        except Exception:
            return None
        match = re.search(r"HOSTNAME:\s*([A-Za-z0-9-]+)", output)
        return match.group(1) if match else None

    async def wait_windows_ready(self, instance_id: str, *, max_attempts: int = 90, delay_seconds: int = 10) -> str | None:
        return await asyncio.to_thread(self._wait_windows_ready_sync, instance_id, max_attempts, delay_seconds)

    def _wait_windows_ready_sync(self, instance_id: str, max_attempts: int, delay_seconds: int) -> str | None:
        last_output = ""
        for attempt in range(max_attempts):
            output = self.client.get_console_output(InstanceId=instance_id, Latest=True).get("Output") or ""
            if output:
                last_output = output
            if "Windows is Ready to use" in output or "CLOUDLAB_BOOTSTRAP_DONE" in output:
                match = re.search(r"HOSTNAME:\s*([A-Za-z0-9-]+)", output)
                return match.group(1) if match else None
            if attempt < max_attempts - 1:
                import time

                time.sleep(delay_seconds)

        recent_lines = [line.strip() for line in last_output.splitlines() if line.strip()][-6:]
        detail = " | ".join(recent_lines) if recent_lines else "no console output yet"
        minutes = round((max_attempts * delay_seconds) / 60)
        raise RuntimeError(f"Windows did not become ready within {minutes} minutes. Latest console output: {detail}")

    async def wait_claude_ready(self, instance_id: str, *, max_attempts: int = 60, delay_seconds: int = 10) -> None:
        await asyncio.to_thread(self._wait_claude_ready_sync, instance_id, max_attempts, delay_seconds)

    def _wait_claude_ready_sync(self, instance_id: str, max_attempts: int, delay_seconds: int) -> None:
        import time

        ssm = boto3.client(
            "ssm",
            region_name=self.region,
            config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1}),
        )
        commands = [
            "$ErrorActionPreference = 'SilentlyContinue'",
            "$known = @((Test-Path 'C:\\Users\\Administrator\\AppData\\Local\\Programs\\Claude\\Claude.exe'), (Test-Path 'C:\\Program Files\\Claude\\Claude.exe'), (Test-Path 'C:\\Program Files\\AnthropicClaude\\Claude.exe')) -contains $true",
            "$pkg = [bool](Get-ChildItem 'C:\\Program Files\\WindowsApps' -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue | Select-Object -First 1)",
            "$ready = Test-Path 'C:\\ProgramData\\CloudLab\\ClaudeReady.marker'",
            "$log = Test-Path 'C:\\ProgramData\\CloudLab\\ClaudeBootstrap.log'",
            "if (($known -or $pkg) -and $ready) { Write-Output 'CLAUDE_READY'; exit 0 }",
            "Write-Output ('known_exe=' + $known + '; appx_package=' + $pkg + '; ready_marker=' + $ready + '; bootstrap_log=' + $log)",
            "exit 42",
        ]
        last_output = "SSM is not online yet"
        for attempt in range(max_attempts):
            try:
                info = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
                items = info.get("InstanceInformationList") or []
                if not items or items[0].get("PingStatus") != "Online":
                    last_output = f"SSM status: {items[0].get('PingStatus') if items else 'not registered'}"
                    raise RuntimeError(last_output)

                response = ssm.send_command(
                    InstanceIds=[instance_id],
                    DocumentName="AWS-RunPowerShellScript",
                    Parameters={"commands": commands},
                )
                command_id = response["Command"]["CommandId"]
                invocation = None
                for _ in range(12):
                    try:
                        invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                            raise
                        time.sleep(2)
                        continue
                    if invocation.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                        break
                    time.sleep(2)
                if invocation and invocation.get("Status") == "Success" and "CLAUDE_READY" in (invocation.get("StandardOutputContent") or ""):
                    return
                if invocation:
                    last_output = (invocation.get("StandardOutputContent") or invocation.get("StandardErrorContent") or invocation.get("Status") or "").strip()
            except Exception as exc:
                last_output = str(exc)
            if attempt < max_attempts - 1:
                time.sleep(delay_seconds)

        minutes = round((max_attempts * delay_seconds) / 60)
        raise RuntimeError(f"Claude Desktop was not ready within {minutes} minutes. Latest check: {last_output}")

    async def terminate_instance(self, instance_id: str, *, lab_id: str) -> None:
        await asyncio.to_thread(self._terminate_sync, instance_id, lab_id)

    async def terminate_lab_resources(self, *, lab_id: str, primary_instance_id: str | None = None) -> list[str]:
        return await asyncio.to_thread(self._terminate_lab_resources_sync, lab_id, primary_instance_id)

    def _active_lab_instances(self, lab_id: str) -> list[dict]:
        response = self.client.describe_instances(
            Filters=[
                {"Name": "tag:cloudlab:lab_id", "Values": [lab_id]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]},
            ]
        )
        return [instance for reservation in response["Reservations"] for instance in reservation["Instances"]]

    def _terminate_lab_resources_sync(self, lab_id: str, primary_instance_id: str | None = None) -> list[str]:
        instances_by_id: dict[str, dict] = {}
        if primary_instance_id:
            try:
                primary = self._tagged_instance(primary_instance_id, lab_id)
                instances_by_id[primary_instance_id] = primary
            except RuntimeError as exc:
                if "was not found" not in str(exc):
                    raise

        for instance in self._active_lab_instances(lab_id):
            instances_by_id[instance["InstanceId"]] = instance

        instances = list(instances_by_id.values())
        spot_request_ids = [
            instance.get("SpotInstanceRequestId")
            for instance in instances
            if instance.get("SpotInstanceRequestId")
        ]
        if spot_request_ids:
            try:
                self.client.cancel_spot_instance_requests(SpotInstanceRequestIds=list(dict.fromkeys(spot_request_ids)))
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code not in {"InvalidSpotInstanceRequestID.NotFound", "InvalidSpotInstanceRequestID.Malformed"}:
                    raise

        terminate_ids = [
            instance["InstanceId"]
            for instance in instances
            if instance.get("State", {}).get("Name") not in {"terminated", "shutting-down"}
        ]
        if terminate_ids:
            self.client.terminate_instances(InstanceIds=terminate_ids)
            self.client.get_waiter("instance_terminated").wait(
                InstanceIds=terminate_ids,
                WaiterConfig={"Delay": 15, "MaxAttempts": 40},
            )

        self._delete_available_lab_volumes_sync(lab_id)
        return terminate_ids

    def _terminate_sync(self, instance_id: str, lab_id: str) -> None:
        try:
            instance = self._tagged_instance(instance_id, lab_id)
        except RuntimeError as exc:
            if "was not found" in str(exc):
                self._delete_available_lab_volumes_sync(lab_id)
                return
            raise
        state = instance.get("State", {}).get("Name")
        if state == "terminated":
            return
        spot_request_id = instance.get("SpotInstanceRequestId")
        if spot_request_id:
            try:
                self.client.cancel_spot_instance_requests(SpotInstanceRequestIds=[spot_request_id])
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code not in {"InvalidSpotInstanceRequestID.NotFound", "InvalidSpotInstanceRequestID.Malformed"}:
                    raise
        if state != "shutting-down":
            self.client.terminate_instances(InstanceIds=[instance_id])
        self.client.get_waiter("instance_terminated").wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 15, "MaxAttempts": 40},
        )
        self._delete_available_lab_volumes_sync(lab_id)

    async def delete_available_lab_volumes(self, *, lab_id: str) -> list[str]:
        return await asyncio.to_thread(self._delete_available_lab_volumes_sync, lab_id)

    def _delete_available_lab_volumes_sync(self, lab_id: str) -> list[str]:
        response = self.client.describe_volumes(
            Filters=[
                {"Name": "tag:cloudlab:lab_id", "Values": [lab_id]},
                {"Name": "status", "Values": ["available"]},
            ]
        )
        deleted: list[str] = []
        for volume in response.get("Volumes", []):
            volume_id = volume["VolumeId"]
            self.client.delete_volume(VolumeId=volume_id)
            deleted.append(volume_id)
        return deleted

    async def stop_instance(self, instance_id: str, *, lab_id: str) -> None:
        await asyncio.to_thread(self._stop_sync, instance_id, lab_id)

    def _stop_sync(self, instance_id: str, lab_id: str) -> None:
        instance = self._tagged_instance(instance_id, lab_id)
        state = instance.get("State", {}).get("Name")
        if state in {"stopped", "stopping"}:
            return
        if state == "terminated":
            raise RuntimeError(f"EC2 instance {instance_id} is already terminated")
        self.client.stop_instances(InstanceIds=[instance_id])
        self.client.get_waiter("instance_stopped").wait(InstanceIds=[instance_id])

    async def start_instance(self, instance_id: str, *, lab_id: str) -> InstanceResult:
        return await asyncio.to_thread(self._start_sync, instance_id, lab_id)

    def _start_sync(self, instance_id: str, lab_id: str) -> InstanceResult:
        instance = self._tagged_instance(instance_id, lab_id)
        state = instance.get("State", {}).get("Name")
        if state == "terminated":
            raise RuntimeError(f"EC2 instance {instance_id} is already terminated")
        if state == "stopping":
            self.client.get_waiter("instance_stopped").wait(InstanceIds=[instance_id])
            state = "stopped"
        if state not in {"running", "pending"}:
            self.client.start_instances(InstanceIds=[instance_id])
        self.client.get_waiter("instance_running").wait(InstanceIds=[instance_id])
        described = self.client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        return InstanceResult(
            instance_id=instance_id,
            private_ip=described.get("PrivateIpAddress"),
            public_ip=described.get("PublicIpAddress"),
            windows_hostname=self._windows_hostname_from_console(instance_id),
            instance_type=described.get("InstanceType"),
            market=described.get("InstanceLifecycle") or "on-demand",
            spot_instance_request_id=described.get("SpotInstanceRequestId"),
        )

    async def instance_state(self, instance_id: str, *, lab_id: str) -> str:
        return await asyncio.to_thread(self._instance_state_sync, instance_id, lab_id)

    def _instance_state_sync(self, instance_id: str, lab_id: str) -> str:
        try:
            return self._tagged_instance(instance_id, lab_id).get("State", {}).get("Name", "unknown")
        except RuntimeError as exc:
            if "was not found" in str(exc):
                return "terminated"
            raise

    async def update_instance_expiry_tag(self, instance_id: str, *, lab_id: str, expiry_iso: str) -> None:
        await asyncio.to_thread(self._update_instance_expiry_tag_sync, instance_id, lab_id, expiry_iso)

    def _update_instance_expiry_tag_sync(self, instance_id: str, lab_id: str, expiry_iso: str) -> None:
        self._tagged_instance(instance_id, lab_id)
        self.client.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": "cloudlab:expiry_time", "Value": expiry_iso}],
        )

    async def update_instance_budget_tag(self, instance_id: str, *, lab_id: str, budget_limit: float) -> None:
        await asyncio.to_thread(self._update_instance_budget_tag_sync, instance_id, lab_id, budget_limit)

    def _update_instance_budget_tag_sync(self, instance_id: str, lab_id: str, budget_limit: float) -> None:
        self._tagged_instance(instance_id, lab_id)
        self.client.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": "cloudlab:budget_limit", "Value": str(budget_limit)}],
        )

    def _tagged_instance(self, instance_id: str, lab_id: str) -> dict:
        try:
            described = self.client.describe_instances(InstanceIds=[instance_id])
        except ClientError as exc:
            if _is_instance_not_found(exc):
                raise RuntimeError(f"EC2 instance {instance_id} was not found") from exc
            raise
        instances = [instance for reservation in described["Reservations"] for instance in reservation["Instances"]]
        if not instances:
            raise RuntimeError(f"EC2 instance {instance_id} was not found")

        tags = {tag["Key"]: tag["Value"] for tag in instances[0].get("Tags", [])}
        if tags.get("cloudlab:lab_id") != lab_id:
            raise RuntimeError(f"Refusing to manage EC2 instance {instance_id}: missing matching cloudlab:lab_id tag")
        return instances[0]
