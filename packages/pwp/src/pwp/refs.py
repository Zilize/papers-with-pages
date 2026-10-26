"""Parse paper references without retrieving paper content."""

import re
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ID = re.compile(
    r"(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|"
    r"[a-zA-Z][a-zA-Z0-9.-]*/\d{2}(?:0[1-9]|1[0-2])\d{3})"
)
_REF = re.compile(rf"(?P<id>{_ID.pattern})(?:v(?P<version>[1-9]\d*))?")


class PaperRef(BaseModel):
    """An unversioned arXiv identifier and an optional explicit version."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    arxiv_id: str
    version: int | None = Field(default=None, ge=1)

    @field_validator("arxiv_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("Expected an unversioned arXiv identifier")
        return value

    @property
    def identifier(self) -> str:
        return self.arxiv_id + (f"v{self.version}" if self.version is not None else "")


def parse_paper_ref(value: str) -> PaperRef:
    """Accept an ID, arXiv prefix, or official abstract/PDF/HTML URL."""
    value = value.strip()
    if value.lower().startswith("arxiv:"):
        value = value[6:].strip()
    elif "://" in value:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
            or url.username is not None
            or url.password is not None
            or url.port not in {None, 80, 443}
        ):
            raise ValueError("Expected an official arXiv URL")
        parts = unquote(url.path).strip("/").split("/", 1)
        if len(parts) != 2 or parts[0] not in {"abs", "pdf", "html"}:
            raise ValueError("Expected an arXiv paper URL")
        value = parts[1]
        if parts[0] == "pdf":
            value = value.removesuffix(".pdf")
    match = _REF.fullmatch(value)
    if match is None:
        raise ValueError(f"Invalid arXiv reference: {value!r}")
    version = match.group("version")
    return PaperRef(arxiv_id=match.group("id"), version=int(version) if version else None)
