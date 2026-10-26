"""Deterministic fetch responses, with the same operation budget as real requests."""

import json

from pwp.errors import RequestError
from pwp.network import FetchResult, RequestBudget, consume_request, validate_target


class FakeFetcher:
    def __init__(self, responses, maximum=1000):
        self.responses = responses
        self.budget = RequestBudget(maximum)
        self.calls = []

    async def get(self, url, **kwargs):
        validate_target(url)
        consume_request(self.budget)
        self.calls.append((url, kwargs))
        value = self.responses.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RequestError("missing_fixture", "No fixture for " + url)
        if isinstance(value, FetchResult):
            return FetchResult(
                value.url, value.final_url, value.status, value.headers.copy(), value.body
            )
        if isinstance(value, dict):
            return FetchResult(
                url, url, 200, {"content-type": "application/json"}, json.dumps(value).encode()
            )
        if isinstance(value, str):
            return FetchResult(url, url, 200, {"content-type": "text/html"}, value.encode())
        return FetchResult(url, url, 200, {"content-type": "image/png"}, value)
