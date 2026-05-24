import asyncio
import base64

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config


AWS_CLIENT_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1})


async def store_lab_password(region: str, lab_id: str, password: str) -> tuple[str, str]:
    name = f"cloudlabs/{lab_id}/windows-password"
    try:
        client = boto3.client("secretsmanager", region_name=region, config=AWS_CLIENT_CONFIG)
        await asyncio.to_thread(client.create_secret, Name=name, SecretString=password)
        return name, "stored-in-secrets-manager"
    except Exception:
        # MVP fallback for local development only. Production should grant Secrets Manager access.
        return f"local-dev:{name}", base64.b64encode(password.encode()).decode()


async def get_lab_password(region: str, secret_ref: str, ciphertext: str) -> str:
    if secret_ref.startswith("local-dev:"):
        return reveal_local_dev_password(ciphertext)
    client = boto3.client("secretsmanager", region_name=region, config=AWS_CLIENT_CONFIG)
    response = await asyncio.to_thread(client.get_secret_value, SecretId=secret_ref)
    return response["SecretString"]


async def delete_lab_password(region: str, secret_ref: str) -> bool:
    if not secret_ref or secret_ref in {"pending", "deleted"} or secret_ref.startswith("local-dev:"):
        return False
    client = boto3.client("secretsmanager", region_name=region, config=AWS_CLIENT_CONFIG)
    try:
        await asyncio.to_thread(
            client.delete_secret,
            SecretId=secret_ref,
            ForceDeleteWithoutRecovery=True,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise
    return True


def reveal_local_dev_password(ciphertext: str) -> str:
    return base64.b64decode(ciphertext.encode()).decode()
