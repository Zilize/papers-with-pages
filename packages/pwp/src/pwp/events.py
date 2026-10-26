"""Optional observation callbacks, not a persistent job system."""

from collections.abc import Callable
from typing import Literal
from uuid import uuid4

from pydantic import Field

from pwp.models import Issue, Model


class ProgressEvent(Model):
    type: Literal[
        "step_started", "step_progress", "step_completed", "step_failed", "resource_discovered"
    ]
    step: str
    operation_id: str
    paper_id: str | None = None
    resource_url: str | None = None
    completed: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    unit: str | None = None
    issue: Issue | None = None


EventHandler = Callable[[ProgressEvent], None]


class EventCallbackError(RuntimeError):
    """Separate caller callback failures from recoverable resource errors."""


class EventReporter:
    """Emit synchronous callbacks; callback failures propagate to the caller."""

    def __init__(self, step: str, handler: EventHandler | None = None):
        self.step = step
        self.handler = handler
        self.operation_id = uuid4().hex

    def emit(self, event_type: str, **fields: object) -> None:
        if self.handler is not None:
            event = ProgressEvent(
                type=event_type,
                step=self.step,
                operation_id=self.operation_id,
                **fields,
            )
            try:
                self.handler(event)
            except Exception as exc:
                raise EventCallbackError(f"Progress callback failed: {exc}") from exc
