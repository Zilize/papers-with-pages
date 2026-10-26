from datetime import date, datetime

import pytest
from pwp import MetadataError, extract_metadata_links
from pwp.harvest import parse_atom, parse_oai


def test_atom_preserves_fields_and_no_link_papers(atom_xml):
    papers = parse_atom(atom_xml)
    assert len(papers) == 2
    assert papers[0].title == "A synthetic research project"
    assert papers[0].version == 2
    assert papers[0].primary_category == "cs.CV"
    assert papers[0].authors[0].affiliations == ["Example Institute"]
    assert isinstance(papers[0].published_at, datetime)
    assert papers[0].published_at.utcoffset().total_seconds() == 0
    assert extract_metadata_links(papers[1]) == []


def test_oai_preserves_deleted_and_invalid_records(oai_xml):
    batch = parse_oai(oai_xml)
    assert [record.status for record in batch.records] == ["active", "deleted", "invalid"]
    paper = batch.records[0].paper
    assert paper.version is None
    assert paper.primary_category is None
    assert type(paper.published_at) is date
    assert paper.published_at == date(2024, 10, 16)
    assert paper.oai_datestamp == date(2026, 10, 9)
    assert paper.authors[0].name == "Alex Example"
    assert extract_metadata_links(paper) == []
    assert batch.records[1].paper is None
    assert batch.records[2].issue.code == "invalid_record"
    assert batch.next_token == "next+token/="
    assert batch.token_expires_at.day == 11


def test_oai_identity_mismatch_is_not_silently_associated(oai_xml):
    content = oai_xml.replace(b"<id>2410.12345</id>", b"<id>2410.99999</id>")
    record = parse_oai(content).records[0]
    assert record.status == "invalid"
    assert "does not match" in record.issue.message


@pytest.mark.parametrize("parser", [parse_atom, parse_oai])
def test_rejects_html_and_entity_documents(parser):
    for content in [
        b"<html><body>Temporary outage</body></html>",
        b'<!DOCTYPE feed [<!ENTITY x SYSTEM "file:///etc/passwd">]><feed>&x;</feed>',
        b"not XML",
    ]:
        with pytest.raises(MetadataError):
            parser(content)


def test_atom_protocol_error_is_not_a_paper():
    content = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>http://arxiv.org/api/errors#incorrect_id_format_for</id>
      <summary>Incorrect ID format</summary></entry></feed>"""
    with pytest.raises(MetadataError) as error:
        parse_atom(content)
    assert error.value.issue.code == "arxiv_api_error"


def test_oai_empty_result_and_expired_token():
    template = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">{}</OAI-PMH>'
    content = template.format('<error code="noRecordsMatch">None</error>').encode()
    assert parse_oai(content).records == []
    content = template.format('<error code="badResumptionToken">Expired</error>').encode()
    with pytest.raises(MetadataError) as error:
        parse_oai(content)
    assert error.value.issue.code == "badResumptionToken"
