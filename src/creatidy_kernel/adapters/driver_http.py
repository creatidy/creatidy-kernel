# SPDX-License-Identifier: Apache-2.0
"""Finite, origin/path-bound stdlib HTTPS. Provider semantics stay in each driver."""

import json
import re
from collections.abc import Callable, Mapping
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from creatidy_kernel.adapters.forgejo_transport import HTTPSForgejoTransport, NoRedirectHTTPHandler, http_deadline
from creatidy_kernel.core.drivers import DriverBinding, driver_bytes
from creatidy_kernel.core.forge import UnsupportedForge


class DriverHTTP:
    def __init__(
        self,
        binding: DriverBinding,
        prefix: str,
        *,
        read_authorization: Callable[[], str] | None = None,
        write_authorization: Callable[[], str] | None = None,
        timeout: int = 15,
        max_bytes: int = 1_000_000,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if not prefix.startswith("/") or prefix.endswith("/") or timeout <= 0 or not 0 < max_bytes <= 16_000_000:
            raise ValueError("explicit namespace prefix and finite HTTP bounds required")
        self.binding, self.prefix = binding, prefix
        self.read_authorization, self.write_authorization = read_authorization, write_authorization
        self.timeout, self.max_bytes = timeout, max_bytes
        self.headers = dict(headers or {})
        if any(key.lower() not in {"accept", "x-github-api-version"} for key in self.headers):
            raise ValueError("unsupported driver headers")
        self.opener = build_opener(ProxyHandler({}), NoRedirectHTTPHandler())

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> tuple[int, object, dict[str, str]]:
        parsed = urlsplit(path)
        decoded = unquote(parsed.path)
        if (
            method not in {"GET", "POST", "PUT"}
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not parsed.path.startswith(self.prefix)
            or parsed.path[len(self.prefix) : len(self.prefix) + 1] not in {"", "/"}
            or any(part in {".", ".."} for part in decoded.split("/"))
            or "\\" in decoded
            or "//" in decoded
            or re.search(r"[\x00-\x20\x7f]", path)
            or len(path) > 8192
            or (method == "GET" and body is not None)
        ):
            raise ValueError("driver request outside configured namespace")
        if not HTTPSForgejoTransport.deadline_available():
            raise UnsupportedForge("total driver HTTP deadline unavailable")
        if method != "GET" and (before_send is None or self.write_authorization is None):
            raise UnsupportedForge("mutation requires separate credentials and current durable authorization")
        data = None if body is None else driver_bytes(dict(body))
        if data is not None and len(data) > self.max_bytes:
            raise ValueError("driver request exceeds safe limit")
        try:
            with http_deadline(self.timeout):
                headers = {"Accept": "application/json", "Content-Type": "application/json", **self.headers}
                credential = self.read_authorization if method == "GET" else self.write_authorization
                if credential is not None:
                    secret = credential()
                    if not secret or any(ord(char) < 32 or ord(char) == 127 for char in secret):
                        raise ValueError("invalid driver authorization")
                    headers["Authorization"] = secret
                request = Request(  # noqa: S310 - trusted configured HTTPS origin; no redirects.
                    self.binding.origin + path, data=data, headers=headers, method=method
                )
                if before_send is not None:
                    before_send()
                try:
                    with self.opener.open(request, timeout=self.timeout) as response:
                        status, content = response.status, response.read(self.max_bytes + 1)
                        response_headers = dict(response.headers.items())
                except HTTPError as error:
                    with error:
                        status, content = error.code, error.read(self.max_bytes + 1)
                        response_headers = dict(error.headers.items())
                if len(content) > self.max_bytes:
                    raise OSError("driver response exceeds safe limit")
                try:
                    payload: object = json.loads(content) if content else None
                except (UnicodeDecodeError, ValueError, RecursionError):
                    payload = None
                return status, payload, {key.lower(): value for key, value in response_headers.items()}
        except (URLError, TimeoutError):
            raise OSError("driver transport outcome unknown") from None


def object_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in cast(dict[object, object], value)):
        raise ValueError("invalid native object")
    return cast(dict[str, object], value)


def list_value(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("invalid native list")
    return cast(list[object], value)


def text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("missing native field")
    return value


def native_number(value: object) -> str:
    if type(value) is not int or not 0 < value < 10**18:
        raise ValueError("invalid native numeric identifier")
    return str(value)
