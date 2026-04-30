"""Locust-instrumented HTTP plumbing shared by the server and mobile emulators.

Every outbound request the SDK or our mobile emulator makes goes through one
of the pool managers in this module. They wrap urllib3 to fire a Locust
``request`` event for each call (so connect/eval/event-POST traffic shows up
in the Locust stats panel) and to redact per-context URL segments so SSE and
flag-fetch URLs aggregate by endpoint rather than per user.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse, urlunparse

import certifi
import urllib3
from ldclient.impl.http import HTTPFactory, _get_proxy_url
from ldclient.impl.util import (
    UnsuccessfulResponseException,
    throw_if_unsuccessful_response,
)
from ldclient.version import VERSION

from locust import events

MOBILE_USER_AGENT = "PythonMobileEmulator/locust"


def ld_headers(sdk_key: str | None, client: str = "PythonClient") -> dict:
    """Server-style request headers (raw SDK key in Authorization)."""
    return {
        "Authorization": sdk_key or "",
        "User-Agent": f"{client}/{VERSION}",
        "Content-Type": "application/json",
    }


def mobile_headers(mobile_key: str | None, *, application_tag: str | None = None, wrapper: str | None = None) -> dict:
    """Mobile-style request headers.

    Mobile SDKs send ``Authorization: api_key <mobileKey>`` (note the
    ``api_key`` prefix - server SDKs use the raw key). We additionally tag the
    User-Agent so traffic from the emulator is distinguishable from real mobile
    or server traffic on the wire.
    """
    headers = {
        "Authorization": f"api_key {mobile_key or ''}",
        "User-Agent": MOBILE_USER_AGENT,
    }
    if application_tag:
        headers["X-LaunchDarkly-Tags"] = application_tag
    if wrapper:
        headers["X-LaunchDarkly-Wrapper"] = wrapper
    return headers


def clean_name(method: str, url: str) -> str:
    """Strip per-context path segments so SDK endpoints aggregate in Locust stats."""
    parsed = urlparse(url)
    if method in ("GET", "REPORT") and (
        parsed.path.startswith("/meval/")
        or parsed.path.startswith("/eval/")
        or "/contexts/" in parsed.path
        or "/users/" in parsed.path
    ):
        parts = parsed.path.split("/")
        parts.pop()
        parts.append("[context]")
        parsed = parsed._replace(path="/".join(parts))
        return urlunparse(parsed)
    return url


def _fire(method: str, url: str, content_len: int, start: float, exception: Exception | None, is_sse: bool) -> None:
    events.request.fire(
        request_type="sse:connect" if is_sse else method,
        name=clean_name(method, url),
        response_time=int((time.time() - start) * 1000),
        response_length=content_len,
        response=None,
        exception=exception,
        context={},
        url=url,
    )


class _InstrumentedMixin:
    def urlopen(self, method, url, redirect=True, **kw):  # type: ignore[override]
        streaming = not kw.get("preload_content", True)
        is_sse = streaming and kw.get("headers", {}).get("Accept") == "text/event-stream"
        start = time.time()
        resp = None
        content_len = 0
        try:
            resp = super().urlopen(method, url, redirect, **kw)  # type: ignore[misc]
            if streaming:
                content_len = int(resp.headers.get("content-length") or 0)
            else:
                content_len = len(resp.data or b"")
            throw_if_unsuccessful_response(resp)
        except UnsuccessfulResponseException as e:
            _fire(method, url, content_len, start, e, is_sse)
            return resp
        except Exception as e:
            _fire(method, url, content_len, start, e, is_sse)
            raise
        _fire(method, url, content_len, start, None, is_sse)
        return resp


class LocustPoolManager(_InstrumentedMixin, urllib3.PoolManager):
    pass


class LocustProxyPoolManager(_InstrumentedMixin, urllib3.ProxyManager):
    pass


def create_http_pool_manager(
    num_pools: int = 1,
    verify_ssl: bool = True,
    target_base_uri: str | None = None,
    force_proxy: str | None = None,
) -> urllib3.PoolManager:
    proxy_url = force_proxy or _get_proxy_url(target_base_uri)
    if verify_ssl:
        kwargs = {"cert_reqs": "CERT_REQUIRED", "ca_certs": certifi.where()}
    else:
        kwargs = {"cert_reqs": "CERT_NONE"}
    if proxy_url is None:
        return LocustPoolManager(num_pools=num_pools, **kwargs)
    return LocustProxyPoolManager(proxy_url, num_pools=num_pools, **kwargs)


class LocustHTTPFactory(HTTPFactory):
    """``HTTPFactory`` variant that hands out our instrumented PoolManager."""

    def create_pool_manager(self, num_pools, target_base_uri):  # type: ignore[override]
        return create_http_pool_manager(
            num_pools=num_pools,
            verify_ssl=not self.http_config.disable_ssl_verification,
            target_base_uri=target_base_uri,
            force_proxy=self.http_config.http_proxy,
        )


__all__ = [
    "LocustHTTPFactory",
    "LocustPoolManager",
    "LocustProxyPoolManager",
    "clean_name",
    "create_http_pool_manager",
    "ld_headers",
    "mobile_headers",
]
