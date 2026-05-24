#!/usr/bin/env bash
set -euo pipefail

REGION="${REGION:-ap-south-1}"
SUBNET_ID="${SUBNET_ID:-}"
SECURITY_GROUP_ID="${SECURITY_GROUP_ID:-}"
BASE_AMI_ID="${BASE_AMI_ID:-ami-0b4859245e3b59166}"
INSTANCE_TYPE="${INSTANCE_TYPE:-c6a.xlarge}"
ROOT_VOLUME_SIZE="${ROOT_VOLUME_SIZE:-64}"
AMI_NAME="${AMI_NAME:-unext-win2022-devlab-full-icons-$(date -u +%Y%m%d%H%M%S)}"
BUILDER_TIMEOUT_MINUTES="${BUILDER_TIMEOUT_MINUTES:-45}"
PROJECT_TAG="${PROJECT_TAG:-UNextCloudLab}"
ENVIRONMENT_TAG="${ENVIRONMENT_TAG:-production}"

if [[ -z "$SUBNET_ID" || -z "$SECURITY_GROUP_ID" ]]; then
  echo "SUBNET_ID and SECURITY_GROUP_ID are required." >&2
  exit 1
fi

USER_DATA="$(mktemp)"
trap 'rm -f "$USER_DATA"' EXIT

cat > "$USER_DATA" <<'POWERSHELL'
<powershell>
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$LogDir = "C:\ProgramData\UNext"
$LabRoot = "C:\LabFiles"
$Desktop = "C:\Users\Public\Desktop"
$BinDir = "C:\ProgramData\chocolatey\bin"
New-Item -ItemType Directory -Force -Path $LogDir,$Desktop,$BinDir,"$LabRoot\Python","$LabRoot\Node","$LabRoot\React","$LabRoot\MongoDB" | Out-Null
Start-Transcript -Path (Join-Path $LogDir "golden-ami-icons-repair.log") -Append

function Add-MachinePath([string]$PathItem) {
  if (-not (Test-Path $PathItem)) { return }
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  if ($machinePath -notlike "*$PathItem*") {
    [Environment]::SetEnvironmentVariable("Path", "$machinePath;$PathItem", "Machine")
  }
}

function First-Path([string[]]$Paths) {
  return $Paths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function New-LabShortcut([string]$Name, [string]$Target, [string]$Arguments = "", [string]$WorkingDirectory = "") {
  if (-not $Target -or -not (Test-Path $Target)) { throw "Shortcut target missing for ${Name}: $Target" }
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut((Join-Path $Desktop $Name))
  $shortcut.TargetPath = $Target
  $shortcut.Arguments = $Arguments
  if ($WorkingDirectory) { $shortcut.WorkingDirectory = $WorkingDirectory }
  $shortcut.Save()
}

@(
  "C:\Program Files\Google\Chrome\Application",
  "C:\Program Files\Microsoft VS Code\bin",
  "C:\Program Files\nodejs",
  "C:\Program Files\mongosh",
  "$env:APPDATA\npm",
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

$ChromeMsi = "$env:TEMP\chrome-enterprise.msi"
try {
  Invoke-WebRequest "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi" -OutFile $ChromeMsi -UseBasicParsing
  Start-Process msiexec.exe -ArgumentList "/i `"$ChromeMsi`" /qn /norestart" -Wait
} catch {
  Write-Host "Chrome direct install warning: $($_.Exception.Message)"
}

try {
  npm install -g mongosh
} catch {
  Write-Host "mongosh npm install warning: $($_.Exception.Message)"
}

@(
  "C:\Program Files\Google\Chrome\Application",
  "$env:APPDATA\npm",
  "C:\ProgramData\chocolatey\bin"
) | ForEach-Object { Add-MachinePath $_ }
$env:Path = "$([Environment]::GetEnvironmentVariable("Path", "Machine"));$([Environment]::GetEnvironmentVariable("Path", "User"));$env:APPDATA\npm"

$Chrome = First-Path @("C:\Program Files\Google\Chrome\Application\chrome.exe","C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
$Code = First-Path @("C:\Program Files\Microsoft VS Code\Code.exe","$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe")
$PowerShell = First-Path @("$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe","C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

if ($Chrome -and (Test-Path $Chrome)) {
  "@echo off`r`n`"$Chrome`" %*" | Set-Content "$BinDir\chrome.cmd" -Encoding ASCII
}

Get-ChildItem -Path $Desktop,"C:\Users\Administrator\Desktop" -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match "Eclipse|Jupyter|Notebook|Anaconda|Conda|Server Manager|EC2|Amazon|Guide|Feedback|Readme" } |
  Remove-Item -Force -ErrorAction SilentlyContinue

New-LabShortcut "Google Chrome.lnk" $Chrome
New-LabShortcut "Google Colab.lnk" $Chrome "https://colab.research.google.com/"
New-LabShortcut "Visual Studio Code.lnk" $Code "`"$LabRoot`"" $LabRoot
New-LabShortcut "Node.js Terminal.lnk" $PowerShell "-NoProfile -NoExit -Command `"node --version; cd '$LabRoot\Node'`"" "$LabRoot\Node"
New-LabShortcut "npm Terminal.lnk" $PowerShell "-NoProfile -NoExit -Command `"npm --version; cd '$LabRoot\Node'`"" "$LabRoot\Node"
New-LabShortcut "Python 3 Shell.lnk" $PowerShell "-NoProfile -NoExit -Command `"python --version; cd '$LabRoot\Python'; python`"" "$LabRoot\Python"
New-LabShortcut "pip Terminal.lnk" $PowerShell "-NoProfile -NoExit -Command `"pip --version; cd '$LabRoot\Python'`"" "$LabRoot\Python"
New-LabShortcut "React Sample App.lnk" $PowerShell "-NoProfile -ExecutionPolicy Bypass -NoExit -Command `"cd '$LabRoot\React\sample-react-app'; npm run dev -- --host 127.0.0.1`"" "$LabRoot\React\sample-react-app"
New-LabShortcut "React Developer Terminal.lnk" $PowerShell "-NoProfile -NoExit -Command `"vite --version; cd '$LabRoot\React'`"" "$LabRoot\React"
New-LabShortcut "MongoDB Shell.lnk" $PowerShell "-NoProfile -NoExit -Command `"Start-Service MongoDB -ErrorAction SilentlyContinue; mongosh`"" "$LabRoot\MongoDB"
New-LabShortcut "MongoDB Service Status.lnk" $PowerShell "-NoProfile -NoExit -Command `"Get-Service MongoDB; mongosh --quiet --eval 'db.runCommand({ ping: 1 })'`"" "$LabRoot\MongoDB"

$folderShortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $Desktop "Lab Files.lnk"))
$folderShortcut.TargetPath = $LabRoot
$folderShortcut.Save()

$failed = @()
foreach ($cmd in @("chrome --version","code --version","python --version","pip --version","node --version","npm --version","vite --version","create-react-app --version","mongosh --version")) {
  Write-Host "CHECK: $cmd"
  cmd /c $cmd
  if ($LASTEXITCODE -ne 0) { $failed += $cmd }
}

try {
  Set-Service MongoDB -StartupType Automatic -ErrorAction Stop
  Start-Service MongoDB -ErrorAction SilentlyContinue
  $svc = Get-Service MongoDB -ErrorAction Stop
  if ($svc.Status -ne "Running") { $failed += "MongoDB service running" }
} catch { $failed += "MongoDB service" }

try { cmd /c "mongosh --quiet --eval `"db.runCommand({ ping: 1 }).ok`""; if ($LASTEXITCODE -ne 0) { $failed += "MongoDB ping" } } catch { $failed += "MongoDB ping" }
try { Invoke-WebRequest -UseBasicParsing -Uri "https://colab.research.google.com/" -TimeoutSec 30 | Out-Null } catch { $failed += "Google Colab access" }

foreach ($shortcut in @("Google Chrome.lnk","Google Colab.lnk","Visual Studio Code.lnk","Node.js Terminal.lnk","npm Terminal.lnk","Python 3 Shell.lnk","pip Terminal.lnk","React Sample App.lnk","React Developer Terminal.lnk","MongoDB Shell.lnk","MongoDB Service Status.lnk","Lab Files.lnk")) {
  if (-not (Test-Path (Join-Path $Desktop $shortcut))) { $failed += "Shortcut $shortcut" }
}

if ($failed.Count -gt 0) {
  Set-Content -Path (Join-Path $LogDir "GOLDEN_AMI_FAILED.txt") -Value ($failed -join "`n") -Encoding ASCII
  Write-Host "UNEXT_GOLDEN_AMI_FAILED: $($failed -join ', ')"
} else {
  New-Item -ItemType File -Force -Path (Join-Path $LogDir "GOLDEN_AMI_READY.txt") | Out-Null
  Write-Host "UNEXT_GOLDEN_AMI_READY"
}

Stop-Transcript
Stop-Computer -Force
</powershell>
POWERSHELL

INSTANCE_ID="$(aws ec2 run-instances \
  --region "$REGION" \
  --image-id "$BASE_AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --network-interfaces "DeviceIndex=0,SubnetId=$SUBNET_ID,Groups=[$SECURITY_GROUP_ID],AssociatePublicIpAddress=true" \
  --user-data "file://$USER_DATA" \
  --instance-initiated-shutdown-behavior stop \
  --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$ROOT_VOLUME_SIZE,VolumeType=gp3,Iops=3000,Throughput=125,DeleteOnTermination=true}" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$AMI_NAME-builder},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-ami-icons-repair},{Key=ManagedBy,Value=cloud-lab-platform}]" "ResourceType=volume,Tags=[{Key=Name,Value=$AMI_NAME-builder-root},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-ami-icons-repair},{Key=ManagedBy,Value=cloud-lab-platform},{Key=Disk,Value=64GB-gp3}]" \
  --query 'Instances[0].InstanceId' \
  --output text)"

echo "Repair builder instance: $INSTANCE_ID"
deadline=$((SECONDS + BUILDER_TIMEOUT_MINUTES * 60))
while (( SECONDS < deadline )); do
  STATE="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' --output text)"
  echo "$(date -u +%H:%M:%S) state=$STATE"
  [[ "$STATE" == "stopped" ]] && break
  sleep 30
done

if [[ "${STATE:-}" != "stopped" ]]; then
  echo "Repair builder timed out; leaving running for inspection: $INSTANCE_ID" >&2
  exit 2
fi

AMI_ID="$(aws ec2 create-image \
  --region "$REGION" \
  --instance-id "$INSTANCE_ID" \
  --name "$AMI_NAME" \
  --description "UNext Windows lab AMI repaired with explicit desktop icons, PATH checks, MongoDB, React, VS Code, Node, Python, Chrome, Colab" \
  --no-reboot \
  --tag-specifications "ResourceType=image,Tags=[{Key=Name,Value=$AMI_NAME},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-windows-lab},{Key=ManagedBy,Value=cloud-lab-platform},{Key=OS,Value=Windows_Server_2022},{Key=RAM,Value=8GB},{Key=vCPU,Value=4},{Key=Disk,Value=64GB-gp3},{Key=Software,Value=vscode-nodejs-npm-python3-pip-mongodb-react-chrome-colab-full-icons}]" "ResourceType=snapshot,Tags=[{Key=Name,Value=$AMI_NAME-root},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-windows-lab},{Key=ManagedBy,Value=cloud-lab-platform},{Key=Disk,Value=64GB-gp3}]" \
  --query 'ImageId' \
  --output text)"

echo "Created repaired AMI: $AMI_ID"
aws ec2 wait image-available --region "$REGION" --image-ids "$AMI_ID"
aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null
echo "Golden Windows lab AMI with full icons is ready: $AMI_ID"
