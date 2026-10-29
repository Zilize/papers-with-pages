"""Author and affiliation declarations from the hero area of a project page.

Elements whose class declares authors or affiliations are read first; without them,
the region after the page title is used. Footers, related work, and template credits do
not contribute. Schema.org JSON-LD person declarations are included with their source.
Results are declarations as written; matching them to known institutions or to a paper's
author list belongs to callers.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from pwp.models import AuthorBlock, PageAffiliation, PageAuthor
from pwp.patterns import NOISE

_BLOCK = {
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p", "section",
    "table", "td", "th", "tr", "ul",
}  # fmt: skip
_SKIP = ["script", "style", "noscript", "template", "button", "svg", "nav", "code", "pre"]
# Teasers and media end the hero area.
_STOP = {"footer", "video", "iframe", "figure", "canvas", "form", "main"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_CLASS = re.compile(r"author|affil|institut|organi[sz]ation", re.I)
_AUTHOR_CLASS = re.compile(r"author", re.I)
_HERO_CLASS = re.compile(r"\bhero\b|\bheader\b|\btitle\b", re.I)
# Subtitles, venues, and taglines sit between the title and the authors.
_TITLE_CLASS = re.compile(r"^(?:sub)?title$|publication-title|venue|conference|tagline", re.I)
_MAX_REGION_CHARS = 4000
_MAX_LINE_CHARS = 400
_MAX_NAME_CHARS = 150
_MAX_UNMARKED_WORDS = 12
_MAX_CONTAINERS = 4

_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_MARK = re.compile(r"([⁰¹²³⁴⁵⁶⁷⁸⁹]+|[*†‡§¶✉]+|#+(?!\w))")
# Inline elements holding only markers, such as <span class="star">1,2†</span>.
_MARK_ONLY = re.compile(r"^\s*(?:[1-9]\d?|[*†‡§¶#✉]+)(?:\s*,?\s*(?:[1-9]\d?|[*†‡§¶#✉]+))*\s*$")
# Superscripts may also use single letters: <sup>a,b</sup>.
_MARKS = re.compile(r"[1-9]\d?|(?<![A-Za-z])[a-z](?![A-Za-z])|[*†‡§¶#✉]+")
_MARK_PARENTS = {"sup", "span", "small", "i", "em", "b", "strong"}
_SYMBOL = re.compile(r"^[*†‡§¶#✉]+$")
_LEADING_NUMBER = re.compile(r"(?:^|(?<=[\s,;]))(\d{1,2})\s*(?=[A-Z\u4e00-\u9fff])")
_SEPARATOR = re.compile(r"\s*(?:[,;|·•]|\band\b|&)\s*")
_AFFILIATION_SEPARATOR = re.compile(r"\s*[;|·•]\s*")
_INSTITUTION = re.compile(
    r"universit|institut|college|school|academy|acad[eé]mie|laborator|\blabs?\b|research|"
    r"\bcent(?:er|re)\b|polytechn|hospital|foundation|\binc\b|\bcorp(?:oration)?\b|\bltd\b|"
    r"\bllc\b|gmbh|technolog|\bai\b|deepmind|nvidia|google|\bmeta\b|microsoft|\bapple\b|"
    r"amazon|openai|anthropic|bytedance|alibaba|tencent|baidu|huawei|\beth\b|epfl|\bmit\b|"
    r"\bcmu\b|\bkaist\b|\binria\b|\bmpi\b|max planck|"
    r"\u5927\u5b66|\u5b66\u9662|\u7814\u7a76\u9662|\u7814\u7a76\u6240|\u5b9e\u9a8c\u5ba4|"
    r"\u516c\u53f8|\u4e2d\u5fc3",
    re.I,
)
_VENUE = re.compile(
    r"transactions|conference|proceedings|journal|workshop|symposium|accepted|preprint|"
    r"\barxiv\b|\b(?:19|20)\d{2}\b",
    re.I,
)
_NOTE = re.compile(
    r"equal|contribut|correspond|\blead\b|co-?lead|first author|work (?:was )?done|"
    r"intern|contact|indicates|denotes|advis|supervis",
    re.I,
)
_NOTE_AT = re.compile(
    r"(?:work (?:was )?done|intern(?:ship)?|interned)\b.*?\b(?:at|with|@)\s+(?P<name>[^.;,()]+)",
    re.I,
)
_NOT_NAME = re.compile(
    r"\b(paper|code|arxiv|video|abstract|demo|dataset|data|slides|poster|supplementary|github|"
    r"bibtex|hugging ?face|models?|project|page|conference|workshop|anonymous|authors?|"
    r"benchmarks?|toolkit|framework|track|soon|learning|polic(?:y|ies)|methods?|networks?|"
    r"systems?|world|online|real|getting|started|works|generation|diffusion|agentic|group|"
    r"team|robot\w*|"
    r"reinforcement|space|constraints?|routing|patches|terrains|across|via|towards?|for|with)\b",
    re.I,
)
_PERSON_WORD = re.compile(r"^[A-ZÀ-ÖØ-Þ][\w'’.\-À-ÿ]*$|^[a-z]{1,3}$")
_CJK_NAME = re.compile(r"^[\u4e00-\u9fff·]{2,6}$")
# Button labels such as paper, code, data, and "coming soon" are not CJK names.
_CJK_NOT_NAME = re.compile(
    r"\u8bba\u6587|\u4ee3\u7801|\u6570\u636e|\u9879\u76ee|\u4e3b\u9875|\u9996\u9875|"
    r"\u6f14\u793a|\u89c6\u9891|\u6458\u8981|\u5f15\u7528|\u5373\u5c06|\u5f00\u653e|"
    r"\u4e0b\u8f7d|\u6a21\u578b"
)
# Lowercase words allowed inside institution names; any other marks a sentence.
_NAME_PARTICLES = {
    "of", "and", "the", "for", "at", "in", "on", "de", "di", "da", "do", "du", "des", "del",
    "della", "la", "le", "les", "y", "e", "et", "für", "und", "der", "van", "von", "zu",
}  # fmt: skip
_LOGO = re.compile(r"logo", re.I)
_LOGO_WORDS = re.compile(r"\b(?:logo\w*|wordmark|symbol|icon|emblem|seal|image)\b", re.I)
_TRIM = " \t\r\n,;:|·•-–—"


@dataclass
class _Line:
    tokens: list[tuple[str, str]] = field(default_factory=list)
    author_class: bool = False
    # Lines after the title have no declared container to vouch for unmarked names.
    after_title: bool = False

    @property
    def text(self) -> str:
        return "".join(value for kind, value in self.tokens if kind != "break")


def _hidden(node: Tag) -> bool:
    return (
        node.has_attr("hidden")
        or str(node.get("aria-hidden", "")).lower() == "true"
        or bool(
            re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", node.get("style", ""), re.I)
        )
    )


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(_TRIM)
    text = re.sub(r"^(?:and|&)\s+|\s+(?:and|&)$", "", text, flags=re.I).strip(_TRIM)
    # Drop brackets only when they are unbalanced leftovers of a split.
    for opening, closing in ("()", "[]"):
        if text.endswith(closing) and opening not in text:
            text = text[:-1]
        if text.startswith(opening) and closing not in text:
            text = text[1:]
    return text.strip(_TRIM)


def _person(name: str) -> bool:
    if _CJK_NAME.match(name):
        return not _CJK_NOT_NAME.search(name)
    words = name.split()
    return (
        2 <= len(words) <= 5
        and all(_PERSON_WORD.match(word) for word in words)
        and words[0][0].isupper()
        and words[-1][0].isupper()
        and not any(char.isdigit() for char in name)
        and not _NOT_NAME.search(name)
        and not _INSTITUTION.search(name)
        and not _NOTE.search(name)
    )


def _classes(node: Tag) -> str:
    return " ".join([*node.get("class", []), str(node.get("id", ""))])


def _logo_name(node: Tag, declared: bool) -> str | None:
    """Name an institution logo from its alt text or a "logo" file name.

    Images with alt text inside affiliation containers count without "logo" in them.
    """
    alt = node.get("alt", "") or ""
    src = node.get("src", "") or ""
    if not (declared or _LOGO.search(alt) or _LOGO.search(src)):
        return None
    name = _clean(re.sub(r"\s+and\s*$", "", _LOGO_WORDS.sub(" ", alt).strip(), flags=re.I))
    if not name and _LOGO.search(src):
        stem = PurePosixPath(urlsplit(src).path).stem
        name = _clean(re.sub(r"[-_.]+|\d+", " ", _LOGO.sub(" ", stem)))
    return name if 1 < len(name) <= _MAX_NAME_CHARS else None


def _excluded(node: Tag) -> bool:
    return node.find_parent(["footer", *_SKIP]) is not None or any(
        _hidden(parent) for parent in (node, *node.parents)
    )


def _containers(soup: BeautifulSoup) -> list[Tag]:
    """Outermost elements whose class or id declares authors or affiliations."""
    found: list[Tag] = []
    inside: set[int] = set()
    for node in soup.find_all(True):
        if id(node) in inside or node.name in {"img", "sup", "a", "body", "html", "main"}:
            continue
        if not _BLOCK_CLASS.search(_classes(node)) or _excluded(node):
            continue
        if len(node.get_text(" ", strip=True)) > _MAX_REGION_CHARS:
            continue
        heading = node.find_previous(["h2", "h3"])
        if heading is not None and NOISE.search(heading.get_text(" ", strip=True)):
            continue
        found.append(node)
        inside.update(id(child) for child in node.descendants)
        if len(found) >= _MAX_CONTAINERS:
            break
    return found


def _hero_root(title: Tag) -> Tag | None:
    """The outermost header, section, or hero wrapper around the title."""
    root = None
    for parent in title.parents:
        if parent.name in {"body", "html", "main"}:
            break
        if parent.name in {"header", "section"} or _HERO_CLASS.search(_classes(parent)):
            if len(parent.get_text(" ", strip=True)) > 20_000:
                break
            root = parent
    return root


def _after_title(title: Tag) -> Iterator:
    root = _hero_root(title)
    own = {id(node) for node in title.descendants}
    inside = {id(node) for node in root.descendants} if root is not None else None
    for node in title.next_elements:
        if id(node) in own:
            continue
        if inside is not None and id(node) not in inside:
            return
        if root is None and isinstance(node, Tag) and node.name == "h2":
            return
        yield node


def _declared(containers: list[Tag]) -> Iterator:
    """Container nodes; ``None`` separates groups so inline siblings start new lines."""
    for container in containers:
        yield None
        yield container
        yield from container.descendants
    # Unlabeled affiliation rows often follow the last author container.
    for sibling in containers[-1].find_next_siblings(True, limit=2):
        if sibling.name in _HEADINGS or sibling.name in _STOP:
            break
        if len(sibling.get_text(" ", strip=True)) <= 600:
            yield None
            yield sibling
            yield from sibling.descendants


def _in_title(ancestors: list[Tag]) -> bool:
    """Whether the nearest declaring ancestor is a heading, subtitle, or venue."""
    for node in ancestors:
        classes = node.get("class", [])
        if _BLOCK_CLASS.search(" ".join(classes)):
            return False
        if node.name in _HEADINGS and node.name != "h1" and node.find("sup"):
            # "<h4>Google<sup>1</sup></h4>": marked headings list authors or affiliations.
            return False
        if node.name in _HEADINGS or any(_TITLE_CLASS.search(name) for name in classes):
            return True
    return False


def _lines(soup: BeautifulSoup) -> tuple[list[_Line], list[str]]:
    """Tokenize the author region into lines of text and marker tokens, plus logo names."""
    containers = _containers(soup)
    if containers:
        nodes = _declared(containers)
    # Some templates title the page with an <h2> and no <h1>.
    elif (title := soup.find("h1") or soup.find("h2")) is not None:
        nodes = _after_title(title)
    else:
        return [], []
    after_title = not containers
    lines = [_Line(after_title=after_title)]
    logos: list[str] = []
    chars = 0
    for node in nodes:
        if node is None:
            lines.append(_Line(after_title=after_title))
            continue
        if isinstance(node, Tag):
            if node.name in _STOP:
                break
            if node.name == "img" and not _excluded(node):
                declared = any(
                    _BLOCK_CLASS.search(_classes(parent))
                    and not _AUTHOR_CLASS.search(_classes(parent))
                    for parent in node.parents
                )
                name = _logo_name(node, declared)
                if name:
                    logos.append(name)
            if node.name in _BLOCK or (
                node.name not in {"img", "sup", "a"} and _BLOCK_CLASS.search(_classes(node))
            ):
                lines.append(_Line(after_title=after_title))
            elif node.name in {"a", "span", "li"} and lines[-1].tokens:
                # Adjacent links or spans are separate names even without a comma.
                lines[-1].tokens.append(("break", ""))
            continue
        if not isinstance(node, NavigableString) or isinstance(node, Comment):
            continue
        text = str(node)
        if not text.strip() and not lines[-1].tokens:
            continue
        ancestors = list(node.parents)
        if (
            node.find_parent(_SKIP)
            or any(_hidden(parent) for parent in ancestors)
            or _in_title(ancestors)
        ):
            continue
        chars += len(text)
        if chars > _MAX_REGION_CHARS:
            break
        line = lines[-1]
        line.author_class |= any(_AUTHOR_CLASS.search(_classes(parent)) for parent in ancestors)
        if node.find_parent("sup") or (
            node.parent.name in _MARK_PARENTS and _MARK_ONLY.match(text)
        ):
            line.tokens.extend(
                ("mark", mark) for mark in _MARKS.findall(text.translate(_SUPERSCRIPT))
            )
            continue
        for index, part in enumerate(_MARK.split(text)):
            if part:
                line.tokens.append(
                    ("mark", part.translate(_SUPERSCRIPT)) if index % 2 else ("text", part)
                )
    return [line for line in lines if line.tokens], logos


def _json_ld(soup: BeautifulSoup) -> tuple[list[PageAuthor], list[PageAffiliation]]:
    """Person and affiliation declarations from schema.org JSON-LD."""
    authors: list[PageAuthor] = []
    affiliations: list[PageAffiliation] = []

    def visit(item, depth: int) -> None:
        if depth > 6:
            return
        if isinstance(item, list):
            for value in item[:200]:
                visit(value, depth + 1)
            return
        if not isinstance(item, dict):
            return
        kind = item.get("@type")
        if kind == "Person" or (isinstance(kind, list) and "Person" in kind):
            if isinstance(item.get("name"), str) and _clean(item["name"]):
                authors.append(PageAuthor(name=_clean(item["name"])))
            organizations = item.get("affiliation")
            for organization in (
                organizations if isinstance(organizations, list) else [organizations]
            ):
                name = organization.get("name") if isinstance(organization, dict) else organization
                if not isinstance(name, str):
                    continue
                affiliations.extend(
                    PageAffiliation(name=part, source="json_ld")
                    for part in map(_clean, _AFFILIATION_SEPARATOR.split(name))
                    if part and len(part) <= _MAX_NAME_CHARS
                )
        # Only follow authorship keys; "citation" and similar list other works.
        for key in ("@graph", "mainEntity", "author", "creator", "contributor"):
            if key in item:
                visit(item[key], depth + 1)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            visit(json.loads(script.get_text()), 0)
        except ValueError:
            continue
    return authors, affiliations


def _numbered(tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Read plain numbers in affiliation lines ("1 University ..., 2Lab") as markers."""
    first = tokens[0]
    if not (
        first[0] == "mark" or (first[0] == "text" and _LEADING_NUMBER.match(first[1].lstrip()))
    ) or not _INSTITUTION.search("".join(value for _, value in tokens)):
        return tokens
    result = []
    for kind, value in tokens:
        if kind != "text":
            result.append((kind, value))
            continue
        for index, part in enumerate(_LEADING_NUMBER.split(value)):
            if part.strip():
                result.append(("mark", part) if index % 2 else ("text", part))
    return result


def _affiliation(name: str, marker: str | None) -> PageAffiliation | None:
    if (
        not name
        or len(name) > _MAX_NAME_CHARS
        or "?" in name
        or not re.search(r"[A-Z\u4e00-\u9fff]", name)
        or _VENUE.search(name)
        or re.search(r"@|https?://|www\.", name)
    ):
        return None
    if _NOTE.search(name):
        match = _NOTE_AT.search(name)
        named = _affiliation(_clean(match["name"]), marker) if match else None
        return named.model_copy(update={"note": name}) if named else None
    # Sentences such as section teasers are not names; "Corp." and "Inc." are.
    if name.endswith(".") and not _INSTITUTION.search(name):
        return None
    if any(
        word[0].islower() and word.lower() not in _NAME_PARTICLES
        for word in re.findall(r"[^\W\d_][\w'’-]*", name)
    ):
        return None
    if marker is not None and _SYMBOL.match(marker) and not _INSTITUTION.search(name):
        return None
    return PageAffiliation(name=name, marker=marker)


def _parse_line(line: _Line, authors: list, affiliations: list) -> bool:
    """Add declarations found in one line; return whether the line contributed."""
    tokens = _numbered([token for token in line.tokens if token[0] != "break"])
    found = False
    if tokens[0][0] == "mark":
        # Leading markers introduce affiliations or footnotes: "¹MIT ²Google".
        segments: list[list[str]] = []
        for kind, value in tokens:
            if kind == "mark":
                if segments and not segments[-1][1].strip(_TRIM):
                    continue
                segments.append([value, ""])
            else:
                segments[-1][1] += value
        for marker, text in segments:
            item = _affiliation(_clean(text), marker)
            if item:
                affiliations.append(item)
                found = True
        return found
    if not any(kind == "mark" for kind, _ in tokens):
        text = _clean(line.text)
        if _INSTITUTION.search(text) and not _NOTE.search(text):
            # Commas are kept: "University of California, Berkeley" is one name.
            for part in _AFFILIATION_SEPARATOR.split(text):
                part = _clean(part)
                # A declared affiliation container vouches for long multi-unit lines.
                if line.after_title and len(part.split()) > _MAX_UNMARKED_WORDS:
                    continue
                item = _affiliation(part, None)
                if item:
                    affiliations.append(item)
                    found = True
            return found
    # One affiliation with trailing markers: "Max Planck Institute for Informatics, SIC²".
    filled = [(kind, value) for kind, value in tokens if kind == "mark" or value.strip(_TRIM)]
    first_mark = next((index for index, (kind, _) in enumerate(filled) if kind == "mark"), None)
    if first_mark is not None and all(kind == "mark" for kind, _ in filled[first_mark:]):
        text = _clean("".join(value for _, value in filled[:first_mark]))
        if _INSTITUTION.search(text) and not _person(text):
            item = _affiliation(text, filled[first_mark][1])
            if item:
                affiliations.append(item)
                return True
    # Trailing markers follow names: "Alice¹, Bob²" or "Alice*".
    segments_with_marks: list[tuple[list[str], list[str]]] = []
    attach = False
    for kind, value in line.tokens:
        if kind == "break":
            attach = False
        elif kind == "mark":
            if segments_with_marks:
                segments_with_marks[-1][1].append(value)
            attach = False
        else:
            parts = _SEPARATOR.split(value)
            for index, part in enumerate(parts):
                part = part.strip()
                if index == 0 and attach and not segments_with_marks[-1][1]:
                    if part:
                        segments_with_marks[-1][0].append(part)
                elif part:
                    segments_with_marks.append(([part], []))
            attach = bool(parts[-1].strip())
    named = [(_clean(" ".join(words)), marks) for words, marks in segments_with_marks]
    people = [(name, marks) for name, marks in named if _person(name)]
    marked = any(marks for _, marks in named)
    # Unmarked title-case phrases are only names inside an author list.
    if marked or line.author_class or (line.after_title and len(people) >= 2):
        for name, marks in people:
            authors.append(PageAuthor(name=name, markers=marks))
            found = True
    for name, marks in named:
        if marks and not _person(name) and _INSTITUTION.search(name):
            item = _affiliation(name, marks[0])
            if item:
                affiliations.append(item)
                found = True
    return found


def parse_author_block(soup: BeautifulSoup) -> AuthorBlock | None:
    """Return author and affiliation declarations written near the page title."""
    lines, logos = _lines(soup)
    authors: list[PageAuthor] = []
    affiliations: list[PageAffiliation] = []
    context = []
    for line in lines:
        text = line.text
        if len(text) > _MAX_LINE_CHARS:
            continue
        if _parse_line(line, authors, affiliations):
            context.append(_clean(text))
    affiliations.extend(PageAffiliation(name=name, source="logo") for name in logos)
    linked_authors, linked_affiliations = _json_ld(soup)
    authors.extend(linked_authors)
    affiliations.extend(linked_affiliations)
    if not authors and not affiliations:
        return None
    unique_authors: dict[str, PageAuthor] = {}
    for author in authors:
        unique_authors.setdefault(author.name, author)
    unique_affiliations: dict[tuple, PageAffiliation] = {}
    for item in affiliations:
        unique_affiliations.setdefault((item.name.casefold(), item.marker, item.source), item)
    return AuthorBlock(
        authors=list(unique_authors.values()),
        affiliations=list(unique_affiliations.values()),
        context=" | ".join(context)[:500],
    )
