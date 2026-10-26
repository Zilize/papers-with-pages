# Papers with Pages

Python tools for finding the resources and main visuals behind research papers.

Start with arXiv metadata, follow bounded links between repositories and project pages,
discover datasets and models, and capture a project-page hero screenshot. Images,
videos, and embedded demonstrations share one main-visual candidate model. No paper
full text, database, hosted service, or application account is required.

## Install from a checkout

```sh
uv sync --all-packages --all-extras
uv run --all-extras playwright install chromium
```

Alternatively, install `./packages/pwp[render]` with pip, then run
`python -m playwright install chromium`. Omit `[render]` for HTTP-only usage.
Python 3.11+ is required. On Linux, use `playwright install --with-deps chromium` for
browser system dependencies. No PyPI publication is implied by this checkout.

## Use the library

To browse a local gallery instead, run `uv run pwp-app` after installing from the
checkout and open <http://127.0.0.1:8000>. Use the collection modal to harvest an
inclusive metadata update date range. Results and main visuals are saved locally;
see the [local app guide](apps/local/README.md) for storage and recovery rules.

```python
import asyncio
from pwp import resolve


async def main():
    result = await resolve("2410.12345", work_dir="./artifacts")
    for resource in result.resources:
        print(resource.kind, resource.url, resource.relation)
    for visual in result.visuals:
        print(visual.media_type, visual.delivery, visual.url)
    for artifact in result.media:
        print(artifact.kind, artifact.path)


asyncio.run(main())
```

Applications own result persistence, filtering, scheduling, and recovery. Papers without
links remain normal results. Individual resource failures are retained in `result.steps`.
Use `resolve(paper, render=False)` for HTTP-only discovery without implicit files.

## Use the CLI

```sh
uv run --all-extras pwp get 2410.12345 -o paper.jsonl
uv run --all-extras pwp extract paper.jsonl -o links.jsonl
uv run --all-extras pwp discover 2410.12345 -o resources.json
uv run --all-extras pwp resolve 2410.12345 --work-dir ./artifacts --progress -o result.json
uv run --all-extras pwp capture https://project.example/ --work-dir ./artifacts
```

Replace example references and URLs with your papers and project pages. Progress goes
to stderr; stdout and output files contain JSON.

## Package capabilities

| Capability | Public entry points |
| --- | --- |
| Metadata and incremental harvest | `ArxivClient`, `parse_paper_ref` |
| Entry links | `extract_metadata_links`, `normalize_url`, `classify_link`, `merge_links` |
| Resource graph | `inspect_repo`, `inspect_page`, `discover_resources` |
| Page facts and citations | `parse_project_page`, `parse_bibtex` |
| Main visuals | `discover_visuals` |
| Browser hero capture | `render_page`, `capture_hero` |
| Image artifacts | `download_image`, `create_thumbnail` |
| Composition | `resolve`, `resolve_many` |
| Serialization | `to_jsonl`, `from_jsonl` |

Videos are discovered, not downloaded or transcoded. Screenshots capture the first
viewport only. Association and visual ranking use heuristics with evidence; applications
retain the final selection decision.

## Development

```sh
uv run --all-extras pytest
uv run --all-extras ruff check .
uv run --all-extras ruff format --check .
uv build --package papers-with-pages
```

Default tests are offline. Set `PWP_BROWSER_TESTS=1` to include Chromium tests; those use
intercepted synthetic pages and need no external site access.

The core package and CLI live under `packages/pwp`. The separate local gallery lives
under `apps/local`, with Python module name `app`. Comments, docstrings, and
documentation are in English.

- [API and CLI reference](packages/pwp/README.md)
- [Architecture and operational limits](docs/design.md)
- [Contributing](CONTRIBUTING.md)
- [MIT license](LICENSE)

Upstream metadata and linked assets retain their respective terms.
