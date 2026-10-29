"""Curated registry of well-known research institutions and matching against it.

The registry is shipped as package data. Matching is offline: it maps declared
affiliation strings, page hosts, and repository owners to registry entries and reports
which rule matched, leaving inclusion decisions to callers.
"""

import json
import re
import unicodedata
from collections import defaultdict
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from pwp.models import Model, PaperResult

Slug = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")


class Institution(Model):
    id: str = Slug
    ror: str | None = Field(default=None, pattern=r"^https://ror\.org/0[a-z0-9]{6}[0-9]{2}$")
    names: list[str] = Field(min_length=1)
    acronyms: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    github_orgs: list[str] = Field(default_factory=list)
    parent: str | None = Field(default=None, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    country: str = Field(pattern=r"^[A-Z]{2}$")


def load_registry(path: str | Path | None = None) -> list[Institution]:
    """Load the bundled registry, or a caller-supplied file with the same layout."""
    if path is None:
        text = resources.files("pwp").joinpath("registry/institutions.json").read_text("utf-8")
    else:
        text = Path(path).read_text("utf-8")
    return [Institution.model_validate(item) for item in json.loads(text)["institutions"]]


MatchRule = Literal[
    "exact", "segment", "contains", "leading_name", "acronym", "domain", "github_org"
]
MatchSource = Literal[
    "arxiv", "citation_meta", "author_block", "logo", "json_ld", "page_host", "repository_owner"
]


class InstitutionMatch(Model):
    institution_id: str
    # Top of the parent chain, for example "google" for "google-deepmind".
    root_id: str
    # Rules from most to least specific: exact, segment, contains, leading_name, acronym.
    # Hosts and repository owners match by domain or github_org.
    rule: MatchRule
    alias: str
    text: str
    source: MatchSource
    source_url: str | None = None
    # For page declarations: how many listed authors are also paper authors.
    author_overlap: int | None = None


_WIDE = "぀-ヿ一-鿿가-힯"
# Wide scripts are matched per character so "清华大学" is found inside longer names.
_TOKEN = re.compile(rf"[{_WIDE}]|[^\W_{_WIDE}]+")
_SEGMENT = re.compile(r"[,;/|]|\s[-–—]\s")
_BRACKETS = re.compile(r"[()\[\]（）]")
_US_STATES = """alabama alaska arizona arkansas california colorado connecticut delaware florida
georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland
massachusetts michigan minnesota mississippi missouri montana nebraska nevada
new_hampshire new_jersey new_mexico new_york north_carolina north_dakota ohio oklahoma oregon
pennsylvania rhode_island south_carolina south_dakota tennessee texas utah vermont virginia
washington west_virginia wisconsin wyoming""".split()
_STATE_CODES = """al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne
nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc""".split()
_COUNTRIES = """usa|us|u s a|united states|united states of america|america|uk|u k|
united kingdom|england|scotland|wales|china|p r china|pr china|prc|
peoples republic of china|hong kong|hong kong sar|hong kong sar china|macau|macao|taiwan|
japan|korea|south korea|republic of korea|singapore|india|israel|canada|germany|france|
switzerland|netherlands|belgium|italy|spain|portugal|austria|sweden|norway|denmark|finland|
ireland|poland|czech republic|czechia|australia|new zealand|brazil|saudi arabia|uae|
united arab emirates|qatar|turkey|greece|中国|美国|英国|日本|韩国|新加坡"""
# Words that name a unit inside an institution rather than another campus.
_SUBUNIT = set(
    """school college department dept faculty lab labs laboratory laboratories institute
    center centre hospital medical medicine health research graduate division program
    group and""".split()
)


def _tokens(text: str) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    text = text.replace("&", " and ").replace("'", "").replace("’", "")
    return tuple(token for token in _TOKEN.findall(text) if token != "the")


_PLACES = {
    _tokens(place.replace("_", " "))
    for place in [*_US_STATES, *_STATE_CODES, *_COUNTRIES.replace("\n", "").split("|")]
}
_STATE_TOKENS = {_tokens(state.replace("_", " ")) for state in _US_STATES}


def _host(url: str) -> str:
    host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    return host.removeprefix("www.")


class InstitutionIndex:
    """Lookup tables built once from a registry."""

    def __init__(self, registry: list[Institution] | None = None):
        self.registry = load_registry() if registry is None else registry
        self.by_id = {item.id: item for item in self.registry}
        self._names: dict[tuple[str, ...], tuple[str, str]] = {}
        # Single-word names ("Meta", "Apple") only match case-sensitively as a leading word.
        self._single: dict[str, tuple[str, str]] = {}
        self._acronyms: list[tuple[re.Pattern, str, str]] = []
        self._domains: dict[str, str] = {}
        self._orgs: dict[str, str] = {}
        self._alias_tokens: dict[str, set[str]] = defaultdict(set)
        for item in self.registry:
            for name in item.names:
                key = _tokens(name)
                if not key:
                    continue
                self._names.setdefault(key, (item.id, name))
                self._alias_tokens[item.id].update(key)
                if len(key) == 1:
                    self._single.setdefault(name, (item.id, name))
            for acronym in item.acronyms:
                pattern = re.compile(rf"(?<![\w-]){re.escape(acronym)}(?![\w-])")
                self._acronyms.append((pattern, item.id, acronym))
            for domain in item.domains:
                self._domains[domain] = item.id
            for org in item.github_orgs:
                self._orgs[org.casefold()] = item.id
        self._longest = max((len(key) for key in self._names), default=0)

    def root(self, institution_id: str) -> str:
        return ([institution_id, *self.ancestors(institution_id)])[-1]

    def ancestors(self, institution_id: str) -> list[str]:
        """Parents from nearest to the root."""
        found: list[str] = []
        item = self.by_id[institution_id]
        while item.parent and item.parent in self.by_id and item.parent not in found:
            found.append(item.parent)
            item = self.by_id[item.parent]
        return found

    def match_text(self, text: str) -> list[tuple[str, MatchRule, str]]:
        """Match one declared affiliation string; it may name several institutions."""
        parts = [part for part in _SEGMENT.split(text) if _tokens(part)]
        while len(parts) > 1 and _tokens(parts[-1]) in _PLACES:
            parts.pop()
        whole = _tokens(" ".join(parts))
        if whole in self._names:
            institution_id, alias = self._names[whole]
            return [(institution_id, "exact", alias)]
        found: dict[str, tuple[str, MatchRule, str]] = {}
        for index, part in enumerate(parts):
            following = parts[index + 1] if index + 1 < len(parts) else None
            # Brackets split only when the whole part is not a name: "HKUST (GZ)" stays
            # together, while "... Technology (HKUST)" is read piece by piece.
            pieces = [part]
            if _tokens(part) not in self._names:
                pieces = [piece for piece in _BRACKETS.split(part) if _tokens(piece)]
            for position, piece in enumerate(pieces):
                after = pieces[position + 1] if position + 1 < len(pieces) else following
                for match in self._match_segment(piece, after):
                    found.setdefault(match[0], match)
        # Keep the most specific unit: "DAMO Academy, Alibaba Group" names DAMO, whose
        # root_id already reports Alibaba.
        covered = {parent for institution_id in found for parent in self.ancestors(institution_id)}
        return [match for institution_id, match in found.items() if institution_id not in covered]

    def match_host(self, url: str) -> list[tuple[str, MatchRule, str]]:
        """Match an institution-owned host, or a GitHub Pages host by its owner login."""
        host = _host(url)
        if host.endswith(".github.io"):
            return self.match_github_owner(host.split(".")[0])
        best = max(
            (domain for domain in self._domains if host == domain or host.endswith("." + domain)),
            key=len,
            default=None,
        )
        return [(self._domains[best], "domain", best)] if best else []

    def match_github_owner(self, owner: str) -> list[tuple[str, MatchRule, str]]:
        institution_id = self._orgs.get(owner.casefold())
        return [(institution_id, "github_org", owner)] if institution_id else []

    def _campus_conflict(
        self,
        institution_id: str,
        key: tuple[str, ...],
        rest: tuple[str, ...],
        following: str | None,
    ) -> bool:
        """Whether "University of <State>" is qualified as a different campus.

        "University of Maryland, Baltimore County" is not the College Park entry, while
        "University of Washington, Seattle" and "... Medical School" stay with theirs.
        """
        if key[:2] != ("university", "of") or key[2:] not in _STATE_TOKENS:
            return False
        qualifier = rest or (_tokens(following) if following else ())
        return bool(
            qualifier
            and len(qualifier) <= 4
            and qualifier not in _PLACES
            and qualifier not in self._names
            and not set(qualifier) & _SUBUNIT
            and not set(qualifier) <= self._alias_tokens[institution_id]
        )

    def _match_segment(self, segment: str, following: str | None):
        tokens = _tokens(segment)
        if tokens in self._names:
            institution_id, alias = self._names[tokens]
            if self._campus_conflict(institution_id, tokens, (), following):
                return []
            return [(institution_id, "segment", alias)]
        matches = []
        used: set[int] = set()
        # Longest names first, so "Technical University of Munich" beats a shorter name.
        for size in range(min(len(tokens), self._longest), 1, -1):
            for start in range(len(tokens) - size + 1):
                span = set(range(start, start + size))
                key = tokens[start : start + size]
                if span & used or key not in self._names:
                    continue
                institution_id, alias = self._names[key]
                if self._campus_conflict(institution_id, key, tokens[start + size :], following):
                    continue
                matches.append((start, (institution_id, "contains", alias)))
                used |= span
        # Single-word names count only where a name starts: "Meta Reality Labs",
        # "Zhejiang University and Tencent".
        words = [re.sub(r"^\W+|\W+$", "", word) for word in segment.split()]
        covered = {token for _, (_, _, alias) in matches for token in _tokens(alias)}
        for position, word in enumerate(words):
            if (
                word in self._single
                and (position == 0 or words[position - 1].lower() in {"and", ""})
                and not set(_tokens(word)) & covered
            ):
                institution_id, alias = self._single[word]
                matches.append((len(tokens) + position, (institution_id, "leading_name", alias)))
        if matches:
            return [match for _, match in sorted(matches, key=lambda item: item[0])]
        return [
            (institution_id, "acronym", acronym)
            for pattern, institution_id, acronym in self._acronyms
            if pattern.search(segment)
        ][:3]


@cache
def default_index() -> InstitutionIndex:
    return InstitutionIndex()


def match_institutions(
    result: PaperResult, index: InstitutionIndex | None = None
) -> list[InstitutionMatch]:
    """Match a resolved paper's declared affiliations, page hosts, and repository owners.

    Page declarations carry ``author_overlap`` so callers can tell whether a page's author
    block lists this paper's authors; repository and page matches keep their URLs.
    """
    index = index or default_index()
    paper_authors = {
        frozenset(_tokens(author.name)) for author in (result.paper.authors if result.paper else [])
    }
    matches: dict[tuple, InstitutionMatch] = {}

    def add(found, text, source, source_url=None, author_overlap=None):
        for institution_id, rule, alias in found:
            match = InstitutionMatch(
                institution_id=institution_id,
                root_id=index.root(institution_id),
                rule=rule,
                alias=alias,
                text=text,
                source=source,
                source_url=source_url,
                author_overlap=author_overlap,
            )
            matches.setdefault((institution_id, rule, source, source_url, text), match)

    if result.paper:
        for author in result.paper.authors:
            for affiliation in author.affiliations:
                add(index.match_text(affiliation), affiliation, "arxiv")
    for page in result.pages:
        url = page.final_url
        for name in page.institutions:
            add(index.match_text(name), name, "citation_meta", url)
        block = page.author_block
        if block:
            overlap = (
                sum(frozenset(_tokens(author.name)) in paper_authors for author in block.authors)
                if block.authors and paper_authors
                else None
            )
            for item in block.affiliations:
                add(index.match_text(item.name), item.name, item.source, url, overlap)
        add(index.match_host(url), _host(url), "page_host", url)
    for repository in result.repositories:
        owner = repository.full_name.split("/")[0]
        add(index.match_github_owner(owner), owner, "repository_owner", repository.url)
    return list(matches.values())
