#!/usr/bin/env python3
"""The bits of tgx that speak plain HTTPS: Bot API rich messages and telegra.ph.

macOS python.org builds ship without a CA bundle, so `urlopen` fails with
"self-signed certificate in certificate chain" on perfectly good certificates
until certifi (or SSL_CERT_FILE) provides one. That is handled here once, and
the error message says what to do instead of leaking an SSL traceback.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT = 60
_CONTEXT: ssl.SSLContext | None = None


class NetError(RuntimeError):
    """A request that could not be made or was refused, phrased for a human."""


def context() -> ssl.SSLContext:
    global _CONTEXT
    if _CONTEXT is not None:
        return _CONTEXT
    bundle = os.environ.get("TGX_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if not bundle:
        try:
            import certifi

            bundle = certifi.where()
        except ModuleNotFoundError:
            bundle = None
    _CONTEXT = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
    return _CONTEXT


def _open(request: urllib.request.Request, what: str) -> str:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context()) as response:
            return response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.read().decode(errors="replace")
    except ssl.SSLCertVerificationError as exc:
        raise NetError(
            f"{what}: не проверяется TLS-сертификат ({exc.verify_message}). "
            "Поставьте certifi (`pip install -r requirements.txt`) или укажите TGX_CA_BUNDLE"
        ) from exc
    except Exception as exc:
        raise NetError(f"{what} недоступен: {exc}") from exc


def post_json(url: str, payload: dict[str, Any], what: str = "сервис") -> Any:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "tgx/1.0"},
    )
    return json.loads(_open(request, what))


def post_form(url: str, fields: dict[str, Any], what: str = "сервис") -> Any:
    data = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode()
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "tgx/1.0"})
    return json.loads(_open(request, what))
