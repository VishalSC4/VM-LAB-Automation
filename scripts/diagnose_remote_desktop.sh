#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/cloud-lab-platform}

cd "$APP_DIR"

echo "== compose services =="
docker compose ps

echo
echo "== recent backend errors =="
docker compose logs --tail=120 backend | grep -Ei "rdp|guac|failed|error|exception|3389|session" || true

echo
echo "== recent guacamole errors =="
docker compose logs --tail=120 guacamole | grep -Ei "rdp|connect|failed|error|exception|unreachable|timeout" || true

echo
echo "== recent guacd errors =="
docker compose logs --tail=120 guacd | grep -Ei "rdp|connect|failed|error|exception|unreachable|timeout" || true

echo
echo "== latest lab targets =="
docker compose exec -T postgres psql -U "${POSTGRES_USER:-cloudlabs}" -d "${POSTGRES_DB:-cloudlabs}" -c \
  "select owner_label,status,ec2_instance_id,private_ip,public_ip,guacamole_connection_id,created_at from labs order by created_at desc limit 10;"

echo
echo "== rdp reachability from platform host =="
docker compose exec -T postgres psql -U "${POSTGRES_USER:-cloudlabs}" -d "${POSTGRES_DB:-cloudlabs}" -At -c \
  "select coalesce(private_ip,''), coalesce(public_ip,''), owner_label from labs where status in ('running','resuming','provisioning') order by created_at desc limit 10;" |
while IFS='|' read -r private_ip public_ip owner_label; do
  for host in "$private_ip" "$public_ip"; do
    if [ -n "$host" ]; then
      if timeout 4 bash -c "cat < /dev/null > /dev/tcp/$host/3389" 2>/dev/null; then
        echo "OK   $owner_label $host:3389"
      else
        echo "FAIL $owner_label $host:3389"
      fi
    fi
  done
done
