"""Resolve, classify and pin outbound webhook connections."""

import asyncio
import ipaddress
import socket
import time
from typing import Literal, overload
from urllib.parse import urlsplit

import httpx

_ALWAYS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "169.254.0.0/16",
        "255.255.255.255/32",
        "fe80::/10",
        "fd00:ec2::254/128",
        "100.100.100.200/32",
        "168.63.129.16/32",
        "192.0.0.192/32",
    )
)
_PRIVATE = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "fc00::/7",
        "fec0::/10",
        "::1/128",
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "198.18.0.0/15",
        "2001:db8::/32",
        "2001:2::/48",
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTO4 = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")


class BlockedDestination(ValueError):
    pass


class ResponseTooLarge(ValueError):
    pass


def always_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    embedded = _embedded(ip)
    return (
        ip.is_unspecified
        or ip.is_multicast
        or any(ip in net for net in _ALWAYS)
        or (embedded is not None and always_blocked_address(embedded))
    )


def _embedded(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _SIXTO4:
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    if ip in _TEREDO:
        return ipaddress.IPv4Address((int(ip) & 0xFFFFFFFF) ^ 0xFFFFFFFF)
    return None


def allowed_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, private: bool) -> bool:
    if always_blocked_address(ip):
        return False
    transition = isinstance(ip, ipaddress.IPv6Address) and (
        ip.ipv4_mapped is not None or ip in _NAT64 or ip in _SIXTO4 or ip in _TEREDO
    )
    private_range = ip.is_loopback or any(ip in net for net in _PRIVATE)
    if ip.is_reserved and not transition and not private_range:
        return False
    # IPv4 broadcast is covered by is_reserved only on some Python releases.
    if isinstance(ip, ipaddress.IPv4Address) and int(ip) == 0xFFFFFFFF:
        return False
    embedded = _embedded(ip)
    if embedded is not None and not allowed_address(embedded, private):
        return False
    if not private and (private_range or not ip.is_global):
        return False
    return True


async def resolve_checked(host: str, port: int, private: bool) -> list[str]:
    results = await asyncio.wait_for(
        asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM), 5
    )
    addresses = list(dict.fromkeys(str(item[4][0]).split("%", 1)[0] for item in results))
    if not addresses:
        raise socket.gaierror("No addresses")
    if any(not allowed_address(ipaddress.ip_address(value), private) for value in addresses):
        raise BlockedDestination("Address blocked")
    return addresses


@overload
async def post_pinned(
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    private: bool,
    timeout: int,
    max_response_bytes: int = 65536,
    return_body: Literal[False] = False,
) -> tuple[int, str | None]: ...


@overload
async def post_pinned(
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    private: bool,
    timeout: int,
    max_response_bytes: int = 65536,
    return_body: Literal[True],
) -> tuple[int, str | None, bytes]: ...


async def post_pinned(
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    private: bool,
    timeout: int,
    max_response_bytes: int = 65536,
    return_body: bool = False,
) -> tuple[int, str | None] | tuple[int, str | None, bytes]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    deadline = time.monotonic() + timeout
    addresses = await asyncio.wait_for(resolve_checked(host, port, private), timeout)
    ascii_host = host.encode("idna").decode("ascii") if ":" not in host else host
    host_header = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    if parsed.port is not None:
        host_header += f":{port}"
    headers = {**headers, "Host": host_header}
    last_error: httpx.ConnectError | httpx.ConnectTimeout | None = None
    for ip in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.ConnectTimeout("Outbound request deadline exceeded")
        authority = f"[{ip}]" if ":" in ip else ip
        target = parsed._replace(netloc=f"{authority}:{port}").geturl()
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(remaining, connect=min(5, remaining)),
        ) as client:
            request = client.build_request("POST", target, headers=headers, content=body)
            request.extensions["sni_hostname"] = ascii_host
            try:
                async with asyncio.timeout(remaining):
                    async with client.stream(
                        "POST",
                        request.url,
                        headers=request.headers,
                        content=body,
                        extensions=request.extensions,
                    ) as response:
                        chunks: list[bytes] = []
                        read = 0
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            read += len(chunk)
                            if read > max_response_bytes:
                                if return_body:
                                    raise ResponseTooLarge("Response too large")
                                break
                            if return_body:
                                chunks.append(chunk)
                        result = (response.status_code, response.headers.get("Retry-After"))
                        return (*result, b"".join(chunks)) if return_body else result
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
    assert last_error is not None
    raise last_error
