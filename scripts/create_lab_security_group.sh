#!/usr/bin/env bash
set -euo pipefail

: "${VPC_ID:?Set VPC_ID}"
: "${GUACAMOLE_SECURITY_GROUP_ID:?Set GUACAMOLE_SECURITY_GROUP_ID}"
: "${AWS_REGION:=ap-south-1}"

SG_ID=$(aws ec2 create-security-group \
  --region "$AWS_REGION" \
  --vpc-id "$VPC_ID" \
  --group-name cloud-lab-windows-rdp \
  --description "RDP from Guacamole only" \
  --query 'GroupId' \
  --output text)

aws ec2 authorize-security-group-ingress \
  --region "$AWS_REGION" \
  --group-id "$SG_ID" \
  --protocol tcp \
  --port 3389 \
  --source-group "$GUACAMOLE_SECURITY_GROUP_ID"

echo "$SG_ID"

