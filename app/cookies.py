"""Парсинг куки в разных форматах в единый вид (формат Playwright / Vision).

Поддерживаются:
  * JSON-массив (экспорт EditThisCookie, Cookie-Editor, Vision, Playwright)
  * JSON-объект вида {"cookies": [...]}
  * Netscape cookies.txt
  * строка заголовка Cookie: "name=value; name2=value2"
"""
from __future__ import annotations

import json

DEFAULT_DOMAIN = ".kleinanzeigen.de"

_SAME_SITE = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}


class CookieParseError(ValueError):
    pass


def _normalize(raw: dict) -> dict | None:
    name = raw.get("name")
    if not name:
        return None
    value = raw.get("value", "")
    domain = raw.get("domain") or raw.get("host") or DEFAULT_DOMAIN
    expires = raw.get("expires", raw.get("expirationDate", raw.get("expiry", -1)))
    try:
        expires = int(float(expires)) if expires not in (None, "", "Session") else -1
    except (TypeError, ValueError):
        expires = -1
    if raw.get("session") is True:
        expires = -1
    same_site = _SAME_SITE.get(str(raw.get("sameSite") or "lax").lower(), "Lax")
    secure = bool(raw.get("secure", False))
    if same_site == "None":
        # Chrome отвергает SameSite=None без Secure
        secure = True
    return {
        "name": str(name),
        "value": str(value),
        "domain": str(domain),
        "path": str(raw.get("path") or "/"),
        "expires": expires,
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": secure,
        "sameSite": same_site,
    }


def _parse_netscape(text: str) -> list[dict]:
    cookies = []
    for line in text.splitlines():
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        cookies.append({
            "name": name, "value": value.strip(), "domain": domain, "path": path,
            "secure": secure.upper() == "TRUE", "expires": expires, "httpOnly": http_only,
        })
    return cookies


def _parse_header(text: str) -> list[dict]:
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    cookies = []
    for pair in text.split(";"):
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        if name.strip():
            cookies.append({"name": name.strip(), "value": value.strip(), "secure": True})
    return cookies


def parse_cookies(text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        raise CookieParseError("Куки пустые")

    raw: list[dict]
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CookieParseError(f"Некорректный JSON: {e}") from e
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        if not isinstance(data, list):
            raise CookieParseError("Ожидался JSON-массив куки")
        raw = [c for c in data if isinstance(c, dict)]
    elif "\t" in text:
        raw = _parse_netscape(text)
    else:
        raw = _parse_header(text)

    cookies = [c for c in (_normalize(r) for r in raw) if c]
    if not cookies:
        raise CookieParseError("Не удалось распознать ни одной куки")
    return cookies
