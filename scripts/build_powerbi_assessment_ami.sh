#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETUP_SCRIPT="$ROOT_DIR/scripts/setup_powerbi_assessment_vm.ps1"

REGION="${REGION:-ap-south-1}"
SUBNET_ID="${SUBNET_ID:-}"
SECURITY_GROUP_ID="${SECURITY_GROUP_ID:-}"
INSTANCE_TYPE="${INSTANCE_TYPE:-c6a.xlarge}"
ROOT_VOLUME_SIZE="${ROOT_VOLUME_SIZE:-80}"
ROOT_VOLUME_TYPE="${ROOT_VOLUME_TYPE:-gp3}"
ROOT_VOLUME_IOPS="${ROOT_VOLUME_IOPS:-3000}"
ROOT_VOLUME_THROUGHPUT="${ROOT_VOLUME_THROUGHPUT:-125}"
BASE_AMI_ID="${BASE_AMI_ID:-}"
AMI_NAME="${AMI_NAME:-UNext-PowerBI-Assessment-AMI-v1}"
BUILDER_TIMEOUT_MINUTES="${BUILDER_TIMEOUT_MINUTES:-240}"
PROJECT_TAG="${PROJECT_TAG:-UNextCloudLab}"
ENVIRONMENT_TAG="${ENVIRONMENT_TAG:-production}"
MANAGED_BY_TAG="${MANAGED_BY_TAG:-cloud-lab-platform}"
TERMINATE_ON_FAILURE="${TERMINATE_ON_FAILURE:-false}"
OUTPUT_FILE="${OUTPUT_FILE:-$ROOT_DIR/powerbi-assessment-ami-output.env}"

if [[ -z "$SUBNET_ID" || -z "$SECURITY_GROUP_ID" ]]; then
  echo "SUBNET_ID and SECURITY_GROUP_ID are required." >&2
  exit 1
fi

command -v aws >/dev/null 2>&1 || { echo "aws is required." >&2; exit 1; }
command -v gzip >/dev/null 2>&1 || { echo "gzip is required." >&2; exit 1; }
command -v base64 >/dev/null 2>&1 || { echo "base64 is required." >&2; exit 1; }

if base64 --help 2>&1 | grep -q -- "-w"; then
  BASE64_WRAP_ARGS=(-w0)
else
  BASE64_WRAP_ARGS=()
fi

if [[ ! -f "$SETUP_SCRIPT" ]]; then
  echo "Missing setup script: $SETUP_SCRIPT" >&2
  exit 1
fi

if aws ec2 describe-images \
  --region "$REGION" \
  --owners self \
  --filters "Name=name,Values=$AMI_NAME" "Name=state,Values=available,pending" \
  --query 'Images[0].ImageId' \
  --output text | grep -qv '^None$'; then
  echo "An AMI named $AMI_NAME already exists or is pending. Set AMI_NAME to a new value or deregister the old AMI." >&2
  exit 2
fi

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
  echo "Base AMI $BASE_AMI_ID root snapshot is ${BASE_ROOT_SIZE}GB; cannot shrink to ${ROOT_VOLUME_SIZE}GB." >&2
  exit 3
fi

SETUP_B64="$(gzip -c "$SETUP_SCRIPT" | base64 "${BASE64_WRAP_ARGS[@]}" | tr -d '\r\n')"
USER_DATA="$(mktemp)"
trap 'rm -f "$USER_DATA"' EXIT

cat > "$USER_DATA" <<POWERSHELL
<powershell>
\$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path "C:\LabSetup" | Out-Null
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
Expand-GzipBase64 "$SETUP_B64" "C:\LabSetup\setup_powerbi_assessment_vm.ps1"
\$process = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\LabSetup\setup_powerbi_assessment_vm.ps1", "-ShutdownWhenDone") -Wait -PassThru
exit \$process.ExitCode
</powershell>
POWERSHELL

echo "Starting Power BI assessment AMI builder"
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
    "ResourceType=instance,Tags=[{Key=Name,Value=$AMI_NAME-builder},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-powerbi-assessment-ami-builder},{Key=ManagedBy,Value=$MANAGED_BY_TAG},{Key=OS,Value=Windows_Server_2022},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-$ROOT_VOLUME_TYPE},{Key=Software,Value=chrome-powerbi-libreoffice-7zip}]" \
    "ResourceType=volume,Tags=[{Key=Name,Value=$AMI_NAME-builder-root},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-powerbi-assessment-ami-builder},{Key=ManagedBy,Value=$MANAGED_BY_TAG},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-$ROOT_VOLUME_TYPE}]" \
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
  exit 4
fi

CONSOLE_OUTPUT=""
for _ in {1..12}; do
  CONSOLE_OUTPUT="$(aws ec2 get-console-output --region "$REGION" --instance-id "$INSTANCE_ID" --latest --query 'Output' --output text 2>/dev/null || true)"
  if grep -q "POWERBI_ASSESSMENT_VM_READY" <<<"$CONSOLE_OUTPUT"; then
    break
  fi
  sleep 15
done

if ! grep -q "POWERBI_ASSESSMENT_VM_READY" <<<"$CONSOLE_OUTPUT"; then
  echo "Did not find readiness marker in console output." >&2
  echo "Check C:\\LabSetup\\setup-powerbi-assessment.log on builder instance $INSTANCE_ID." >&2
  exit 5
fi

AMI_ID="$(aws ec2 create-image \
  --region "$REGION" \
  --instance-id "$INSTANCE_ID" \
  --name "$AMI_NAME" \
  --description "UNext Windows Server 2022 Power BI assessment lab AMI: Chrome, Power BI Desktop, LibreOffice, 7-Zip, protected assessment desktop, local U drive placeholder submission" \
  --no-reboot \
  --tag-specifications \
    "ResourceType=image,Tags=[{Key=Name,Value=$AMI_NAME},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-powerbi-assessment-windows-lab},{Key=ManagedBy,Value=$MANAGED_BY_TAG},{Key=OS,Value=Windows_Server_2022},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-$ROOT_VOLUME_TYPE},{Key=Software,Value=chrome-powerbi-libreoffice-7zip}]" \
    "ResourceType=snapshot,Tags=[{Key=Name,Value=$AMI_NAME-root},{Key=Project,Value=$PROJECT_TAG},{Key=Environment,Value=$ENVIRONMENT_TAG},{Key=Purpose,Value=unext-powerbi-assessment-windows-lab},{Key=ManagedBy,Value=$MANAGED_BY_TAG},{Key=Disk,Value=${ROOT_VOLUME_SIZE}GB-$ROOT_VOLUME_TYPE}]" \
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
SOFTWARE=chrome-powerbi-libreoffice-7zip
EOF

echo "Power BI assessment AMI is ready: $AMI_ID"
echo "Output written to $OUTPUT_FILE"
