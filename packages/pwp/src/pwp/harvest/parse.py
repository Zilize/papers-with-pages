"""Parse arXiv Atom and OAI XML without external entity resolution."""

from datetime import date, datetime
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from pwp.errors import MetadataError
from pwp.models import Author, HarvestBatch, HarvestRecord, Issue, Paper
from pwp.refs import parse_paper_ref

ATOM = "{http://www.w3.org/2005/Atom}"
API = "{http://arxiv.org/schemas/atom}"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
ARXIV = "{http://arxiv.org/OAI/arXiv/}"


def _root(content: bytes, expected: str) -> Element:
    try:
        root = fromstring(content)
    except (ParseError, DefusedXmlException) as exc:
        raise MetadataError("invalid_xml", "The metadata response is not safe, valid XML") from exc
    if root.tag != expected:
        raise MetadataError("invalid_document", f"Expected XML root {expected!r}")
    return root


def _text(element: Element, path: str, *, required: bool = False) -> str:
    child = element.find(path)
    value = "" if child is None else " ".join("".join(child.itertext()).split())
    if required and not value:
        raise ValueError(f"Missing required metadata field: {path}")
    return value


def _date(value: str) -> date | datetime | None:
    if not value:
        return None
    if "T" in value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.fromisoformat(value)


def _datetime(value: str) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def parse_atom(content: bytes) -> list[Paper]:
    """Parse an Atom feed; malformed entries fail explicitly, never disappear."""
    root = _root(content, ATOM + "feed")
    papers: list[Paper] = []
    for entry in root.findall(ATOM + "entry"):
        try:
            identifier = _text(entry, ATOM + "id", required=True)
            if "/api/errors" in identifier:
                raise MetadataError(
                    "arxiv_api_error", _text(entry, ATOM + "summary") or "arXiv rejected the query"
                )
            ref = parse_paper_ref(identifier)
            primary = entry.find(API + "primary_category")
            authors = [
                Author(
                    name=_text(author, ATOM + "name", required=True),
                    affiliations=[
                        " ".join("".join(node.itertext()).split())
                        for node in author.findall(API + "affiliation")
                    ],
                )
                for author in entry.findall(ATOM + "author")
            ]
            papers.append(
                Paper(
                    **ref.model_dump(),
                    title=_text(entry, ATOM + "title", required=True),
                    abstract=_text(entry, ATOM + "summary"),
                    authors=authors,
                    categories=[
                        node.attrib["term"]
                        for node in entry.findall(ATOM + "category")
                        if "term" in node.attrib
                    ],
                    primary_category=primary.get("term") if primary is not None else None,
                    comments=_text(entry, API + "comment") or None,
                    published_at=_date(_text(entry, ATOM + "published")),
                    updated_at=_date(_text(entry, ATOM + "updated")),
                    doi=_text(entry, API + "doi") or None,
                    journal_ref=_text(entry, API + "journal_ref") or None,
                    source="arxiv_api",
                )
            )
        except ValueError as exc:
            raise MetadataError("invalid_entry", f"Invalid Atom entry: {exc}") from exc
    return papers


def _oai_record(record: Element) -> HarvestRecord:
    header = record.find(OAI + "header")
    identifier = _text(header, OAI + "identifier") if header is not None else ""
    sets = (
        [node.text.strip() for node in header.findall(OAI + "setSpec") if node.text]
        if header is not None
        else []
    )
    stamp = None
    try:
        if header is None or not identifier:
            raise ValueError("Missing OAI record header or identifier")
        stamp = _date(_text(header, OAI + "datestamp", required=True))
        if header.get("status") == "deleted":
            return HarvestRecord(
                identifier=identifier, datestamp=stamp, sets=sets, status="deleted"
            )
        metadata = record.find(f"{OAI}metadata/{ARXIV}arXiv")
        if metadata is None:
            raise ValueError("Missing arXiv metadata payload")
        ref = parse_paper_ref(_text(metadata, ARXIV + "id", required=True))
        if identifier != "oai:arXiv.org:" + ref.arxiv_id:
            raise ValueError("OAI identifier does not match the metadata paper ID")
        authors = []
        for author in metadata.findall(f"{ARXIV}authors/{ARXIV}author"):
            name = " ".join(
                part
                for part in [
                    _text(author, ARXIV + "forenames"),
                    _text(author, ARXIV + "keyname", required=True),
                    _text(author, ARXIV + "suffix"),
                ]
                if part
            )
            affiliations = [
                " ".join("".join(node.itertext()).split())
                for node in author.findall(ARXIV + "affiliation")
            ]
            authors.append(Author(name=name, affiliations=affiliations))
        paper = Paper(
            **ref.model_dump(),
            title=_text(metadata, ARXIV + "title", required=True),
            abstract=_text(metadata, ARXIV + "abstract"),
            authors=authors,
            categories=_text(metadata, ARXIV + "categories").split(),
            comments=_text(metadata, ARXIV + "comments") or None,
            published_at=_date(_text(metadata, ARXIV + "created")),
            updated_at=_date(_text(metadata, ARXIV + "updated")),
            oai_datestamp=stamp,
            doi=_text(metadata, ARXIV + "doi") or None,
            journal_ref=_text(metadata, ARXIV + "journal-ref") or None,
            source="arxiv_oai",
        )
        return HarvestRecord(identifier=identifier, datestamp=stamp, sets=sets, paper=paper)
    except ValueError as exc:
        return HarvestRecord(
            identifier=identifier,
            datestamp=stamp,
            sets=sets,
            status="invalid",
            issue=Issue(code="invalid_record", message=str(exc)),
        )


def parse_oai(content: bytes) -> HarvestBatch:
    """Parse one ListRecords response using the arXiv metadata format."""
    root = _root(content, OAI + "OAI-PMH")
    errors = root.findall(OAI + "error")
    for error in errors:
        code = error.get("code", "oai_error")
        if code != "noRecordsMatch":
            raise MetadataError(code, (error.text or "OAI request failed").strip())
    try:
        response_date = _datetime(_text(root, OAI + "responseDate"))
        if errors:
            return HarvestBatch(response_date=response_date)
        listing = root.find(OAI + "ListRecords")
        if listing is None:
            raise ValueError("Missing ListRecords payload")
        token = listing.find(OAI + "resumptionToken")
        return HarvestBatch(
            records=[_oai_record(record) for record in listing.findall(OAI + "record")],
            response_date=response_date,
            next_token=(token.text or "").strip() or None if token is not None else None,
            token_expires_at=(
                _datetime(token.get("expirationDate", "")) if token is not None else None
            ),
        )
    except ValueError as exc:
        raise MetadataError("invalid_oai_response", str(exc)) from exc
