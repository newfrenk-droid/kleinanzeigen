"""Клиент антидетект-браузера Vision (browser.vision).

Облачный API (https://v1.empr.cloud/api/v1, заголовок X-Token):
  создание прокси, профилей, импорт куки.
Локальный API (http://127.0.0.1:3030, работает при запущенном приложении Vision):
  запуск/остановка профиля; при запуске возвращается порт для подключения по CDP.
"""
from __future__ import annotations

import httpx

from .proxy import Proxy


class VisionError(RuntimeError):
    pass


def _data(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _id(obj) -> str:
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    if isinstance(obj, dict):
        for key in ("id", "profile_id", "proxy_id"):
            if obj.get(key):
                return str(obj[key])
    raise VisionError(f"В ответе Vision нет id: {obj!r}")


class VisionClient:
    def __init__(self, token: str, folder_id: str, api_url: str, local_url: str,
                 platform: str = "windows", browser: str = "Chrome", timeout: float = 30):
        if not token or not folder_id:
            raise VisionError("Не заданы токен Vision и/или ID папки (раздел «Настройки»)")
        self.folder_id = folder_id
        self.platform = platform
        self.browser = browser
        self.api_url = api_url.rstrip("/")
        self.local_url = local_url.rstrip("/")
        self.timeout = timeout
        self._headers = {"X-Token": token, "Content-Type": "application/json"}

    @classmethod
    def from_settings(cls, s: dict) -> "VisionClient":
        return cls(s["vision_token"], s["vision_folder_id"], s["vision_api_url"], s["vision_local_url"],
                   s.get("vision_platform", "windows"), s.get("vision_browser", "Chrome"))

    async def _request(self, method: str, url: str, **kw):
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.request(method, url, **kw)
        if r.status_code >= 400:
            raise VisionError(f"Vision {method} {url} -> {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return {}

    async def _cloud(self, method: str, path: str, json=None):
        return _data(await self._request(method, self.api_url + path, json=json, headers=self._headers))

    # ---------- облачный API ----------

    async def create_proxy(self, name: str, proxy: Proxy) -> str:
        body = {"proxies": [{
            "proxy_name": name,
            "proxy_type": proxy.type.upper(),
            "proxy_ip": proxy.host,
            "proxy_port": proxy.port,
            "proxy_username": proxy.username,
            "proxy_password": proxy.password,
            "update_url": "",
        }]}
        return _id(await self._cloud("POST", f"/folders/{self.folder_id}/proxies", body))

    async def delete_proxy(self, proxy_id: str) -> None:
        await self._cloud("DELETE", f"/folders/{self.folder_id}/proxies/{proxy_id}")

    async def get_fingerprint(self) -> dict:
        data = await self._cloud("GET", f"/fingerprints/{self.platform}/latest")
        if isinstance(data, dict) and isinstance(data.get("fingerprint"), dict):
            return data["fingerprint"]
        return data or {}

    async def create_profile(self, name: str, proxy_id: str | None, notes: str = "") -> str:
        body = {
            "profile_name": name,
            "profile_notes": notes,
            "profile_tags": [],
            "new_profile_tags": [],
            "proxy_id": proxy_id,
            "profile_status": None,
            "browser": self.browser,
            "platform": self.platform.capitalize() if self.platform != "macos" else "macOS",
            "fingerprint": await self.get_fingerprint(),
        }
        return _id(await self._cloud("POST", f"/folders/{self.folder_id}/profiles", body))

    async def update_profile_proxy(self, profile_id: str, proxy_id: str | None) -> None:
        await self._cloud("PATCH", f"/folders/{self.folder_id}/profiles/{profile_id}", {"proxy_id": proxy_id})

    async def delete_profile(self, profile_id: str) -> None:
        await self._cloud("DELETE", f"/folders/{self.folder_id}/profiles/{profile_id}")

    async def import_cookies(self, profile_id: str, cookies: list[dict]) -> None:
        await self._cloud("POST", f"/cookies/import/{self.folder_id}/{profile_id}", {"cookies": cookies})

    # ---------- локальный API ----------

    async def start_profile(self, profile_id: str) -> int:
        data = await self._request("GET", f"{self.local_url}/start/{self.folder_id}/{profile_id}")
        data = _data(data)
        port = data.get("port") if isinstance(data, dict) else None
        if not port:
            raise VisionError(f"Vision не вернул порт при запуске профиля: {data!r}. Запущено ли приложение Vision?")
        return int(port)

    async def stop_profile(self, profile_id: str) -> None:
        await self._request("GET", f"{self.local_url}/stop/{self.folder_id}/{profile_id}")
