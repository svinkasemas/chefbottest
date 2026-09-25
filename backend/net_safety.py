"""
Безопасное скачивание по «чужим» ссылкам (присланным пользователями или
найденным на сторонних страницах) - защита от SSRF.

Без этой проверки сервер скачивал любой адрес, включая внутренние: например,
страница с рецептом могла указать в og:image http://127.0.0.1:2019/config/
(админ-API Caddy), сервер скачивал ответ и сохранял его как фото рецепта -
то есть отдавал содержимое внутреннего ресурса всем желающим.

Что делает safe_get:
- пускает только http/https;
- перед КАЖДЫМ запросом (и после каждого редиректа) проверяет, что имя хоста
  разрешается только в публичные IP-адреса (не 127.0.0.1, не 10.x/192.168.x,
  не 169.254.x - адрес облачных метаданных, и т.п.);
- следует редиректам сам, не больше MAX_REDIRECTS, проверяя каждый адрес;
- скачивает потоком и обрывает, если ответ больше max_bytes.

Как и везде в проекте: сначала прямое соединение, при сетевой ошибке - через
PROXY_URL. Проверка адреса выполняется до любой из попыток.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import requests

from backend.config import PROXY_URL

logger = logging.getLogger("net_safety")

MAX_REDIRECTS = 3
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class UnsafeUrlError(ValueError):
    """Ссылка не прошла проверку (не http/https, внутренний адрес и т.п.)."""


class DownloadError(Exception):
    """Не удалось скачать (сеть, HTTP-ошибка, слишком большой ответ)."""


def assert_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError("Разрешены только ссылки http:// и https://")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("Ссылки с логином/паролем не поддерживаются")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeUrlError("Не удалось найти сайт по этой ссылке") from e
    if not infos:
        raise UnsafeUrlError("Не удалось найти сайт по этой ссылке")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise UnsafeUrlError("Ссылка ведёт во внутреннюю сеть")


def is_safe_display_url(url: str | None) -> bool:
    """
    Можно ли сохранить ссылку в базу и показать её пользователям как href:
    только http/https, без кавычек, угловых скобок, пробелов и управляющих
    символов (иначе ссылка может «вырваться» из атрибута в HTML).
    """
    if not url or len(url) > 500:
        return False
    if any(c in url for c in "\"'<>`\\") or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _proxies(use_proxy: bool) -> dict | None:
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def _read_limited(response: requests.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise DownloadError(f"Файл слишком большой ({int(declared) // 1024} КБ)")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise DownloadError(f"Файл больше {max_bytes // 1024} КБ")
        chunks.append(chunk)
    return b"".join(chunks)


def _get_once(url: str, headers: dict | None, timeout: float, max_bytes: int, use_proxy: bool):
    """Один переход по цепочке редиректов; возвращает (final_url, response, body)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        assert_public_http_url(current)
        response = requests.get(
            current, headers=headers, timeout=timeout, proxies=_proxies(use_proxy),
            allow_redirects=False, stream=True,
        )
        try:
            if response.status_code in _REDIRECT_STATUSES and response.headers.get("Location"):
                current = urljoin(current, response.headers["Location"])
                continue
            response.raise_for_status()
            return current, response, _read_limited(response, max_bytes)
        finally:
            response.close()
    raise DownloadError("Слишком много перенаправлений")


def safe_get(
    url: str,
    *,
    max_bytes: int,
    headers: dict | None = None,
    timeout: float = 30,
) -> tuple[str, requests.Response, bytes]:
    """
    Скачивает url с проверками (см. docstring модуля). Возвращает
    (итоговый_url_после_редиректов, response, тело). Поднимает
    UnsafeUrlError (ссылка запрещена - повторять бессмысленно) или
    DownloadError (не получилось скачать).
    """
    errors: list[str] = []
    attempts = (False, True) if PROXY_URL else (False,)
    for use_proxy in attempts:
        try:
            return _get_once(url, headers, timeout, max_bytes, use_proxy)
        except UnsafeUrlError:
            raise
        except DownloadError:
            raise
        except Exception as e:
            logger.warning("Не удалось скачать %s (прокси=%s): %s", url, use_proxy, e)
            errors.append(str(e))
    raise DownloadError("Не удалось открыть страницу")


def looks_like_image(data: bytes) -> bool:
    return (
        data[:3] == b"\xff\xd8\xff"                          # JPEG
        or data[:8] == b"\x89PNG\r\n\x1a\n"                  # PNG
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")   # WebP
        or data[:6] in (b"GIF87a", b"GIF89a")
    )
