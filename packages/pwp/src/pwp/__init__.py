"""Metadata-first research resource discovery tools."""

from pwp.browser import RenderSession
from pwp.capture import capture_hero, render_page
from pwp.discover import discover_resources
from pwp.errors import MetadataError, PaperNotFoundError, PwpError, RequestError
from pwp.events import EventCallbackError, EventHandler, ProgressEvent
from pwp.extract import classify_link, extract_metadata_links, merge_links, normalize_url
from pwp.github import inspect_repo
from pwp.harvest import ArxivClient
from pwp.media import create_thumbnail, download_image
from pwp.models import (
    Author,
    Citation,
    DiscoveryLimits,
    DiscoveryResult,
    HarvestBatch,
    HarvestRecord,
    Issue,
    LinkEvidence,
    MediaArtifact,
    Paper,
    PaperLookup,
    PaperResult,
    ProjectPage,
    Repository,
    ResourceLink,
    StepOutcome,
    VisualCandidate,
    VisualSource,
)
from pwp.network import RequestBudget, SafeFetcher
from pwp.page import inspect_page, parse_bibtex, parse_project_page
from pwp.refs import PaperRef, parse_paper_ref
from pwp.resolve import resolve, resolve_many
from pwp.serialization import from_jsonl, to_jsonl
from pwp.visuals import discover_visuals, select_main_video

__all__ = [
    "RenderSession",
    "EventCallbackError",
    "SafeFetcher",
    "RequestBudget",
    "Citation",
    "DiscoveryLimits",
    "DiscoveryResult",
    "MediaArtifact",
    "PaperResult",
    "ProjectPage",
    "Repository",
    "StepOutcome",
    "VisualCandidate",
    "VisualSource",
    "capture_hero",
    "render_page",
    "discover_resources",
    "inspect_repo",
    "create_thumbnail",
    "download_image",
    "inspect_page",
    "parse_bibtex",
    "parse_project_page",
    "resolve",
    "resolve_many",
    "from_jsonl",
    "to_jsonl",
    "discover_visuals",
    "select_main_video",
    "ArxivClient",
    "Author",
    "EventHandler",
    "HarvestBatch",
    "HarvestRecord",
    "Issue",
    "LinkEvidence",
    "MetadataError",
    "Paper",
    "PaperLookup",
    "PaperNotFoundError",
    "PaperRef",
    "ProgressEvent",
    "PwpError",
    "RequestError",
    "ResourceLink",
    "classify_link",
    "extract_metadata_links",
    "merge_links",
    "normalize_url",
    "parse_paper_ref",
]
