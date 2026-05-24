#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AMI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

REGION="${REGION:-ap-south-1}"
SUBNET_ID="${SUBNET_ID:-}"
SECURITY_GROUP_ID="${SECURITY_GROUP_ID:-}"
INSTANCE_TYPE="${INSTANCE_TYPE:-c6a.xlarge}"
ROOT_VOLUME_SIZE="${ROOT_VOLUME_SIZE:-64}"
ROOT_VOLUME_TYPE="${ROOT_VOLUME_TYPE:-gp3}"
ROOT_VOLUME_IOPS="${ROOT_VOLUME_IOPS:-3000}"
ROOT_VOLUME_THROUGHPUT="${ROOT_VOLUME_THROUGHPUT:-125}"
BASE_AMI_ID="${BASE_AMI_ID:-}"
AMI_NAME="${AMI_NAME:-unext-win2022-devlab-vscode-node-python-mongo-react-chrome-$(date -u +%Y%m%d%H%M%S)}"
BUILDER_TIMEOUT_MINUTES="${BUILDER_TIMEOUT_MINUTES:-120}"
PROJECT_TAG="${PROJECT_TAG:-UNextCloudLab}"
ENVIRONMENT_TAG="${ENVIRONMENT_TAG:-production}"
MANAGED_BY_TAG="${MANAGED_BY_TAG:-cloud-lab-platform}"
TERMINATE_ON_FAILURE="${TERMINATE_ON_FAILURE:-false}"
SKIP_SYSPREP="${SKIP_SYSPREP:-false}"
OUTPUT_FILE="${OUTPUT_FILE:-$AMI_DIR/build-output.env}"

if [[ -z "$SUBNET_ID" || -z "$SECURITY_GROUP_ID" ]]; then
  echo "SUBNET_ID and SECURITY_GROUP_ID are required." >&2
  exit 1
fi

require_command() {
  command -v "$1" >/dev/null 2>&1 || { echo "$1 is required." >&2; exit 1; }
}

require_command aws
require_command base64
require_command gzip

if [[ -z "$BASE_AMI_ID" ]]; then
  BASE_AMI_ID="$(aws ec2 describe-images \
    --region "$REGION" \
    --owners amazon \
    --filters "Name=name,Values=Windows_Server-2022-English-Full-Base-*" "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
    --output text)"
fi

ROOT_DEVICE="$(aws ec2 describe-images \
  --region "$REGION" \
  --image-ids "$BASE_AMI_ID" \
  --query 'Images[0].RootDeviceName' \
  --output text)"

BASE_ROOT_SIZE="$(aws ec2 describe-images \
  --region "$REGION" \
  --image-ids "$BASE_AMI_ID" \
  --query "Images[0].BlockDeviceMappings[?DeviceName=='$ROOT_DEVICE']|[0].Ebs.VolumeSize" \
  --output text)"

if [[ "$BASE_ROOT_SIZE" != "None" && "$BASE_ROOT_SIZE" -gt "$ROOT_VOLUME_SIZE" ]]; then
  echo "Base AMI $BASE_AMI_ID root snapshot is ${BASE_ROOT_SIZE}GB, which cannot be shrunk to ${ROOT_VOLUME_SIZE}GB." >&2
  echo "Use a smaller official Windows base AMI or set ROOT_VOLUME_SIZE >= $BASE_ROOT_SIZE." >&2
  exit 2
fi

PROVISION_B64="$(gzip -c "$SCRIPT_DIR/provision-windows-lab.ps1" | base64 -w0)"
VALIDATE_B64="$(gzip -c "$SCRIPT_DIR/validate-windows-lab.ps1" | base64 -w0)"
USER_DATA="$(mktemp)"
trap 'rm -f "$USER_DATA"' EXIT

cat > "$USER_DATA" <<POWERSHELL
<powershell>
\$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path "C:\ProgramData\UNext" | Out-Null
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
Expand-GzipBase64 "$PROVISION_B64" "C:\ProgramData\UNext\provision-windows-lab.ps1"
Expand-GzipBase64 "$VALIDATE_B64" "C:\ProgramData\UNext\validate-windows-lab.ps1"
\$argsList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\ProgramData\UNext\provision-windows-lab.ps1")
if ("$SKIP_SYSPREP" -eq "true") { \$argsList += "-SkipSysprep" }
\$process = Start-Process -FilePath "powershell.exe" -ArgumentList \$argsList -Wait -PassThru
exit \$process.ExitCode
</powershell>
POWERSHELL

echo "Starting Windows AMI builder"
echo "Region: $REGION"
echo "Base AMI: $BASE_AMI_ID"
echo "Builder instance: $INSTANCE_TYPE"
echo "Root volume: ${ROOT_VOLUME_SIZE}GB $ROOT_VOLUME_TYPE"
echo "AMI name: $AMI_NAME"

INSTANCE_ID="$(aws ec2 run-instances \
  --region "$REGION" \
  --image-id "$BASE_AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --network-interfaces "DeviceIndex=0,SubnetId=$SUBNET_ID,Groups=[$SECURITY_GROUP_ID],AssociatePublicIpAddress=true" \
  --user-data "file://$USER_DATA" \
  --instance-initiated-shutdown-behavior stop \
  --block-device-mappings "DeviceName=$ROOT_DEVICE,Ebs={VolumeSize=$ROOT_VOLUME_SIZE,VolumeType=$ROOT_VOLUME_TYPE,Iops=$ROOT_VOLUME_IOPS,Throughput=$ROOT_VOLUME_THROUGHPUT,DeleteOnTermination=true}" \
  --tag-specifications \
    "ResourceType=instance,Tags=[{Key=Name,Value=$AMI_NAME-builder},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-ami-builder},{Key=ManagedBy,Value=$MANAGED_BY_TAG},{Key=OS,Value=Windows_Server_2022},{Key=RAM,Value=8GB},{Key=vCPU,Value=4},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-$ROOT_VOLUME_TYPE},{Key=Software,Value=vscode-nodejs-npm-python3-pip-mongodb-react-chrome-colab}]" \
    "ResourceType=volume,Tags=[{Key=Name,Value=$AMI_NAME-builder-root},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-ami-builder},{Key=ManagedBy,Value=$MANAGED_BY_TAG},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-$ROOT_VOLUME_TYPE}]" \
  --query 'Instances[0].InstanceId' \
  --output text)"

echo "Builder instance ID: $INSTANCE_ID"

cleanup_failure() {
  if [[ "$TERMINATE_ON_FAILURE" == "true" && -n "${INSTANCE_ID:-}" ]]; then
    aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null || true
  fi
}
trap cleanup_failure ERR

deadline=$((SECONDS + BUILDER_TIMEOUT_MINUTES * 60))
STATE=""
while (( SECONDS < deadline )); do
  STATE="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' --output text)"
  echo "$(date -u +%H:%M:%S) state=$STATE"
  [[ "$STATE" == "stopped" ]] && break
  sleep 30
done

if [[ "$STATE" != "stopped" ]]; then
  echo "Builder timed out. Leaving instance for inspection: $INSTANCE_ID" >&2
  exit 3
fi

CONSOLE_OUTPUT=""
for _ in {1..10}; do
  CONSOLE_OUTPUT="$(aws ec2 get-console-output --region "$REGION" --instance-id "$INSTANCE_ID" --latest --query 'Output' --output text 2>/dev/null || true)"
  if grep -q "UNEXT_GOLDEN_AMI_READY" <<<"$CONSOLE_OUTPUT"; then
    break
  fi
  sleep 15
done

if grep -q "UNEXT_GOLDEN_AMI_FAILED" <<<"$CONSOLE_OUTPUT"; then
  echo "$CONSOLE_OUTPUT" | tail -120 >&2
  echo "Validation failed. Leaving builder instance for inspection: $INSTANCE_ID" >&2
  exit 4
fi

if ! grep -q "UNEXT_GOLDEN_AMI_READY" <<<"$CONSOLE_OUTPUT"; then
  echo "Did not find readiness marker in console output." >&2
  echo "Check C:\\ProgramData\\UNext\\golden-ami-build.log on builder instance $INSTANCE_ID." >&2
  exit 5
fi

AMI_ID="$(aws ec2 create-image \
  --region "$REGION" \
  --instance-id "$INSTANCE_ID" \
  --name "$AMI_NAME" \
  --description "UNext Windows developer lab AMI: 8GB/4vCPU/${ROOT_VOLUME_SIZE}GB, VS Code, Node.js/npm, Python/pip, MongoDB, React, Chrome, Colab access" \
  --no-reboot \
  --tag-specifications \
    "ResourceType=image,Tags=[{Key=Name,Value=$AMI_NAME},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-windows-lab},{Key=ManagedBy,Value=$MANAGED_BY_TAG},{Key=OS,Value=Windows_Server_2022},{Key=RAM,Value=8GB},{Key=vCPU,Value=4},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-$ROOT_VOLUME_TYPE},{Key=Software,Value=vscode-nodejs-npm-python3-pip-mongodb-react-chrome-colab}]" \
    "ResourceType=snapshot,Tags=[{Key=Name,Value=$AMI_NAME-root},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-golden-windows-lab},{Key=ManagedBy,Value=$MANAGED_BY_TAG},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-$ROOT_VOLUME_TYPE}]" \
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

echo "Golden Windows lab AMI is ready: $AMI_ID"
echo "Output written to $OUTPUT_FILE"
