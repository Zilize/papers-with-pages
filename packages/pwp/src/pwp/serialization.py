"""JSONL conversion without selecting storage locations or managing a database."""

from collections.abc import Iterable, Iterator

from pydantic import BaseModel

from pwp.models import PaperResult


def to_jsonl(records: Iterable[BaseModel]) -> Iterator[str]:
    for record in records:
        yield record.model_dump_json() + "\n"


def from_jsonl(lines: Iterable[str], *, model=PaperResult) -> Iterator[BaseModel]:
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            yield model.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"Invalid JSONL record on line {number}: {exc}") from exc
