"""Serializable metadata contracts; persistence belongs to the caller."""

from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pwp.refs import PaperRef


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Author(Model):
    name: str
    affiliations: list[str] = Field(default_factory=list)


class Paper(PaperRef):
    title: str
    abstract: str = ""
    authors: list[Author] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    primary_category: str | None = None
    comments: str | None = None
    published_at: date | datetime | None = None
    updated_at: date | datetime | None = None
    oai_datestamp: date | datetime | None = None
    doi: str | None = None
    journal_ref: str | None = None
    source: Literal["arxiv_api", "arxiv_oai", "user"] = "user"


ResourceKind = Literal[
    "repository", "project_page", "dataset", "model", "demo", "video", "paper", "other"
]


class LinkEvidence(Model):
    original_url: str
    source: Literal["abstract", "comments", "repository", "project_page"]
    source_url: str | None = None
    context: str = ""
    anchor: str = ""


class ResourceLink(Model):
    url: str
    kind: ResourceKind = "other"
    platform: str | None = None
    relation: Literal["candidate", "related"] = "candidate"
    evidence: list[LinkEvidence] = Field(default_factory=list)


class Issue(Model):
    resource_url: str | None = None
    code: str
    message: str
    retryable: bool = False
    retry_after_s: float | None = None
    http_status: int | None = None


class PaperLookup(Model):
    requested: PaperRef
    paper: Paper | None = None
    issue: Issue | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "PaperLookup":
        if (self.paper is None) == (self.issue is None):
            raise ValueError("Exactly one of paper or issue is required")
        return self


class HarvestRecord(Model):
    """Retain deleted and malformed records instead of silently losing them."""

    identifier: str
    datestamp: date | datetime | None = None
    sets: list[str] = Field(default_factory=list)
    status: Literal["active", "deleted", "invalid"] = "active"
    paper: Paper | None = None
    issue: Issue | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "HarvestRecord":
        if self.status == "active" and (self.paper is None or self.issue is not None):
            raise ValueError("Active records require a paper and no issue")
        if self.status == "deleted" and (self.paper is not None or self.issue is not None):
            raise ValueError("Deleted records have neither a paper nor an issue")
        if self.status == "invalid" and (self.issue is None or self.paper is not None):
            raise ValueError("Invalid records require an issue and no paper")
        return self


class HarvestBatch(Model):
    records: list[HarvestRecord] = Field(default_factory=list)
    response_date: datetime | None = None
    next_token: str | None = None
    token_expires_at: datetime | None = None


class VisualSource(Model):
    url: str
    delivery: Literal["direct", "embed"] = "direct"
    provider: str | None = None
    provider_id: str | None = None


class VisualCandidate(VisualSource):
    id: str
    media_type: Literal["image", "video", "unknown"]
    source_page_url: str
    evidence: list[str] = Field(default_factory=list)
    poster_url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    alternatives: list[VisualSource] = Field(default_factory=list)
    rank: int = 0
    selection_reason: str = ""
    main_video_score: int = 0
    main_video_reasons: list[str] = Field(default_factory=list)


class Citation(Model):
    raw: str
    entries: list[dict[str, str]] = Field(default_factory=list)
    issue: Issue | None = None


class PageAuthor(Model):
    name: str
    markers: list[str] = Field(default_factory=list)


class PageAffiliation(Model):
    name: str
    marker: str | None = None
    source: Literal["author_block", "logo", "json_ld"] = "author_block"
    # Footnote text when the institution comes from a note such as "work done at X".
    note: str | None = None


class AuthorBlock(Model):
    """Author and affiliation declarations near the page title, as written."""

    authors: list[PageAuthor] = Field(default_factory=list)
    affiliations: list[PageAffiliation] = Field(default_factory=list)
    context: str = ""


class ProjectPage(Model):
    url: str
    final_url: str
    http_status: int = 200
    title: str = ""
    description: str = ""
    canonical_url: str | None = None
    links: list[ResourceLink] = Field(default_factory=list)
    visuals: list[VisualCandidate] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    institutions: list[str] = Field(default_factory=list)
    author_block: AuthorBlock | None = None
    related_paper_ids: list[str] = Field(default_factory=list)


class Repository(Model):
    url: str
    full_name: str
    description: str = ""
    homepage: str | None = None
    stars: int | None = None
    license: str | None = None
    pushed_at: datetime | None = None
    readme_url: str | None = None
    links: list[ResourceLink] = Field(default_factory=list)
    related_paper_ids: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class MediaArtifact(Model):
    id: str
    kind: Literal["hero_screenshot", "image", "thumbnail"]
    path: Path
    source_url: str
    candidate_id: str | None = None
    content_type: str
    bytes: int
    sha256: str
    width: int
    height: int
    captured_at: datetime
    device_scale_factor: float | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None


class StepOutcome(Model):
    issues: list[Issue] = Field(default_factory=list)
    step: str
    status: Literal["completed", "partial", "failed", "skipped"]
    resource_url: str | None = None
    reason: str | None = None
    issue: Issue | None = None


class DiscoveryLimits(Model):
    max_depth: int = Field(default=2, ge=0, le=10)
    max_resources: int = Field(default=12, ge=1, le=1000)
    max_requests: int = Field(default=80, ge=1, le=10000)
    max_links: int = Field(default=200, ge=1, le=10000)


class DiscoveryResult(Model):
    resources: list[ResourceLink] = Field(default_factory=list)
    pages: list[ProjectPage] = Field(default_factory=list)
    repositories: list[Repository] = Field(default_factory=list)
    steps: list[StepOutcome] = Field(default_factory=list)
    limit_reached: bool = False


class PaperResult(Model):
    schema_version: str = "1"
    requested: str
    paper: Paper | None = None
    resources: list[ResourceLink] = Field(default_factory=list)
    pages: list[ProjectPage] = Field(default_factory=list)
    repositories: list[Repository] = Field(default_factory=list)
    visuals: list[VisualCandidate] = Field(default_factory=list)
    media: list[MediaArtifact] = Field(default_factory=list)
    steps: list[StepOutcome] = Field(default_factory=list)
    status: Literal["completed", "partial", "failed"] = "completed"
