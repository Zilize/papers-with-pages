# Local gallery

## Explicit trusted proxy

For a local HTTP proxy, including TUN/Fake-IP environments:

```sh
uv run pwp-app --trusted-proxy http://127.0.0.1:7890
```

Destination DNS and routing are delegated to this explicitly trusted proxy. The
proxy is responsible for destination network policy; the app cannot validate IPs
resolved behind it. Literal private URLs, redirect checks, TLS verification, robots
rules, and budgets remain enforced. Browser subresources use the same fetcher.
There is no direct fallback. Supported proxies use HTTP with a loopback or public
numeric IP and explicit port, without credentials. SOCKS is not supported.
System VPN settings and the metadata client's transport are unchanged. Omit the
option to retain strict direct-connection DNS checks.

A single-user gallery for paper resources. Collection runs in a modal; the main
screen stays focused on projects, code, datasets, and demos. No database or frontend
build tool is required. The import package is `app`, separate from `pwp`.

The gallery opens on Projects by default. The filter icon opens resource type,
inclusive first-publication date range, and arXiv category filters. Categories
come from the saved collection and match primary or secondary tags. Filters
combine with search; Clear filters preserves the search query. Cards render in batches of 18 as you
scroll, with a Load more button as a keyboard-accessible fallback. Search and
filters cover the full collection and reset the visible batch. The gallery API
still returns the full metadata collection; pagination limits rendered cards. Use Grid for visual browsing or List for
compact scanning; press `/` to focus search. Paper details show metadata and resource links. Escape, the close
button, or a click outside the dialog returns to the gallery. Grid cards omit authors and abstracts; List shows both in full.
Authors also remain available in details and search. Visible cards rotate automatically:
page previews stay for six seconds, embedded videos for fifteen seconds, and direct
videos advance when playback ends. Videos start muted; browser restrictions may
require Play. Hover or keyboard focus pauses rotation, and Pause autoplay pauses
media too. Offscreen cards, hidden tabs, and open dialogs suspend playback.
Reduced-motion mode starts paused until explicitly enabled using the controls. Collection updates
reuse unchanged visible cards, preserving their media state. Motion follows the
system reduced-motion preference.

Code-only papers remain stored and can
be browsed through Code or All papers. Project links are heuristic candidates,
not verified associations. Known publication platforms, documentation, profiles,
and direct files are excluded; these exclusions also apply when displaying older
saved results, without modifying their original evidence.

From the repository root:

```sh
uv sync --all-packages
uv run playwright install chromium
uv run pwp-app
```

Open <http://127.0.0.1:8000>. Use `--port 8001` or `--data-dir ./my-collection`
when needed. On Linux, use `playwright install --with-deps chromium` if system
browser dependencies are missing. Keep the server running while collecting.

## Collection

Use **Stop collection** in the running modal to cancel pending work while keeping
saved metadata and completed results. Repeat the date range to continue; closing
the modal leaves collection running.

Saved video candidates are selected again when the gallery is read. When no
confident main video exists, an explicit page player is used as a fallback;
hidden, experimental, and supplementary candidates remain excluded. No video
download or full recollection is needed for this display update.

Paper details show resource notices with the affected URLs. Successful captures
with asset warnings are distinguished from complete results without warnings.
Repeating a date range also retries results with transient resource warnings,
even if a screenshot was saved. Each paper is still attempted at most once per
run; upstream rate limits and robots restrictions remain enforced. Temporary
robots HTTP failures share a cooldown to avoid repeatedly probing the same host.

Each paper has a 180-second processing budget (browser cleanup may add a short
bounded grace period). Rendering has a 60-second budget after acquiring a browser
context, and visual readiness has a seven-second host-side deadline. Context
setup and cleanup are bounded too, so an unresponsive page cannot occupy a worker
indefinitely. Timed-out papers are saved as retryable failures, preserving any
previous successful result. The collection dialog's active-task section reports
the stage and elapsed time for each worker.
Finished captures cancel their own pending HTTP downloads before closing the page.
Cleanup does not cancel Playwright's internal route-completion notifications, which
would otherwise corrupt later operations on the shared browser.

Gallery scans run outside the async event loop. Concurrent reads share a scan,
with a two-second cache invalidated by collection progress. The connection pool
allows four connections per host, matching the request gate; GitHub API requests
still use a single slot and respect upstream cooldowns.

`GET /api/collection` includes a `network` snapshot for `web` and `github`, also
saved in the final run state. It records request counts, cancellation, retries,
response bytes, active waits, and cumulative seconds in host queues, pacing, and
network I/O. `connector_queue_s` is a subset of network time. These durations sum
concurrent operations; they are not wall-time percentages. `completed` counts HTTP
responses, including HTTP error statuses, not successful papers. No token or request
headers are included. Discovery progress distinguishes repository and page inspection.
GitHub statistics also include the most recent quota limit, remaining requests,
reset timestamp, response status, and current cooldown seconds. Missing quota headers
are reported as null, not zero.

Gallery reads and pointer replacement are synchronized in-process, with bounded
retries for transient file-sharing conflicts. Persistent permission errors remain
visible as retryable HTTP 503 responses rather than being treated as missing papers.

Choose inclusive **metadata update dates** and an optional arXiv archive in the
collection modal. These are OAI-PMH datestamps, not publication dates. Broad archives
such as `cs` are OAI sets, not individual subject filters such as `cs.CV`.
Metadata is harvested first, then an adaptive scheduler starts with at most two
paper workers. Every two seconds it samples system CPU, available memory, and event-loop
lag. Two healthy samples allow one additional worker, up to the CPU count and
`--max-workers` (default 8; supported range 1–16). CPU at 85%, memory at 90%,
or 250 ms event-loop lag reduces admission; active work
finishes normally. Growth requires CPU below 65%, at least 2 GiB available memory,
and lag below 100 ms. The memory ceiling reserves 1 GiB and allows approximately
512 MiB of available headroom per worker. These are conservative heuristics, not
hard per-process memory guarantees. Unavailable metrics limit admission to two.
Use `--max-workers 2` to retain the previous paper concurrency ceiling.
Workers share one lazy Chromium session with at most six isolated page contexts
(configurable with `--render-pages`, range 1–16, capped by the worker ceiling).
Use `--render-pages 4` for a more conservative browser limit. This is a fixed
browser ceiling; adaptive admission controls the number of active paper tasks.
They share one HTTP fetcher with up to twice the worker
ceiling in web connections (minimum four), plus one separate GitHub API connection.
Page assets cannot consume the GitHub connection pool. Host cooldowns affect only
that host, without reducing the global paper-worker target. Per-host gates, spacing, retries,
and robots rules remain unchanged. Concurrent checks share each origin's robots
lookup. The collection modal reports active workers and the current target. Each paper retains its
own request budget. Chromium starts only if rendering is needed and closes after
the collection; a disconnected browser is rebuilt on the next page request. The
modal reports counts instead of a fabricated percentage during pagination. It
can be closed and reopened without stopping the operation. Completed papers enter
the gallery as they become available. Resource-free papers are saved but hidden.

One collection and one server process can own a data directory at a time. An OS
file lock prevents two instances from modifying the same collection. Shutdown or
crash marks unfinished runs as interrupted. Repeat the date range to continue;
unchanged, fully processed papers are reused. Partial and failed attempts are
retried. The refresh checkbox reprocesses completed papers too. There is no
automatic background scheduling or network-step checkpoint resume.

## Files and update rules

```text
data/
  papers/<hex-encoded-unversioned-arxiv-id>/
    metadata.json
    current.json
    latest.json
    results/<run-id>.json
  assets/<paper-key>/<run-id>/...
  runs/<run-id>.json
  runs/<run-id>/batch-000000.json
```

Raw batches retain deleted and invalid records. Metadata merges by OAI datestamp.
Each result is an immutable attempt; `latest.json` tracks the latest result, while
`current.json` points to the displayed one. A complete result replaces the current
one. A partial result is displayed only if there is no previous display result;
otherwise the previous card remains with a stale indicator. Deleted records hide
the card. A complete, resource-free result is valid and reusable.

Metadata fingerprints exclude transport provenance and the OAI datestamp. Changes
to actual paper metadata invalidate reuse. Duplicate and overlapping date ranges
merge by unversioned arXiv ID. Old-style IDs containing `/` are safely encoded.
Media paths inside results are relative to `assets`, so the whole data directory
can be moved. Result files and display pointers use atomic replace after writing;
assets are written before pointers switch. The gallery reads these files directly,
with no second persistent search index. Old attempts and unused assets are retained;
there is no automatic destructive cleanup. Back up the data directory with the app
stopped for a consistent snapshot. Do not edit files during a collection.

The server binds to loopback and is intended for local use, without authentication.
The default cover is the first-viewport hero screenshot of the primary project page.
The app no longer downloads static teaser images. Previously downloaded images are
retained on disk but are not used as covers. Main-page native videos and YouTube/Vimeo
videos are ranked, and at most one main video appears alongside the hero. Explicit
title-area Video actions outrank teaser/overview labels and early large players.
Hidden/footer media and experimental result sections are ineligible. DOM placement
is a heuristic, not measured viewport visibility. The minimum selection score is 60;
ties preserve candidate order. All candidates and their `main_video_score` and
`main_video_reasons` remain in results. Old unscored results show only the hero until
reprocessed. Visible carousels advance automatically with muted video playback;
users can pause them. YouTube embeds send the local app origin as Referer and
player origin, as required by YouTube. Videos whose owners prohibit embedding still
require the original-site link. During hero capture only, up to two visible native videos may load
temporary byte ranges (2 MiB per request, 8 MiB reserved per page) to decode a frame.
The renderer waits up to five seconds for these frames, visible images, and fonts,
then pauses the videos for capture. Video files are not saved. Offscreen media stays
blocked; unavailable frames produce an explicit warning. Other embeds
remain external links. Remote images are not automatically loaded. Harvested strings are
rendered as text. Resource fetching uses `pwp` public-address validation, budgets,
robots handling, and rendering isolation. Networks that resolve public sites to
private or synthetic addresses must provide normal public DNS for resource fetching.

Resource discovery admits metadata entry links and explicit resource buttons reachable
from them. It excludes reference/related-work, dependency, template, and footer links.
Pages do not fan out into other project pages, and repositories do not fan out into
other repositories. GitLab URLs are retained as code resources without GitHub API
inspection. This deliberately conservative policy may miss unconventional links.
Saved results are filtered for presentation without deleting their original evidence.
PyPI package pages are excluded from project-page discovery. A failed main document
with HTTP 401/403/404/410/451, an explicit robots or network-policy denial, non-HTML
content, or a redirect loop/limit is retained as an ordinary link, not a project page.
This also applies when displaying older results. Timeouts, 429/5xx responses, and
individual asset failures retain the project page. Known analytics/ad requests are
skipped before consuming the page request budget and do not produce gallery warnings.
Noncritical page-asset issues appear in paper details; core resource failures remain
visible on cards. A successful hero with asset warnings does not alone mark a new
result partial. API rate-limit responses are distinguished when response headers
identify the limit; an unexplained 403 remains an access error.

## Development

```sh
uv run pytest apps/local/tests
uv run ruff check .
uv run ruff format --check .
uv build --package papers-with-pages-app
```

Set `PWP_BROWSER_TESTS=1` to include the local browser integration test. It uses
synthetic metadata and a local server, with no upstream network calls.


### Required GitHub token and retries

Set `PWP_GITHUB_TOKEN` (or the fallback `GITHUB_TOKEN`) in the launching process's
environment before starting `pwp-app`. Use a personal access token with public
repository read access only. Do not put tokens in command-line arguments or data files.
The app forwards the token only to repository inspection; it is not included in
collection requests, saved results, or browser responses.

The fetcher respects GitHub quota reset and Retry-After headers. During cooldown,
repository enrichment returns a retryable outcome with remaining seconds while other
sites can continue. Repeat the date range after cooldown to retry partial results;
this is not an automatic scheduled retry. Cooldown state lasts for the fetcher session.
Regular web origins allow four in-flight requests with paced starts; GitHub API calls
remain serialized. Render steps retain all issues, including failing asset URLs,
and screenshot failures take priority over supplementary asset warnings. Previously
saved results with only a first issue cannot recover missing diagnostics.

You can also open Collect papers > GitHub token (required) to set or clear a token in
the UI. The server can start without a token, but collection is disabled until one is
configured. The collection API also rejects starts without a token.
UI changes are saved in the system credential store (Windows Credential Locker,
macOS Keychain, or Linux Secret Service), scoped to the resolved data directory.
Saved tokens take precedence over the startup environment token after restart.
Clearing removes the stored credential; an environment token is still a fallback
on the next restart. Deleting collection files does not delete the credential.
No plaintext fallback is used. If the credential service is unavailable, the server
can still start using its environment token; a failed save leaves the current token
unchanged and reports an error. Settings responses expose only configuration and
persistence status. Tokens cannot be changed during collection; setting a token does
not verify it with GitHub. Previously memory-only UI tokens must be saved once after
upgrading to this version.

