# pwp

### Reusing Chromium

```python
from pwp import RenderSession, resolve

async with RenderSession(concurrency=2) as session:
    result = await resolve(paper, work_dir="./artifacts", render_session=session)
```

Pass the same session to concurrent calls to `render_page`, `resolve`, or
`resolve_many`. Chromium starts lazily, contexts are isolated per page, and the
session limits simultaneous contexts independently of paper/HTTP concurrency.
Use a session on one event loop and close it after awaiting or cancelling callers.
Disconnected browsers are recreated on the next lease; the interrupted page is
reported as a failure rather than silently retried. Single `resolve` calls reuse
a session across their pages. `resolve_many` creates one shared session by default
and closes it on completion or generator closure. HTTP-only runs never launch it.

Explicit proxy support: use `SafeFetcher(trusted_proxy="http://127.0.0.1:7890")`
or pass `trusted_proxy` to `resolve` / `resolve_many`. With an existing fetcher,
configure the proxy on that fetcher instead. Destination DNS and network policy
are delegated to the trusted proxy; URL restrictions, TLS, robots, and request
limits remain active. No automatic proxy selection or direct fallback occurs.
See [proxy setup and trust boundaries](../../apps/local/README.md).

A Python library for metadata-first research resource discovery. Import it as `pwp`.
The package implements metadata acquisition, bounded resource discovery, project-page
parsing, main-visual discovery, hero capture, image artifacts, and composition.

Install `./packages/pwp` for HTTP tools, or `./packages/pwp[render]` followed by
`python -m playwright install chromium` for browser rendering. The optional browser
dependency is imported only when a rendering function is called.

## Fetch metadata

```python
from pwp import ArxivClient, extract_metadata_links

async with ArxivClient() as client:
    paper = await client.get_paper("arXiv:2410.12345v1")
    links = extract_metadata_links(paper)
    json_text = paper.model_dump_json(indent=2)

    results = await client.get_papers(["2410.12345", "2410.99999"])
    for result in results:
        if result.paper is not None:
            print(result.paper.title)
        else:
            print(result.requested.identifier, result.issue.code)
```

Use these statements inside an async function. `get_papers` returns one `PaperLookup`
per input, preserving duplicates and input order. Invalid reference syntax raises
`ValueError` before any requests; missing papers and request/parser failures become
explicit per-paper issues. `get_paper` raises `PaperNotFoundError` or `PwpError` on failure.

API Atom entries are normalized to `Paper`. Missing optional metadata stays absent.
Dates retain their upstream precision: OAI date-only values remain dates, and API
timestamps remain datetimes. The OAI format currently used does not supply a version
number or a dedicated primary-category field; these are not inferred.

## Harvest incremental metadata

```python
from datetime import date

from pwp import ArxivClient

async with ArxivClient() as client:
    async for batch in client.harvest(
        set_spec="cs:cs:CV",
        metadata_updated_from=date(2026, 10, 1),
        metadata_updated_until=date(2026, 10, 2),
    ):
        for record in batch.records:
            print(record.status, record.identifier)
        # Persist the records before saving batch.next_token in your application.
```

The date bounds select metadata record modifications, not paper publication dates.
Each `HarvestRecord` is `active`, `deleted`, or `invalid`; no-link papers and failed
record parses are not discarded. An empty range is a successful empty batch.
Protocol errors such as `badResumptionToken` raise `MetadataError`.

To resume a valid token, call `client.harvest(resumption_token=token)` without other
query fields. Tokens may expire. Store durable synchronization information in the
application and use overlap plus idempotent writes when restarting a harvest.
Exhausting the iterator produces a completion event; stopping it early does not.

## Extract entry links

```python
from pwp import Paper, extract_metadata_links

paper = Paper(
    arxiv_id="2410.12345",
    title="A synthetic example",
    abstract="Code: https://github.com/example/research-project.",
    comments="Project: https://example.github.io/research-project/",
)
links = extract_metadata_links(paper)
```

Extraction is a pure operation on `abstract` and `comments`. It recognizes explicit
HTTP(S) links and `www.` URLs, handles surrounding punctuation, and preserves evidence
from both fields when URLs repeat. Scheme-less arbitrary domains and broken line-wrap
URLs are not reconstructed in this version.

Classification is conservative and based on known URL shapes. An unknown custom host
is `other`, not automatically a project page. A classification is not proof that a
resource belongs to a paper; extracted links remain candidates. URL normalization
preserves paths, query parameters, and fragments, and does not resolve redirects.
Extraction does not make URLs safe to fetch; network validation belongs to future
resource-fetching tools.

## Observe progress

```python
def on_event(event):
    print(event.type, event.step, event.completed, event.total)


async with ArxivClient() as client:
    results = await client.get_papers(["2410.12345"], on_event=on_event)
```

Callbacks run synchronously and should be fast. Queue slow writes in your application.
Callback exceptions are wrapped in `EventCallbackError` with their original cause;
cancellation propagates unchanged. Events are observations,
not durable job state. Returned models remain the source of truth for outcomes.
Harvest progress counts records, with no invented total or percentage.
Lookup progress counts processed inputs, including failures. A partly successful lookup
ends with `step_completed` and an issue summary; an entirely failed lookup ends with
`step_failed`. Inspect individual `PaperLookup` values for retry decisions.

## Request behavior

Reuse one `ArxivClient` per event loop. It serializes API and OAI requests, spaces them
at least three seconds apart by default, applies timeouts, limits response size, and
retries transient failures. `Retry-After` is honored; a delay exceeding
`max_retry_wait` returns a retryable issue rather than blocking indefinitely. The
client retains that backoff for subsequent calls, so an immediate retry cannot bypass it.

The limiter is per client, not process-wide or distributed. Applications must coordinate
multiple clients and machines. `min_interval=0` is intended for mocked tests only.
Use an identifying `user_agent` with contact details for real collection.

An injected `httpx.AsyncClient` remains caller-owned; otherwise the context manager
closes its own client. Redirects are not followed. This client only requests the fixed
arXiv metadata endpoints; it is not a general-purpose webpage downloader.

Protocol references: [arXiv API](https://info.arxiv.org/help/api/user-manual.html),
[OAI-PMH](https://info.arxiv.org/help/oa/index.html), and
[request limits](https://info.arxiv.org/help/api/tou.html).

## Resource discovery

```python
from pwp import DiscoveryLimits, discover_resources, inspect_repo

resources = await discover_resources(
    paper,
    limits=DiscoveryLimits(max_depth=2, max_resources=12, max_requests=80, max_links=200),
    on_event=on_event,
)
repository = await inspect_repo("https://github.com/example/project", token=None)
```

Repository inspection reads GitHub metadata and the README, resolves relative links,
and returns stars, license, and update information when available. Optional authentication
is sent only to GitHub API requests. README failure preserves available repository facts.

Discovery expands candidate repositories and project pages. Datasets, models, demos,
and videos are terminal resources. Entry links have depth zero. Unknown metadata URLs
and GitHub Pages roots may be inspected as candidates; their presence does not confirm
association. Depth, resource, request, and returned-link limits are independent.
Requests include robots, redirects, retries, and browser subresources. Budget exhaustion
is partial, not an empty result. No cross-run cache or checkpoint is saved.

## Pages and main visuals

```python
from pwp import discover_visuals, inspect_page, parse_project_page

page = await inspect_page("https://project.example/")
parsed = parse_project_page(html_text, "https://project.example/")
visuals = discover_visuals(html_text, "https://project.example/")
```

Page facts include title, description, resource links, raw and parsed BibTeX, and explicitly
declared author/institution metadata. They remain page facts; a multi-paper page does not
automatically confirm all resources for one paper.

`VisualCandidate` separates `image`/`video`/`unknown` media type from `direct`/`embed`
delivery. It carries evidence, ranking reasons, known dimensions, posters, and confirmed
alternative URLs. Video posters stay with videos; obvious logos, tiny images, and hidden
elements are excluded. The application selects candidates; an empty list is valid.

## Rendering and image artifacts

```python
from pwp import create_thumbnail, download_image, render_page

rendered = await render_page("https://project.example/", work_dir="./artifacts")
hero = rendered.screenshot
candidate = next((v for v in rendered.page.visuals if v.media_type == "image"), None)
if candidate:
    image = await download_image(candidate, work_dir="./artifacts")
    thumbnail = create_thumbnail(image, work_dir="./artifacts", width=720)
```

One browser visit supplies DOM extraction and a first-viewport PNG hero. Omit `work_dir`
from `render_page` for DOM-only rendering. `RenderResult` contains the parsed page, HTML,
optional screenshot, and issues. `capture_hero` also accepts a caller-owned Playwright
page; the caller is responsible for how that page was loaded. There is no long screenshot.

Files are validated, content-hashed, and atomically written inside the supplied directory.
`MediaArtifact` includes dimensions, hash, timestamp, and capture parameters. Paths identify
local files; applications rewrite paths and archive/upload files as needed. Downloads are
explicit. Managed rendering identifies video requests without downloading video bodies.

## End-to-end composition

```python
from pwp import resolve, resolve_many

result = await resolve(paper, work_dir="./artifacts", download_images=True)
http_only = await resolve(paper, render=False)

async for result in resolve_many([paper, "2410.12345"], concurrency=2, work_dir="./artifacts"):
    print(result.requested, result.status)
```

Existing metadata avoids a repeated API call. Rendered links participate in discovery,
including dynamically inserted repository/dataset links. Each project graph visit renders
once; `max_pages` caps browser visits and `max_images` caps explicit image downloads.
Rendering failure falls back to HTTP parsing and remains in `steps`.

Default `render=True` requires `work_dir`. Each invocation uses a unique child directory;
no result JSON or durable job file is written by the core. `render=False` needs no directory
unless downloading images. Nested operations share the request budget.

`resolve_many` limits in-flight tasks, shares clients, and yields in completion order.
Each result retains its requested reference. Close the async iterator when stopping early
(for example with `contextlib.aclosing`) to cancel remaining work and close owned clients.

## CLI reference

| Command | Output |
| --- | --- |
| `pwp get ID` | `Paper` JSON |
| `pwp harvest --set cs:cs:CV --from 2026-10-01` | `HarvestBatch` JSONL with records and continuation tokens |
| `pwp extract papers.jsonl` | Paper plus extracted links per input |
| `pwp extract batches.jsonl --input-model batch` | Harvest record plus links, retaining deleted/invalid records |
| `pwp discover ID` | `DiscoveryResult` JSON |
| `pwp inspect-page URL` | HTTP-parsed `ProjectPage` JSON |
| `pwp resolve ID --work-dir ./artifacts` | Result JSON plus hero files |
| `pwp resolve ID --no-render` | HTTP-only result without implicit files |
| `pwp capture URL --work-dir ./artifacts` | Page, hero artifact, and issues |
| `pwp export results.jsonl --format json` | Validated JSON array; `--model paper` also supported |

Use `-o PATH` for output or omit it for stdout. Metadata, discovery, and resolve commands
support `--progress` JSON events on stderr. `harvest --token TOKEN` excludes initial filters.
`--max-batches` bounds harvesting and leaves the continuation token in the last batch.
The CLI does not persist checkpoints. JSON/JSONL conversion is also available through
`to_jsonl` and `from_jsonl` without choosing a storage destination.

## General network policy

`SafeFetcher` validates literal addresses and all DNS answers at the connector boundary.
The connector uses the validated numeric addresses without a second unvalidated lookup.
Redirect targets are checked separately. Private, loopback, link-local, multicast, and
selected transition addresses are rejected, and ports are limited to 80 and 443.
There are no ambient proxy or cookie credentials. Robots rules, timeouts, host spacing,
bounded retries, response byte limits, and request budgets are applied.

GitHub API requests use one separate connector slot; web resources share the configured
`max_connections` pool with at most four connections per host. `fetcher.statistics()`
returns bounded aggregate timing and counter snapshots for these two lanes, without
URLs or credentials. Timings include host-slot queues, pacing, network work, and the
connector-queue subset. Durations accumulate across concurrent requests; active-phase
counters identify waits still in progress. HTTP responses counted as completed may
still have an error status. Cancelled requests release their slots and remain counted.

Managed browser HTTP is fulfilled through this fetcher. Service workers, WebSockets,
write methods, video body downloads, and fallback proxy traffic are blocked. Authenticated,
streaming, anti-bot, and unusual dynamic pages may produce partial results. Browser and
OS-level isolation remain appropriate for hostile sites.

Networks returning synthetic non-public DNS answers for public hosts receive `blocked_dns`.
Use normal public DNS and reachable public routes. The library does not silently bypass
the policy or adopt a local proxy. Fixed-endpoint metadata access uses its own transport
and may behave differently on such networks.

