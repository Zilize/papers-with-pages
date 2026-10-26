"""Async metadata acquisition with caller-owned checkpoints and persistence."""

from collections.abc import AsyncIterator, Iterable
from datetime import date

import httpx

from pwp.errors import MetadataError, PaperNotFoundError, PwpError
from pwp.events import EventHandler, EventReporter
from pwp.harvest.http import MetadataTransport
from pwp.harvest.parse import parse_atom, parse_oai
from pwp.models import HarvestBatch, Issue, Paper, PaperLookup
from pwp.refs import parse_paper_ref

API_URL = "https://export.arxiv.org/api/query"
OAI_URL = "https://oaipmh.arxiv.org/oai"


class ArxivClient:
    """Reuse one client for API and OAI requests within one async event loop."""

    def __init__(
        self,
        *,
        user_agent: str = "papers-with-pages/0.1.0a1",
        client: httpx.AsyncClient | None = None,
        min_interval: float = 3.0,
        max_retries: int = 2,
        max_retry_wait: float = 60.0,
        max_response_bytes: int = 64 * 1024 * 1024,
        timeout: float = 30.0,
    ):
        self._transport = MetadataTransport(
            client=client,
            user_agent=user_agent,
            min_interval=min_interval,
            max_retries=max_retries,
            max_retry_wait=max_retry_wait,
            max_response_bytes=max_response_bytes,
            timeout=timeout,
        )

    async def __aenter__(self) -> "ArxivClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._transport.close()

    async def get_paper(self, reference: str, *, on_event: EventHandler | None = None) -> Paper:
        """Return a paper or raise a structured error; link presence is irrelevant."""
        result = (await self.get_papers([reference], on_event=on_event))[0]
        if result.paper is not None:
            return result.paper
        assert result.issue is not None
        error_type = PaperNotFoundError if result.issue.code == "not_found" else PwpError
        raise error_type(
            result.issue.code,
            result.issue.message,
            retryable=result.issue.retryable,
            retry_after_s=result.issue.retry_after_s,
        )

    async def get_papers(
        self,
        references: Iterable[str],
        *,
        batch_size: int = 50,
        on_event: EventHandler | None = None,
    ) -> list[PaperLookup]:
        """Preserve input order and duplicates, with one outcome per valid reference."""
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        if isinstance(references, str):
            raise TypeError("Pass an iterable of references, not a single string")
        requested = [parse_paper_ref(reference) for reference in references]
        reporter = EventReporter("get_papers", on_event)
        reporter.emit("step_started", total=len(requested), unit="papers")
        results: list[PaperLookup] = []
        for start in range(0, len(requested), batch_size):
            batch = requested[start : start + batch_size]
            identifiers = list(dict.fromkeys(ref.identifier for ref in batch))
            batch_issue = None
            try:
                papers, invalid = await self._fetch_entries(identifiers)
            except PwpError as exc:
                results.extend(PaperLookup(requested=ref, issue=exc.issue) for ref in batch)
                batch_issue = exc.issue
            else:
                batch_issue = next(iter(invalid.values()), None)
                for ref in batch:
                    if ref.identifier in invalid:
                        results.append(PaperLookup(requested=ref, issue=invalid[ref.identifier]))
                        continue
                    matches = [
                        paper
                        for paper in papers
                        if paper.arxiv_id == ref.arxiv_id
                        and (ref.version is None or ref.version == paper.version)
                    ]
                    paper = max(matches, key=lambda item: item.version or 0) if matches else None
                    issue = (
                        None
                        if paper
                        else Issue(
                            code="not_found", message=f"No metadata returned for {ref.identifier}"
                        )
                    )
                    results.append(PaperLookup(requested=ref, paper=paper, issue=issue))
            reporter.emit(
                "step_progress",
                completed=len(results),
                total=len(requested),
                unit="papers",
                issue=batch_issue,
            )
        failed = sum(result.issue is not None for result in results)
        summary = (
            Issue(
                code="lookup_failures",
                message=f"{failed} of {len(results)} lookups failed; inspect results",
            )
            if failed
            else None
        )
        reporter.emit(
            "step_failed" if results and failed == len(results) else "step_completed",
            completed=len(results),
            total=len(requested),
            unit="papers",
            issue=summary,
        )
        return results

    async def _fetch_entries(self, identifiers: list[str]) -> tuple[list[Paper], dict[str, Issue]]:
        """Fetch one ID list, bisecting malformed responses to isolate the bad references.

        Transport failures propagate unchanged; they are not attributable to an entry.
        """
        try:
            content = await self._transport.get(
                API_URL,
                {"id_list": ",".join(identifiers), "max_results": str(len(identifiers))},
            )
            return parse_atom(content), {}
        except MetadataError as exc:
            if len(identifiers) == 1:
                return [], {identifiers[0]: exc.issue}
        middle = len(identifiers) // 2
        left, left_invalid = await self._fetch_entries(identifiers[:middle])
        right, right_invalid = await self._fetch_entries(identifiers[middle:])
        return left + right, {**left_invalid, **right_invalid}

    async def harvest(
        self,
        *,
        set_spec: str | None = None,
        metadata_updated_from: date | None = None,
        metadata_updated_until: date | None = None,
        resumption_token: str | None = None,
        on_event: EventHandler | None = None,
    ) -> AsyncIterator[HarvestBatch]:
        """Yield OAI batches; save records before advancing the supplied checkpoint."""
        if resumption_token is not None:
            if not resumption_token.strip():
                raise ValueError("resumption_token must not be empty")
            if any(
                value is not None
                for value in [set_spec, metadata_updated_from, metadata_updated_until]
            ):
                raise ValueError("A resumption token cannot be combined with initial query fields")
        if (
            metadata_updated_from is not None
            and metadata_updated_until is not None
            and metadata_updated_from > metadata_updated_until
        ):
            raise ValueError("metadata_updated_from must not be after metadata_updated_until")
        params = {"verb": "ListRecords", "metadataPrefix": "arXiv"}
        if set_spec is not None:
            params["set"] = set_spec
        if metadata_updated_from is not None:
            params["from"] = metadata_updated_from.isoformat()
        if metadata_updated_until is not None:
            params["until"] = metadata_updated_until.isoformat()
        if resumption_token is not None:
            params = {"verb": "ListRecords", "resumptionToken": resumption_token}
        reporter = EventReporter("harvest", on_event)
        reporter.emit("step_started", unit="records")
        completed = 0
        seen_tokens = {resumption_token} if resumption_token else set()
        while True:
            try:
                content = await self._transport.get(OAI_URL, params)
                batch = parse_oai(content)
                if batch.next_token and batch.next_token in seen_tokens:
                    raise MetadataError("repeated_token", "OAI returned a previously used token")
            except PwpError as exc:
                reporter.emit("step_failed", issue=exc.issue)
                raise
            completed += len(batch.records)
            reporter.emit("step_progress", completed=completed, unit="records")
            yield batch
            if batch.next_token is None:
                break
            seen_tokens.add(batch.next_token)
            params = {"verb": "ListRecords", "resumptionToken": batch.next_token}
        reporter.emit("step_completed", completed=completed, unit="records")
