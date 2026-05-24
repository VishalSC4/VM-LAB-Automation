import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import get_settings


class GuacamoleService:
    def __init__(self):
        self.settings = get_settings()

    async def _token(self) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.guacamole_base_url}/api/tokens",
                data={"username": self.settings.guacamole_admin_user, "password": self.settings.guacamole_admin_password},
            )
            response.raise_for_status()
            return response.json()["authToken"]

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

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/connections",
                params={"token": token},
                json=payload,
            )
            response.raise_for_status()
            connection_id = str(response.json()["identifier"])
        public_url = self.settings.guacamole_public_url.rstrip("/")
        access_url = f"{public_url}/session/{connection_id}" if public_url else f"/session/{connection_id}"
        return connection_id, access_url

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
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, params={"token": token})
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

    async def create_user_mapping(self, *, username: str, password: str, connection_id: str) -> None:
        token = await self._token()
        user_payload = {
            "username": username,
            "password": password,
            "attributes": {"disabled": "", "expired": "", "access-window-start": "", "access-window-end": ""},
        }
        async with httpx.AsyncClient(timeout=20) as client:
            user_response = await client.post(
                f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users",
                params={"token": token},
                json=user_payload,
            )
            if user_response.status_code in {400, 409}:
                user_response = await client.put(
                    f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users/{username}",
                    params={"token": token},
                    json=user_payload,
                )
            if user_response.status_code not in {200, 204}:
                user_response.raise_for_status()

            patch = [{"op": "add", "path": f"/connectionPermissions/{connection_id}", "value": "READ"}]
            permission_response = await client.patch(
                f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users/{username}/permissions",
                params={"token": token},
                json=patch,
            )
            permission_response.raise_for_status()

    async def delete_connection(self, connection_id: str) -> None:
        token = await self._token()
        async with httpx.AsyncClient(timeout=20) as client:
            await client.delete(
                f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/connections/{connection_id}",
                params={"token": token},
            )

    async def delete_user(self, username: str) -> None:
        token = await self._token()
        async with httpx.AsyncClient(timeout=20) as client:
            await client.delete(
                f"{self.settings.guacamole_base_url}/api/session/data/{self.settings.guacamole_datasource}/users/{username}",
                params={"token": token},
            )

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
