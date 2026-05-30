#!/usr/bin/env bash
set -euo pipefail

REGION=${REGION:-${AWS_REGION:-ap-south-1}}
SECRET_PREFIX=${SECRET_PREFIX:-cloudlabs/}
SSM_PREFIX=${SSM_PREFIX:-/cloudlabs/}

echo "== terminating active cloud lab EC2 instances in $REGION =="
INSTANCE_IDS=$(aws ec2 describe-instances \
  --region "$REGION" \
  --filters "Name=tag:cloudlab:lab_id,Values=*" "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down" \
  --query "Reservations[].Instances[].InstanceId" \
  --output text)
if [ -n "$INSTANCE_IDS" ] && [ "$INSTANCE_IDS" != "None" ]; then
  aws ec2 terminate-instances --region "$REGION" --instance-ids $INSTANCE_IDS >/dev/null
  aws ec2 wait instance-terminated --region "$REGION" --instance-ids $INSTANCE_IDS
  echo "$INSTANCE_IDS"
else
  echo "none"
fi

echo
echo "== deleting unattached cloud lab EBS volumes in $REGION =="
VOLUME_IDS=$(aws ec2 describe-volumes \
  --region "$REGION" \
  --filters "Name=tag:cloudlab:lab_id,Values=*" "Name=status,Values=available" \
  --query "Volumes[].VolumeId" \
  --output text)
if [ -n "$VOLUME_IDS" ] && [ "$VOLUME_IDS" != "None" ]; then
  for volume_id in $VOLUME_IDS; do
    aws ec2 delete-volume --region "$REGION" --volume-id "$volume_id"
    echo "$volume_id"
  done
else
  echo "none"
fi

echo
echo "== force deleting cloud lab Secrets Manager entries in $REGION =="
SECRET_NAMES=$(aws secretsmanager list-secrets \
  --region "$REGION" \
  --filters "Key=name,Values=$SECRET_PREFIX" \
  --query "SecretList[].Name" \
  --output text)
if [ -n "$SECRET_NAMES" ] && [ "$SECRET_NAMES" != "None" ]; then
  for secret_name in $SECRET_NAMES; do
    aws secretsmanager delete-secret \
      --region "$REGION" \
      --secret-id "$secret_name" \
      --force-delete-without-recovery >/dev/null
    echo "$secret_name"
  done
else
  echo "none"
fi

echo
echo "== deleting cloud lab SSM parameters in $REGION =="
PARAMETER_NAMES=$(aws ssm get-parameters-by-path \
  --region "$REGION" \
  --path "$SSM_PREFIX" \
  --recursive \
  --query "Parameters[].Name" \
  --output text 2>/dev/null || true)
if [ -n "$PARAMETER_NAMES" ] && [ "$PARAMETER_NAMES" != "None" ]; then
  batch=()
  for parameter_name in $PARAMETER_NAMES; do
    batch+=("$parameter_name")
    if [ "${#batch[@]}" -eq 10 ]; then
      aws ssm delete-parameters --region "$REGION" --names "${batch[@]}" >/dev/null
      printf '%s\n' "${batch[@]}"
      batch=()
    fi
  done
  if [ "${#batch[@]}" -gt 0 ]; then
    aws ssm delete-parameters --region "$REGION" --names "${batch[@]}" >/dev/null
    printf '%s\n' "${batch[@]}"
  fi
else
  echo "none"
fi
