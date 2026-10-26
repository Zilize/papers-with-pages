# Contributing

Start with the [architecture](docs/design.md). Keep reusable discovery tools in
`packages/pwp` and gallery, persistence, and task lifecycle changes in `apps/local`.

Use Python 3.11+, uv, pytest, and Ruff. Before submitting a change, run:

```sh
uv sync --all-packages
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build --package papers-with-pages
```

Write comments, docstrings, and documentation in English. Keep persistence and job
management outside the core package. Add regression tests for protocol edge cases or
behavioral fixes. Use synthetic fixtures or clearly attributed data, and avoid network
requests in the default test suite. Never include credentials or local environment paths.

