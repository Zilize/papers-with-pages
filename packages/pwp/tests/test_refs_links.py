import pytest
from pwp import Paper, classify_link, extract_metadata_links, normalize_url, parse_paper_ref


@pytest.mark.parametrize(
    "value,identifier",
    [
        ("2410.12345", "2410.12345"),
        (" arXiv:2410.12345v2 ", "2410.12345v2"),
        ("https://arxiv.org/abs/hep-th/9901001v1", "hep-th/9901001v1"),
        ("https://arxiv.org/pdf/2410.12345v3.pdf?download=1", "2410.12345v3"),
        ("https://arxiv.org/html/2410.12345v2", "2410.12345v2"),
    ],
)
def test_reference_formats(value, identifier):
    assert parse_paper_ref(value).identifier == identifier


@pytest.mark.parametrize(
    "value",
    [
        "2413.12345",
        "2410.12345v0",
        "not-an-id",
        "2410.12345/extra",
        "https://arxiv.org.evil.example/abs/2410.12345",
        "https://user@arxiv.org/abs/2410.12345",
        "https://arxiv.org/search/2410.12345",
        "ftp://arxiv.org/abs/2410.12345",
    ],
)
def test_rejects_invalid_references(value):
    with pytest.raises(ValueError):
        parse_paper_ref(value)


def test_normalization_preserves_meaning():
    assert normalize_url("HTTPS://EXAMPLE.ORG:443/a/?token=abc&x=1#part") == (
        "https://example.org/a/?token=abc&x=1#part"
    )
    assert normalize_url("http://example.org:8080/a") == "http://example.org:8080/a"
    assert normalize_url("www.example.org") == "https://www.example.org/"
    assert normalize_url("https://example.org/?x=1&amp;y=2") == "https://example.org/?x=1&y=2"
    assert normalize_url("https://example.org/?a=1&not=2") == "https://example.org/?a=1&not=2"


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "/relative",
        "https://user:pass@example.org/",
        "https://example.org:bad/",
        "https://example.org/a b",
        "https://example.org\\x",
    ],
)
def test_rejects_unsafe_url_shapes(value):
    with pytest.raises(ValueError):
        normalize_url(value)


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://github.com/example/project", "repository"),
        ("https://github.com/example", "other"),
        ("https://github.com/topics/vision", "other"),
        ("https://example.github.io/project/", "project_page"),
        ("https://example.github.io/", "other"),
        ("https://huggingface.co/datasets/example/data", "dataset"),
        ("https://huggingface.co/spaces/example/demo", "demo"),
        ("https://huggingface.co/example/model", "model"),
        ("https://huggingface.co/docs/transformers", "other"),
        ("https://youtu.be/example", "video"),
        ("https://youtube.com/@example", "other"),
        ("https://vimeo.com/123456", "video"),
        ("https://arxiv.org/abs/2410.12345", "paper"),
        ("https://github.com.evil.example/example/project", "other"),
        ("https://custom-project.example/", "other"),
    ],
)
def test_classification_does_not_confirm_association(url, kind):
    link = classify_link(url)
    assert link.kind == kind
    assert link.relation == "candidate"


def test_extraction_keeps_evidence_and_balanced_parentheses():
    paper = Paper(
        arxiv_id="2410.12345",
        title="Example",
        abstract="Code (https://github.com/example/project). Figure: https://example.org/a_(b).",
        comments="Code: https://github.com/example/project; [Page](https://example.org/page)",
    )
    links = extract_metadata_links(paper)
    assert [link.url for link in links] == [
        "https://github.com/example/project",
        "https://example.org/a_(b)",
        "https://example.org/page",
    ]
    assert [item.source for item in links[0].evidence] == ["abstract", "comments"]
    assert "Code" in links[0].evidence[0].context
    assert links[0].evidence[0].original_url == "https://github.com/example/project"


def test_no_links_is_normal_and_models_round_trip():
    paper = Paper(arxiv_id="2410.12345", title="No links", abstract="A normal paper.")
    assert extract_metadata_links(paper) == []
    assert Paper.model_validate_json(paper.model_dump_json()) == paper


def test_latex_href_does_not_merge_target_and_label():
    paper = Paper(
        arxiv_id="2410.12345",
        title="Example",
        abstract=(
            r"Our \href{https://github.com/example/project}{code} and "
            r"\url{https://example.org/project} are available."
        ),
    )
    assert [link.url for link in extract_metadata_links(paper)] == [
        "https://github.com/example/project",
        "https://example.org/project",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://openreview.net/forum?id=paper",
        "https://proceedings.mlr.press/v1/paper.html",
        "https://aclanthology.org/2026.test/",
        "https://example.org/docs/start",
        "https://example.org/people/alex",
        "https://example.org/assets/cover.webp",
        "https://researcher.github.io/",
    ],
)
def test_non_project_metadata_destinations_are_not_promoted(url):
    from pwp.page import seed_link

    paper = Paper(arxiv_id="2410.12345", title="Example", abstract=f"Available at {url}")
    assert seed_link(extract_metadata_links(paper)[0]).kind != "project_page"


def test_explicit_project_root_and_custom_domains_remain_candidates():
    from pwp.page import seed_link

    for url in ["https://nerfies.github.io/", "https://custom-project.example/"]:
        paper = Paper(arxiv_id="2410.12345", title="Example", comments=f"Project page: {url}")
        assert seed_link(extract_metadata_links(paper)[0]).kind == "project_page"
