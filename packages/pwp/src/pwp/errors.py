"""Errors from metadata services and document parsing."""

from pwp.models import Issue


class PwpError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: float | None = None,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.issue = Issue(
            code=code,
            message=message,
            retryable=retryable,
            retry_after_s=retry_after_s,
            http_status=http_status,
        )


class MetadataError(PwpError):
    """Invalid metadata or a protocol-level error returned by arXiv."""


class RequestError(PwpError):
    """A bounded HTTP request failed."""


class PaperNotFoundError(PwpError):
    """No matching paper was returned for the requested reference."""
