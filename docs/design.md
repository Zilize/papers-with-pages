# Package architecture

The library returns facts, candidates, evidence, artifacts, and execution outcomes.
Applications own durable storage, inclusion policies, scheduling, and recovery.

## Implemented layers

```text
arXiv metadata -> entry links -> bounded repository / project-page exploration
              -> page facts and unified main visuals -> optional hero and image files
              -> typed results / JSONL
```

| Module | Responsibility |
| --- | --- |
| `refs`, `harvest` | Reference parsing, API queries, OAI batches, protocol errors |
| `extract` | Pure extraction, conservative URL classification, evidence merging |
| `network` | Connection-time DNS policy, robots, redirects, request and byte limits |
| `github` | Repository metadata, README resolution, resource evidence |
| `page` | HTML facts, links, citations, author and institution declarations |
| `visuals` | Ranked image/video/embed candidates with previews and provenance |
| `discover` | Bounded traversal, association heuristics, cycle handling |
| `capture` | Managed browser visit and first-viewport hero screenshot |
| `media` | Validated image files and thumbnails in caller-owned directories |
| `resolve` | Single-paper composition and bounded completion-order batches |
| `events`, `models`, `serialization` | Data contracts and observations |
| `cli` | Command-line adapters and explicit file output |

The local gallery lives under `apps/local` with Python module name `app`, depends
on `pwp`, and owns its file persistence and single collection lifecycle. Its main
screen is a resource gallery; date-based harvest and progress live in a modal.
See [the app guide](../apps/local/README.md) for storage and recovery rules.

## Discovery

All queried papers are returned, regardless of links. Repository descriptions,
homepages, and READMEs can lead to project pages, and project pages can lead back to
repositories, datasets, models, and demos. Only repositories and project pages expand;
other types are terminal. Metadata entry links have depth zero.

Depth, resource visits, requests, and returned links have separate limits. Robots,
redirects, retries, and browser subresources count toward requests. Canonical repository
keys and visited URLs stop cycles. Budget exhaustion is a partial outcome. Cross-call
caches and restart checkpoints belong to applications.

Classification and association are separate. Paper-ID links and title matches provide
evidence, while multi-paper pages stay ambiguous. Personal profiles, templates, and
generic navigation are not confirmed resources. These are transparent heuristics,
not an official-resource classifier.

## Visuals and files

`VisualCandidate` separates media type from direct/embedded delivery. It retains
evidence, preview metadata, labels, dimensions, ranking reasons, and known alternative
URLs. Obvious decorative assets are excluded; posters stay attached to videos.
Applications choose what to display, including a hero fallback for empty candidate sets.

The hero is a first-viewport screenshot, never a full-page screenshot. Rendered DOM feeds
back into discovery, so dynamic links join the graph. The same visit produces the hero.
Video bodies are not downloaded.

Image files are validated, content-hashed, and written atomically. Each resolve invocation
uses a distinct child work directory. The core does not store result JSON, choose permanent
storage, delete previous outputs, or maintain a database. CLI output is explicit.

## Failures and progress

Results distinguish no links, unknown association, request failures, exhausted budgets,
rendering failures, and downloaded artifacts. Available data survives individual resource
failures. Final results carry outcomes; progress events are optional.

Callbacks are synchronous and should be lightweight. `EventCallbackError` wraps callback
exceptions with their cause, separating application bugs from resource errors.
Cancellation propagates. Callers must close batch iterators when stopping early so
remaining work is cancelled and owned clients are closed.

Events are neither durable nor exactly-once. Known counts are reported; unknown graph
totals are not guessed. Applications attach job IDs and persist their progress.

## Network policy

Metadata transport uses fixed arXiv endpoints. General fetching validates literal IPs
and all DNS answers in the connector resolver, which passes numeric addresses directly
to the connection layer. Redirects are checked at each hop. Authentication headers are
stripped across origin changes, including HTTPS-to-HTTP downgrades.

Browser HTTP is fulfilled through that fetcher. Service workers, WebSockets, write
methods, video body downloads, and fallback proxy traffic are blocked. Robots policies,
request budgets, byte limits, and timeouts bound requests. The package does not fetch
paper HTML/PDF. Browser sandboxing and OS-level egress isolation remain additional
controls for hostile websites; this is not a complete virtual-machine sandbox.

Only public destinations on ports 80 and 443 are allowed. Ambient proxies and cookies
are not adopted. Networks returning synthetic non-public IPs for public hosts receive
`blocked_dns`; use normal public DNS and reachable public routes. Authenticated,
streaming, anti-bot, and unusual dynamic sites may yield partial results.

## Validation and scope

Offline fixtures cover metadata, graph cycles and limits, README links, association,
images/videos/embeds, artifacts, CLI, callbacks, and cancellation. Opt-in Chromium tests
check dynamic DOM, hero dimensions, and blocked private requests. CI includes a browser job.

The package, CLI, and separate local gallery are implemented. Paper full text, LLM
processing, institution normalization, video downloading/transcoding, and database
integration remain outside the current scope.
