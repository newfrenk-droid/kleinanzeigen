"""Парсинг строки прокси.

Поддерживаемые форматы:
  type://user:pass@host:port   (type = http | https | socks5)
  user:pass@host:port
  host:port:user:pass
  host:port
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


class ProxyParseError(ValueError):
    pass


@dataclass
class Proxy:
    type: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    def __str__(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.type}://{auth}{self.host}:{self.port}"


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as e:
        raise ProxyParseError(f"Некорректный порт: {value!r}") from e
    if not 0 < port < 65536:
        raise ProxyParseError(f"Некорректный порт: {value!r}")
    return port


def parse_proxy(text: str) -> Proxy | None:
    text = (text or "").strip()
    if not text:
        return None

    if "://" not in text:
        parts = text.split(":")
        if "@" not in text and len(parts) == 4:
            host, port, user, password = parts
            return Proxy("http", host, _port(port), user, password)
        text = "http://" + text

    url = urlsplit(text)
    scheme = url.scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in ("http", "https", "socks5", "socks4"):
        raise ProxyParseError(f"Неподдерживаемый тип прокси: {url.scheme}")
    try:
        port = url.port
    except ValueError as e:
        raise ProxyParseError(str(e)) from e
    if not url.hostname or not port:
        raise ProxyParseError("Нужно указать host и port")
    return Proxy(
        scheme,
        url.hostname,
        port,
        unquote(url.username or ""),
        unquote(url.password or ""),
    )
