import asyncio

import httpx
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential
from urllib.parse import quote

from app.core.config import get_settings


_GUACAMOLE_WRITE_SEMAPHORE = asyncio.Semaphore(5)


class GuacamoleConnectionNotFound(RuntimeError):
    def __init__(self, connection_id: str):
        super().__init__(f"Guacamole connection {connection_id} does not exist")
        self.connection_id = connection_id


class GuacamoleService:
    def __init__(self):
        self.settings = get_settings()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def _token(self) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.guacamole_base_url}/api/tokens",
                data={"username": self.settings.guacamole_admin_user, "password": self.settings.guacamole_admin_password},
            )
            response.raise_for_status()
            return response.json()["authToken"]

    def access_url_for_connection(self, connection_id: str) -> str:
        public_url = self.settings.guacamole_public_url.rstrip("/")
        return f"{public_url}/session/{connection_id}" if public_url else f"/session/{connection_id}"

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def create_rdp_connection(
        self,
        *,
        name: str,
        hostname: str,
        username: str,
        password: str,
        domain: str | None = None,
    ) -> tuple[str, str]:
        token = await self._token()
        payload = {
            "parentIdentifier": "ROOT",
            "name": name,
            "protocol": "rdp",
            "parameters": {
                "hostname": hostname,
                "port": "3389",
                "username": username,
                "password": password,
                "security": "nla",
                "ignore-cert": "true",
                "enable-drive": "false",
            },
            "attributes": {"max-connections": "1", "max-connections-per-user": "1"},
        }
        if domain:
            payload["parameters"]["domain"] = domain

        async with _GUACAMOLE_WRITE_SEMAPHORE:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/connections",
                    params={"token": token},
                    json=payload,
                )
                response.raise_for_status()
                connection_id = str(response.json()["identifier"])
        return connection_id, self.access_url_for_connection(connection_id)

    @retry(
        retry=retry_if_not_exception_type(GuacamoleConnectionNotFound),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=20),
    )
    async def update_rdp_connection(
        self,
        connection_id: str,
        *,
        hostname: str,
        username: str,
        password: str,
        domain: str | None = None,
    ) -> None:
        token = await self._token()
        url = f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/connections/{connection_id}"
        async with _GUACAMOLE_WRITE_SEMAPHORE:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, params={"token": token})
                if response.status_code == 404:
                    raise GuacamoleConnectionNotFound(connection_id)
                response.raise_for_status()
                current = response.json()

                payload = {
                    "parentIdentifier": current.get("parentIdentifier") or "ROOT",
                    "name": current.get("name") or connection_id,
                    "protocol": "rdp",
                    "parameters": {
                        **(current.get("parameters") or {}),
                        "hostname": hostname,
                        "port": "3389",
                        "username": username,
                        "password": password,
                        "security": "nla",
                        "ignore-cert": "true",
                        "enable-drive": "false",
                    },
                    "attributes": current.get("attributes") or {"max-connections": "1", "max-connections-per-user": "1"},
                }
                if domain:
                    payload["parameters"]["domain"] = domain
                else:
                    payload["parameters"].pop("domain", None)

                update_response = await client.put(url, params={"token": token}, json=payload)
                update_response.raise_for_status()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def create_user_mapping(self, *, username: str, password: str, connection_id: str) -> None:
        token = await self._token()
        encoded_username = quote(username, safe="")
        user_payload = {
            "username": username,
            "password": password,
            "attributes": {"disabled": "", "expired": "", "access-window-start": "", "access-window-end": ""},
        }
        async with _GUACAMOLE_WRITE_SEMAPHORE:
            async with httpx.AsyncClient(timeout=30) as client:
                user_response = await client.post(
                    f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users",
                    params={"token": token},
                    json=user_payload,
                )
                if user_response.status_code in {400, 409}:
                    user_response = await client.put(
                        f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users/{encoded_username}",
                        params={"token": token},
                        json=user_payload,
                    )
                if user_response.status_code not in {200, 201, 204}:
                    user_response.raise_for_status()

                patch = [{"op": "add", "path": f"/connectionPermissions/{connection_id}", "value": "READ"}]
                permission_response = await client.patch(
                    f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users/{encoded_username}/permissions",
                    params={"token": token},
                    json=patch,
                )
                if permission_response.status_code in {400, 409}:
                    permission_response = await client.patch(
                        f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users/{encoded_username}/permissions",
                        params={"token": token},
                        json=[{"op": "replace", "path": f"/connectionPermissions/{connection_id}", "value": "READ"}],
                    )
                permission_response.raise_for_status()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def delete_connection(self, connection_id: str) -> None:
        token = await self._token()
        async with _GUACAMOLE_WRITE_SEMAPHORE:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.delete(
                    f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/connections/{connection_id}",
                    params={"token": token},
                )
                if response.status_code != 404:
                    response.raise_for_status()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def delete_connections_by_name_prefix(self, name_prefix: str) -> list[str]:
        token = await self._token()
        async with _GUACAMOLE_WRITE_SEMAPHORE:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/connections",
                    params={"token": token},
                )
                response.raise_for_status()
                connections = response.json()
                if isinstance(connections, dict):
                    values = connections.values()
                elif isinstance(connections, list):
                    values = connections
                else:
                    values = []

                deleted: list[str] = []
                for connection in values:
                    if not isinstance(connection, dict):
                        continue
                    name = str(connection.get("name") or "")
                    connection_id = connection.get("identifier")
                    if not connection_id or not name.startswith(name_prefix):
                        continue
                    delete_response = await client.delete(
                        f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/connections/{connection_id}",
                        params={"token": token},
                    )
                    if delete_response.status_code != 404:
                        delete_response.raise_for_status()
                    deleted.append(str(connection_id))
        return deleted

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def delete_user(self, username: str) -> None:
        token = await self._token()
        encoded_username = quote(username, safe="")
        async with _GUACAMOLE_WRITE_SEMAPHORE:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.delete(
                    f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users/{encoded_username}",
                    params={"token": token},
                )
                if response.status_code != 404:
                    response.raise_for_status()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def active_connection_ids(self) -> set[str]:
        token = await self._token()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/activeConnections",
                params={"token": token},
            )
            response.raise_for_status()
        active = response.json()
        if isinstance(active, dict):
            values = active.values()
        elif isinstance(active, list):
            values = active
        else:
            values = []

        connection_ids: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            connection_id = item.get("connectionIdentifier") or item.get("connection_id") or item.get("identifier")
            if connection_id is not None:
                connection_ids.add(str(connection_id))
        return connection_ids
