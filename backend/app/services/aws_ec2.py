import asyncio
from dataclasses import dataclass
from html import escape
import re

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import get_settings

log = structlog.get_logger()


@dataclass
class InstanceResult:
    instance_id: str
    private_ip: str | None
    public_ip: str | None
    windows_hostname: str | None = None
    instance_type: str | None = None
    market: str = "on-demand"


def windows_user_data(username: str, password: str, idle_timeout_minutes: int, admin_username: str = "Administrator") -> str:
    def ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = f"""
$ErrorActionPreference = 'Continue'
$Username = {ps_literal(username)}
$AdminUsername = {ps_literal(admin_username)}
$PlainPassword = {ps_literal(password)}
$IdleLimitMinutes = {idle_timeout_minutes}
$Password = ConvertTo-SecureString -String $PlainPassword -AsPlainText -Force

Start-Transcript -Path 'C:\\ProgramData\\Amazon\\EC2-Windows\\Launch\\Log\\cloudlab-bootstrap.log' -Append
net user $AdminUsername $PlainPassword /active:yes
Set-LocalUser -Name $AdminUsername -Password $Password -ErrorAction Continue
Enable-LocalUser -Name $AdminUsername -ErrorAction Continue

if (-not (Get-LocalUser -Name $Username -ErrorAction SilentlyContinue)) {{
    New-LocalUser -Name $Username -Password $Password -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires
}} else {{
    Set-LocalUser -Name $Username -Password $Password -ErrorAction Continue
}}
Enable-LocalUser -Name $Username -ErrorAction Continue
Add-LocalGroupMember -Group 'Administrators' -Member $Username -ErrorAction SilentlyContinue
Add-LocalGroupMember -Group 'Remote Desktop Users' -Member $Username -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' -Name fDenyTSConnections -Value 0
Set-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp' -Name UserAuthentication -Value 1
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction Continue
Restart-Service TermService -Force -ErrorAction Continue
powercfg /hibernate off
powercfg /setactive SCHEME_MAX
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
Set-TimeZone -Id 'India Standard Time' -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\ServerManager' -Name DoNotOpenServerManagerAtLogon -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' -Name LongPathsEnabled -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue

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
    return f"<persist>true</persist>\n<powershell>\n{escape(script, quote=False)}\n</powershell>"


class AwsEc2Service:
    def __init__(self, region: str):
        self.region = region
        self.settings = get_settings()
        self.client = boto3.client(
            "ec2",
            region_name=region,
            config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1}),
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
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
    ):
        minimum_root_volume_size_gb = self._minimum_root_volume_size_gb(ami_id)
        root_volume_size_gb = max(int(self.settings.lab_root_volume_size_gb), minimum_root_volume_size_gb, 30)
        root_disk_tag = f"{root_volume_size_gb}GB-gp3"
        params = {
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "InstanceInitiatedShutdownBehavior": "terminate" if market == "spot" else "stop",
            "UserData": windows_user_data(username, password, idle_timeout_minutes, self.settings.windows_admin_user),
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
                        {"Key": "RAM", "Value": "8GB"},
                        {"Key": "vCPU", "Value": "4"},
                        {"Key": "Disk", "Value": root_disk_tag},
                        {"Key": "Software", "Value": "vscode-nodejs-pip-python3-mongodb-react-chrome-colab"},
                        {"Key": "cloudlab:owner", "Value": username},
                        {"Key": "cloudlab:user_label", "Value": display_name},
                        {"Key": "cloudlab:batch_id", "Value": batch_id},
                        {"Key": "cloudlab:lab_id", "Value": lab_id},
                        {"Key": "cloudlab:expiry_time", "Value": expiry_iso},
                        {"Key": "cloudlab:budget_limit", "Value": str(budget_limit)},
                        {"Key": "cloudlab:instance_market", "Value": market},
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
                    ],
                },
            ],
        }
        if market == "spot":
            spot_options = {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            }
            if self.settings.lab_spot_max_price:
                spot_options["MaxPrice"] = self.settings.lab_spot_max_price
            params["InstanceMarketOptions"] = {
                "MarketType": "spot",
                "SpotOptions": spot_options,
            }
        if self.settings.lab_subnet_id:
            network_interface: dict = {
                "DeviceIndex": 0,
                "SubnetId": self.settings.lab_subnet_id,
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
        running_waiter.wait(InstanceIds=[instance_id])
        status_waiter = self.client.get_waiter("instance_status_ok")
        status_waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40})
        described = self.client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        windows_hostname = self._windows_hostname_from_console(instance_id)
        return InstanceResult(
            instance_id=instance_id,
            private_ip=described.get("PrivateIpAddress"),
            public_ip=described.get("PublicIpAddress"),
            windows_hostname=windows_hostname,
            instance_type=described.get("InstanceType") or params["InstanceType"],
            market=market,
        )

    def _launch_sync(self, ami_id, instance_type, username, password, display_name, batch_id, lab_id, budget_limit, idle_timeout_minutes, expiry_iso, instance_market=None):
        if self._spot_enabled(instance_market):
            last_error: Exception | None = None
            for candidate_type in self._spot_instance_types(instance_type):
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
                )
                try:
                    result = self._run_and_wait(params, "spot")
                    log.info("lab_instance_launched", lab_id=lab_id, instance_id=result.instance_id, market="spot", instance_type=result.instance_type)
                    return result
                except ClientError as exc:
                    last_error = exc
                    if not self._is_spot_capacity_error(exc):
                        raise
                    log.warning("lab_spot_launch_failed", lab_id=lab_id, instance_type=candidate_type, error=str(exc))

            if not self.settings.lab_spot_fallback_to_on_demand:
                raise last_error or RuntimeError("Spot launch failed")
            log.warning("lab_spot_fallback_to_on_demand", lab_id=lab_id, requested_instance_type=instance_type)

        params = self._base_launch_params(
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
            "on-demand",
        )
        result = self._run_and_wait(params, "on-demand")
        log.info("lab_instance_launched", lab_id=lab_id, instance_id=result.instance_id, market="on-demand", instance_type=result.instance_type)
        return result

    def _windows_hostname_from_console(self, instance_id: str) -> str | None:
        try:
            output = self.client.get_console_output(InstanceId=instance_id, Latest=True).get("Output") or ""
        except Exception:
            return None
        match = re.search(r"HOSTNAME:\s*([A-Za-z0-9-]+)", output)
        return match.group(1) if match else None

    async def terminate_instance(self, instance_id: str, *, lab_id: str) -> None:
        await asyncio.to_thread(self._terminate_sync, instance_id, lab_id)

    def _terminate_sync(self, instance_id: str, lab_id: str) -> None:
        described = self.client.describe_instances(InstanceIds=[instance_id])
        instances = [instance for reservation in described["Reservations"] for instance in reservation["Instances"]]
        if not instances:
            raise RuntimeError(f"EC2 instance {instance_id} was not found")

        tags = {tag["Key"]: tag["Value"] for tag in instances[0].get("Tags", [])}
        if tags.get("cloudlab:lab_id") != lab_id:
            raise RuntimeError(f"Refusing to terminate EC2 instance {instance_id}: missing matching cloudlab:lab_id tag")

        self.client.terminate_instances(InstanceIds=[instance_id])
        self.client.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])

    async def stop_instance(self, instance_id: str, *, lab_id: str) -> None:
        await asyncio.to_thread(self._stop_sync, instance_id, lab_id)

    def _stop_sync(self, instance_id: str, lab_id: str) -> None:
        instance = self._tagged_instance(instance_id, lab_id)
        market = instance.get("InstanceLifecycle") or "on-demand"
        if market == "spot":
            raise RuntimeError("Spot labs use one-time terminate behavior and cannot be stopped")
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
        market = instance.get("InstanceLifecycle") or "on-demand"
        if market == "spot":
            raise RuntimeError("Spot labs use one-time terminate behavior and cannot be restarted")
        state = instance.get("State", {}).get("Name")
        if state == "terminated":
            raise RuntimeError(f"EC2 instance {instance_id} is already terminated")
        if state == "stopping":
            self.client.get_waiter("instance_stopped").wait(InstanceIds=[instance_id])
            state = "stopped"
        if state not in {"running", "pending"}:
            self.client.start_instances(InstanceIds=[instance_id])
        self.client.get_waiter("instance_running").wait(InstanceIds=[instance_id])
        self.client.get_waiter("instance_status_ok").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40})
        described = self.client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        return InstanceResult(
            instance_id=instance_id,
            private_ip=described.get("PrivateIpAddress"),
            public_ip=described.get("PublicIpAddress"),
            windows_hostname=self._windows_hostname_from_console(instance_id),
            instance_type=described.get("InstanceType"),
            market=market,
        )

    async def instance_state(self, instance_id: str, *, lab_id: str) -> str:
        return await asyncio.to_thread(self._instance_state_sync, instance_id, lab_id)

    def _instance_state_sync(self, instance_id: str, lab_id: str) -> str:
        return self._tagged_instance(instance_id, lab_id).get("State", {}).get("Name", "unknown")

    async def update_instance_expiry_tag(self, instance_id: str, *, lab_id: str, expiry_iso: str) -> None:
        await asyncio.to_thread(self._update_instance_expiry_tag_sync, instance_id, lab_id, expiry_iso)

    def _update_instance_expiry_tag_sync(self, instance_id: str, lab_id: str, expiry_iso: str) -> None:
        self._tagged_instance(instance_id, lab_id)
        self.client.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": "cloudlab:expiry_time", "Value": expiry_iso}],
        )

    def _tagged_instance(self, instance_id: str, lab_id: str) -> dict:
        described = self.client.describe_instances(InstanceIds=[instance_id])
        instances = [instance for reservation in described["Reservations"] for instance in reservation["Instances"]]
        if not instances:
            raise RuntimeError(f"EC2 instance {instance_id} was not found")

        tags = {tag["Key"]: tag["Value"] for tag in instances[0].get("Tags", [])}
        if tags.get("cloudlab:lab_id") != lab_id:
            raise RuntimeError(f"Refusing to manage EC2 instance {instance_id}: missing matching cloudlab:lab_id tag")
        return instances[0]
