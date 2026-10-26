# Repository guidance

- Write code comments, docstrings, tests, and documentation in English.
- Keep this repository self-contained. Do not introduce private deployment details,
  unrelated product names, machine-specific paths, or credentials.
- Focus the current milestone on `packages/pwp`. Application development belongs to
  a later phase unless the task explicitly requests it.
- Keep storage, scheduling, inclusion policies, and durable job state outside the core.
- Do not retrieve paper full text. Preserve no-link papers and explicit failure outcomes.
- Main-visual discovery will unify images and videos, including embedded delivery.
  Hero capture means a first-viewport screenshot, not a full-page screenshot.
- Use offline fixtures and mocked transports for the default tests. Live checks must
  remain bounded and respect upstream request limits.
- After relevant changes, run `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `uv build --package papers-with-pages`.
