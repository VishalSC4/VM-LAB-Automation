import asyncio
import base64

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config


AWS_CLIENT_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1})
LOCAL_DB_PREFIX = "local-db:"
LAB_SECRET_PREFIX = "cloudlabs/"
LAB_SSM_PREFIX = "/cloudlabs/"


async def store_lab_password(region: str, lab_id: str, password: str) -> tuple[str, str]:
    name = f"{LAB_SECRET_PREFIX}{lab_id}/windows-password"
    client = boto3.client("secretsmanager", region_name=region, config=AWS_CLIENT_CONFIG)
    try:
        await asyncio.to_thread(
            client.create_secret,
            Name=name,
            SecretString=password,
            Tags=[
                {"Key": "Project", "Value": "cloud-lab-platform"},
                {"Key": "LabId", "Value": lab_id},
            ],
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        message = exc.response.get("Error", {}).get("Message", "")
        if code == "ResourceExistsException":
            await asyncio.to_thread(client.put_secret_value, SecretId=name, SecretString=password)
        elif code == "InvalidRequestException" and "scheduled for deletion" in message:
            await asyncio.to_thread(client.restore_secret, SecretId=name)
            await asyncio.to_thread(client.put_secret_value, SecretId=name, SecretString=password)
        else:
            raise
    return name, ""


async def get_lab_password(region: str, secret_ref: str, ciphertext: str) -> str:
    if secret_ref == "pending":
        return ciphertext
    if secret_ref == "deleted":
        return ""
    if secret_ref.startswith(("local-dev:", LOCAL_DB_PREFIX)):
        return reveal_local_dev_password(ciphertext)
    client = boto3.client("secretsmanager", region_name=region, config=AWS_CLIENT_CONFIG)
    response = await asyncio.to_thread(client.get_secret_value, SecretId=secret_ref)
    return response["SecretString"]


async def delete_lab_password(region: str, secret_ref: str) -> bool:
    if not secret_ref or secret_ref in {"pending", "deleted"}:
        return False
    if secret_ref.startswith(("local-dev:", LOCAL_DB_PREFIX)):
        return True
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


async def delete_lab_credential_artifacts(region: str, lab_id: str, secret_ref: str = "") -> list[str]:
    deleted: list[str] = []
    if await delete_lab_password(region, secret_ref):
        deleted.append(secret_ref)

    secrets_client = boto3.client("secretsmanager", region_name=region, config=AWS_CLIENT_CONFIG)
    expected_secret_prefix = f"{LAB_SECRET_PREFIX}{lab_id}/"
    try:
        paginator = secrets_client.get_paginator("list_secrets")
        pages = await asyncio.to_thread(
            lambda: list(paginator.paginate(Filters=[{"Key": "name", "Values": [expected_secret_prefix]}]))
        )
        for page in pages:
            for item in page.get("SecretList", []):
                name = item.get("Name")
                if not name:
                    continue
                await asyncio.to_thread(
                    secrets_client.delete_secret,
                    SecretId=name,
                    ForceDeleteWithoutRecovery=True,
                )
                deleted.append(name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise

    ssm_client = boto3.client("ssm", region_name=region, config=AWS_CLIENT_CONFIG)
    ssm_path = f"{LAB_SSM_PREFIX}{lab_id}/"
    try:
        paginator = ssm_client.get_paginator("get_parameters_by_path")
        pages = await asyncio.to_thread(
            lambda: list(paginator.paginate(Path=ssm_path, Recursive=True, WithDecryption=False))
        )
        names = [param["Name"] for page in pages for param in page.get("Parameters", []) if param.get("Name")]
        for index in range(0, len(names), 10):
            batch = names[index : index + 10]
            if not batch:
                continue
            await asyncio.to_thread(ssm_client.delete_parameters, Names=batch)
            deleted.extend(batch)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise

    return deleted


def reveal_local_dev_password(ciphertext: str) -> str:
    return base64.b64decode(ciphertext.encode()).decode()
