"""One harvest operation at a time; the gallery remains usable throughout."""

import asyncio
import time
from datetime import date
from uuid import uuid4

from pwp import ArxivClient, RenderSession, RequestBudget, SafeFetcher, resolve
from pwp.models import Issue, Paper, PaperResult, StepOutcome
from pydantic import BaseModel, ConfigDict, model_validator

from app.adaptive import Governor, adaptive_map
from app.storage import Store, atomic_json, now

PAPER_TIMEOUT_SECONDS = 180


class HarvestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    archive: str = "cs"
    refresh: bool = False

    @model_validator(mode="after")
    def validate_range(self):
        if self.start > self.end:
            raise ValueError("Start date must not be after end date")
        if self.end > date.today():
            raise ValueError("End date must not be in the future")
        if self.archive not in {
            "",
            "cs",
            "math",
            "physics",
            "stat",
            "q-bio",
            "q-fin",
            "eess",
            "econ",
        }:
            raise ValueError("Unsupported archive")
        return self


class Collector:
    def __init__(
        self,
        store: Store,
        client_factory=ArxivClient,
        resolver=resolve,
        *,
        trusted_proxy=None,
        github_token=None,
        max_workers=8,
        render_pages=6,
    ):
        if type(render_pages) is not int or not 1 <= render_pages <= 16:
            raise ValueError("Render pages must be between 1 and 16")
        self.render_pages = render_pages
        Governor(max_workers)
        self.max_workers = max_workers
        self.store = store
        self.client_factory = client_factory
        self.resolver = resolver
        self.trusted_proxy = trusted_proxy
        self.github_token = github_token
        self._stop_lock = asyncio.Lock()
        self._work = {}
        self.task = None
        self.state = store.latest_run()

    def start(self, request: HarvestRequest):
        if self.task is not None and not self.task.done():
            raise RuntimeError("A collection is already running")
        run_id = now().replace(":", "").replace("+", "-") + "-" + uuid4().hex[:8]
        self.state = {
            "id": run_id,
            "request": request.model_dump(mode="json"),
            "status": "running",
            "phase": "harvesting",
            "harvested": 0,
            "total": 0,
            "completed": 0,
            "reused": 0,
            "with_resources": 0,
            "with_warnings": 0,
            "invalid": 0,
            "deleted": 0,
            "failures": [],
            "current": None,
            "error": None,
            "started_at": now(),
        }
        self.store.save_run(self.state)
        self.task = asyncio.create_task(self._run(request))
        return self.state

    async def _run(self, request):
        state = self.state
        fetcher = None
        try:
            keys = set()
            async with self.client_factory() as client:
                number = 0
                async for batch in client.harvest(
                    set_spec=request.archive or None,
                    metadata_updated_from=request.start,
                    metadata_updated_until=request.end,
                ):
                    atomic_json(
                        self.store.root / "runs" / state["id"] / f"batch-{number:06}.json",
                        batch.model_dump(mode="json"),
                    )
                    number += 1
                    for record in batch.records:
                        key = self.store.merge(record)
                        state["harvested"] += 1
                        if record.status == "invalid":
                            state["invalid"] += 1
                        elif record.status == "deleted":
                            state["deleted"] += 1
                        if key:
                            keys.add(key)
                    self.store.save_run(state)
            pending = [key for key in sorted(keys) if not self.store.metadata(key)["deleted"]]
            state.update(phase="resolving", total=len(pending))
            self.store.save_run(state)
            active = {}
            governor = Governor(self.max_workers)
            async with (
                RenderSession(concurrency=min(self.render_pages, governor.maximum)) as renderer,
                SafeFetcher(
                    budget=RequestBudget(maximum=None),
                    trusted_proxy=self.trusted_proxy,
                    max_connections=max(4, governor.maximum * 2),
                ) as fetcher,
            ):

                async def process(key):
                    active[key] = self.store.metadata(key)["paper"]["title"]
                    self._work[key] = {
                        "title": active[key],
                        "started": time.monotonic(),
                        "stage_started": time.monotonic(),
                        "stage": "prepare",
                        "resource_url": None,
                    }
                    state["current"] = " | ".join(active.values())
                    try:
                        return await self._process_one(key, request, state, fetcher, renderer)
                    finally:
                        active.pop(key, None)
                        self._work.pop(key, None)
                        state["current"] = " | ".join(active.values()) or None

                def report(limit, count, reason):
                    stamp = time.monotonic()
                    state["network"] = fetcher.statistics()
                    state["workers"] = [
                        {
                            "key": key,
                            "title": work["title"],
                            "stage": work["stage"],
                            "elapsed_s": round(stamp - work["started"], 1),
                            "stage_elapsed_s": round(stamp - work["stage_started"], 1),
                            "resource_url": work["resource_url"],
                        }
                        for key, work in self._work.items()
                    ]
                    state["concurrency"] = {
                        "target": limit,
                        "active": count,
                        "maximum": governor.maximum,
                        "render_limit": renderer.concurrency,
                        "reason": reason,
                    }

                await adaptive_map(pending, process, governor=governor, report=report)
            state.update(status="completed", phase="completed", current=None)
        except asyncio.CancelledError:
            state.update(status="interrupted", phase="interrupted", current=None)
            state["error"] = "Collection stopped. Repeat the date range to continue."
        except Exception as exc:
            state.update(status="failed", phase="failed", error=str(exc), current=None)
        finally:
            if fetcher is not None:
                state["network"] = fetcher.statistics()
            state["workers"] = []
            if "concurrency" in state:
                state["concurrency"]["active"] = 0
            state["finished_at"] = now()
            self.store.save_run(state)

    async def _process_one(self, key, request, state, fetcher, renderer):
        paper = Paper.model_validate(self.store.metadata(key)["paper"])
        self.store.save_run(state)
        if not request.refresh and self.store.reusable(key):
            state["reused"] += 1
        else:

            def progress(event):
                work = self._work.get(key)
                if work is None or event.type not in {"step_started", "step_progress"}:
                    return
                stage = event.step + (":" + event.unit if event.unit else "")
                if work["stage"] != stage:
                    work["stage"] = stage
                    work["stage_started"] = time.monotonic()
                if event.resource_url:
                    work["resource_url"] = event.resource_url

            try:
                async with asyncio.timeout(PAPER_TIMEOUT_SECONDS):
                    result = await self.resolver(
                        paper,
                        work_dir=self.store.root / "assets" / key / state["id"],
                        render=True,
                        download_images=False,
                        fetcher=fetcher,
                        render_session=renderer,
                        github_token=self.github_token,
                        on_event=progress,
                    )
                # SafeFetcher owns host cooldowns. One site's quota must not
                # reduce admission for unrelated hosts or metadata-only papers.
                self.store.save_result(key, state["id"], result)
                if result.status != "completed":
                    messages = [
                        step.issue.message
                        for step in result.steps
                        if step.issue and step.status in {"failed", "partial"}
                    ]
                    if any(step.reason == "limit_reached" for step in result.steps):
                        messages.append("Link discovery reached its exploration limit")
                    state["failures"].append(
                        {
                            "id": paper.arxiv_id,
                            "message": messages[0]
                            if messages
                            else "Some resources could not be processed",
                        }
                    )
            except Exception as exc:
                timed_out = isinstance(exc, TimeoutError)
                message = (
                    f"Paper processing exceeded {PAPER_TIMEOUT_SECONDS:g} seconds"
                    if timed_out
                    else str(exc)
                )
                self.store.save_result(
                    key,
                    state["id"],
                    PaperResult(
                        requested=paper.identifier,
                        paper=paper,
                        status="failed",
                        steps=[
                            StepOutcome(
                                step="resolve",
                                status="failed",
                                issue=Issue(
                                    code="paper_timeout" if timed_out else "app_processing_error",
                                    message=message,
                                    retryable=timed_out,
                                ),
                            )
                        ],
                    ),
                )
                state["failures"].append({"id": paper.arxiv_id, "message": message})
        detail = self.store.detail(key)
        if detail and detail.get("warnings"):
            state["with_warnings"] += 1
        if (
            detail
            and detail["result"]
            and any(r["kind"] not in {"paper", "other"} for r in detail["result"]["resources"])
        ):
            state["with_resources"] += 1
        state["completed"] += 1
        self.store.save_run(state)
        return False

    async def close(self):
        # Serialize cancellation so repeated stop requests cannot interrupt cleanup.
        async with self._stop_lock:
            if self.task is not None and not self.task.done():
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    self.state.update(
                        status="interrupted",
                        phase="interrupted",
                        current=None,
                        finished_at=now(),
                        error="Collection stopped. Repeat the date range to continue.",
                    )
                    self.store.save_run(self.state)
            return self.state
