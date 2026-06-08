#!/usr/bin/env bash
set -euo pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REGION="${REGION:-ap-south-1}"
BASE_AMI_ID="${BASE_AMI_ID:-ami-06e8bc8e415e16e9f}"
SUBNET_ID="${SUBNET_ID:-subnet-07d66f84d93d4cc61}"
SECURITY_GROUP_ID="${SECURITY_GROUP_ID:-sg-0119bbd47e40dca58}"
INSTANCE_PROFILE_NAME="${INSTANCE_PROFILE_NAME:-cloud-lab-windows-instance-profile}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.xlarge}"
ROOT_VOLUME_SIZE="${ROOT_VOLUME_SIZE:-64}"
AMI_NAME="${AMI_NAME:-unext-win2022-chatgpt-java-single-terminal-$(date -u +%Y%m%d%H%M%S)}"
BUILDER_TIMEOUT_MINUTES="${BUILDER_TIMEOUT_MINUTES:-90}"
PROJECT_TAG="${PROJECT_TAG:-UNextCloudLab}"
ENVIRONMENT_TAG="${ENVIRONMENT_TAG:-production}"
OUTPUT_FILE="${OUTPUT_FILE:-$ROOT_DIR/ami/windows-lab/build-chatgpt-java-output.env}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || { echo "$1 is required." >&2; exit 1; }
}

require_command aws
require_command base64
require_command gzip

REPAIR_B64="$(gzip -c "$ROOT_DIR/scripts/repair_windows_lab_apps.ps1" | base64 -w0)"
USER_DATA="$(mktemp)"
if command -v cygpath >/dev/null 2>&1; then
  USER_DATA_PARAM="file://$(cygpath -w "$USER_DATA")"
else
  USER_DATA_PARAM="file://$USER_DATA"
fi
trap 'rm -f "$USER_DATA"' EXIT

cat > "$USER_DATA" <<POWERSHELL
<powershell>
\$ErrorActionPreference = "Stop"
\$RepairRoot = "C:\ProgramData\CloudLab"
\$RepairPath = Join-Path \$RepairRoot "repair_windows_lab_apps.ps1"
New-Item -ItemType Directory -Force -Path \$RepairRoot | Out-Null
function Expand-GzipBase64([string]\$Value, [string]\$Path) {
  \$bytes = [Convert]::FromBase64String(\$Value)
  \$inputStream = New-Object IO.MemoryStream(,\$bytes)
  \$gzipStream = New-Object IO.Compression.GzipStream(\$inputStream, [IO.Compression.CompressionMode]::Decompress)
  \$outputStream = New-Object IO.MemoryStream
  \$gzipStream.CopyTo(\$outputStream)
  [IO.File]::WriteAllBytes(\$Path, \$outputStream.ToArray())
  \$gzipStream.Dispose()
  \$inputStream.Dispose()
  \$outputStream.Dispose()
}
Expand-GzipBase64 "$REPAIR_B64" \$RepairPath
\$process = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",\$RepairPath) -Wait -PassThru
if (\$process.ExitCode -ne 0) { exit \$process.ExitCode }
Stop-Computer -Force
</powershell>
POWERSHELL

echo "Starting non-disruptive Windows AMI repair builder"
echo "Base AMI: $BASE_AMI_ID"
echo "Builder subnet: $SUBNET_ID"
echo "Builder security group: $SECURITY_GROUP_ID"
echo "AMI name: $AMI_NAME"

ROOT_DEVICE="$(aws ec2 describe-images \
  --region "$REGION" \
  --image-ids "$BASE_AMI_ID" \
  --query 'Images[0].RootDeviceName' \
  --output text)"

INSTANCE_ID="$(aws ec2 run-instances \
  --region "$REGION" \
  --image-id "$BASE_AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --iam-instance-profile "Name=$INSTANCE_PROFILE_NAME" \
  --network-interfaces "DeviceIndex=0,SubnetId=$SUBNET_ID,Groups=[$SECURITY_GROUP_ID],AssociatePublicIpAddress=true" \
  --user-data "$USER_DATA_PARAM" \
  --instance-initiated-shutdown-behavior stop \
  --block-device-mappings "DeviceName=$ROOT_DEVICE,Ebs={VolumeSize=$ROOT_VOLUME_SIZE,VolumeType=gp3,Iops=3000,Throughput=125,DeleteOnTermination=true}" \
  --tag-specifications \
    "ResourceType=instance,Tags=[{Key=Name,Value=$AMI_NAME-builder},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-chatgpt-java-ami-builder},{Key=ManagedBy,Value=cloud-lab-platform}]" \
    "ResourceType=volume,Tags=[{Key=Name,Value=$AMI_NAME-builder-root},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-chatgpt-java-ami-builder},{Key=ManagedBy,Value=cloud-lab-platform},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-gp3}]" \
  --query 'Instances[0].InstanceId' \
  --output text)"

echo "Builder instance ID: $INSTANCE_ID"

deadline=$((SECONDS + BUILDER_TIMEOUT_MINUTES * 60))
STATE=""
while (( SECONDS < deadline )); do
  STATE="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' --output text)"
  echo "$(date -u +%H:%M:%S) state=$STATE"
  [[ "$STATE" == "stopped" ]] && break
  sleep 30
done

if [[ "$STATE" != "stopped" ]]; then
  echo "Builder timed out. Leaving it for inspection: $INSTANCE_ID" >&2
  exit 3
fi

CONSOLE_OUTPUT=""
for _ in {1..12}; do
  CONSOLE_OUTPUT="$(aws ec2 get-console-output --region "$REGION" --instance-id "$INSTANCE_ID" --latest --query 'Output' --output text 2>/dev/null || true)"
  if grep -q "CLOUDLAB_DESKTOP_APPS_REPAIR_READY" <<<"$CONSOLE_OUTPUT"; then
    break
  fi
  sleep 15
done

if grep -q "CLOUDLAB_DESKTOP_APPS_REPAIR_FAILED" <<<"$CONSOLE_OUTPUT"; then
  echo "$CONSOLE_OUTPUT" | tail -160 >&2
  echo "Repair validation failed. Leaving builder instance for inspection: $INSTANCE_ID" >&2
  exit 4
fi

if ! grep -q "CLOUDLAB_DESKTOP_APPS_REPAIR_READY" <<<"$CONSOLE_OUTPUT"; then
  echo "Did not find repair readiness marker. Leaving builder instance for inspection: $INSTANCE_ID" >&2
  exit 5
fi

AMI_ID="$(aws ec2 create-image \
  --region "$REGION" \
  --instance-id "$INSTANCE_ID" \
  --name "$AMI_NAME" \
  --description "UNext Windows lab AMI from $BASE_AMI_ID with ChatGPT, Java 21 JDK, Claude shortcut refresh, and single Terminal desktop shortcut" \
  --no-reboot \
  --tag-specifications \
    "ResourceType=image,Tags=[{Key=Name,Value=$AMI_NAME},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-windows-lab},{Key=ManagedBy,Value=cloud-lab-platform},{Key=BaseAmi,Value=$BASE_AMI_ID},{Key=Software,Value=chatgpt-java21-claude-single-terminal}]" \
    "ResourceType=snapshot,Tags=[{Key=Name,Value=$AMI_NAME-root},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-windows-lab},{Key=ManagedBy,Value=cloud-lab-platform},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-gp3}]" \
  --query 'ImageId' \
  --output text)"

echo "Created AMI: $AMI_ID"
aws ec2 wait image-available --region "$REGION" --image-ids "$AMI_ID"
aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null

cat > "$OUTPUT_FILE" <<EOF
AMI_ID=$AMI_ID
AMI_NAME=$AMI_NAME
REGION=$REGION
BASE_AMI_ID=$BASE_AMI_ID
INSTANCE_TYPE=$INSTANCE_TYPE
ROOT_VOLUME_SIZE=$ROOT_VOLUME_SIZE
EOF

echo "Repaired Windows lab AMI is ready: $AMI_ID"
echo "Output written to $OUTPUT_FILE"
