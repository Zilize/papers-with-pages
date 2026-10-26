"""Bounded, serialized requests for fixed metadata service endpoints."""

import asyncio
import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from pwp.errors import RequestError


def retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(0.0, seconds)
        return None
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return max(0.0, (timestamp - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class MetadataTransport:
    """One connection at a time; applications coordinate across client instances."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None,
        user_agent: str,
        min_interval: float,
        max_retries: int,
        max_retry_wait: float,
        max_response_bytes: int,
        timeout: float,
    ):
        for name, value in [
            ("min_interval", min_interval),
            ("max_retry_wait", max_retry_wait),
            ("timeout", timeout),
        ]:
            if not math.isfinite(value) or value < 0 or (name == "timeout" and value == 0):
                raise ValueError(f"Invalid {name}")
        if max_retries < 0 or max_response_bytes < 1:
            raise ValueError("Invalid request limits")
        self.client = client or httpx.AsyncClient()
        self.owns_client = client is None
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.max_response_bytes = max_response_bytes
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.next_request_at = 0.0
        self.closed = False

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()
        self.closed = True

    async def get(self, url: str, params: dict[str, str]) -> bytes:
        async with self.lock:
            if self.closed:
                raise RuntimeError("The metadata client is closed")
            for attempt in range(self.max_retries + 1):
                wait = max(0.0, self.next_request_at - time.monotonic())
                if wait > max(self.max_retry_wait, self.min_interval):
                    raise RequestError(
                        "backoff_active",
                        "The metadata service is still in backoff",
                        retryable=True,
                        retry_after_s=wait,
                    )
                await asyncio.sleep(wait)
                self.next_request_at = time.monotonic() + self.min_interval
                try:
                    # Never follow redirects to an unvalidated origin.
                    async with self.client.stream(
                        "GET",
                        url,
                        params=params,
                        follow_redirects=False,
                        timeout=self.timeout,
                        headers={"User-Agent": self.user_agent},
                    ) as response:
                        if response.status_code in {429, 500, 502, 503, 504}:
                            delay = retry_after_seconds(response.headers.get("retry-after"))
                            delay = delay if delay is not None else min(2.0**attempt, 30.0)
                            # Keep server backoff even if this operation exhausts its retries.
                            self.next_request_at = max(
                                self.next_request_at, time.monotonic() + delay
                            )
                            failure = RequestError(
                                "http_error",
                                f"Metadata service returned HTTP {response.status_code}",
                                retryable=True,
                                retry_after_s=delay,
                            )
                            if attempt == self.max_retries or delay > self.max_retry_wait:
                                raise failure
                            continue
                        if not response.is_success:
                            raise RequestError(
                                "http_error",
                                f"Metadata service returned HTTP {response.status_code}",
                            )
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > self.max_response_bytes:
                                raise RequestError(
                                    "response_too_large", "Metadata response too large"
                                )
                        return bytes(chunks)
                except httpx.TransportError as exc:
                    if attempt == self.max_retries:
                        raise RequestError(
                            "transport_error", "Metadata request failed", retryable=True
                        ) from exc
                    self.next_request_at = max(
                        self.next_request_at, time.monotonic() + min(2.0**attempt, 30.0)
                    )
        raise AssertionError("Unreachable retry state")
