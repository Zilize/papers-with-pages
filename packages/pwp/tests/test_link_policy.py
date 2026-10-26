import pytest
from pwp import Paper, extract_metadata_links, parse_project_page
from pwp.link_policy import admissible_child, select_resources


def test_anonymous_code_host_is_not_a_project_page_in_new_or_saved_results():
    from pwp import classify_link
    from pwp.page import can_explore, seed_link

    url = "https://anonymous.4open.science/r/project-ABCD/"
    paper = Paper(arxiv_id="2410.12345", title="Example", comments=f"Project page: {url}")
    link = extract_metadata_links(paper)[0]
    assert link.kind == "repository"
    assert link.platform == "anonymous_4open"
    assert seed_link(link).kind == "repository"
    assert not can_explore(link)
    legacy = link.model_copy(update={"kind": "project_page", "platform": None})
    repaired = select_resources(paper, [legacy])[0]
    assert repaired.kind == "repository"
    assert repaired.platform == "anonymous_4open"
    assert classify_link("https://anonymous.4open.science/").kind == "other"
    assert classify_link("https://anonymous.4open.science.evil.example/r/demo").platform is None


def test_markdown_adjacent_links_do_not_merge():
    paper = Paper(
        arxiv_id="2410.12345",
        title="Example",
        abstract=("[[paper]](https://example.org/paper.pdf)[[code]](https://github.com/team/repo)"),
    )
    assert [link.url for link in extract_metadata_links(paper)] == [
        "https://example.org/paper.pdf",
        "https://github.com/team/repo",
    ]


def test_labeled_adjacent_metadata_links_do_not_merge():
    paper = Paper(
        arxiv_id="2410.12345",
        title="Example",
        abstract="Code:https://github.com/RoboOpus/ACG-WAM.Website:https://RoboOpus.github.io/ACG-WAM.",
    )
    assert [link.url for link in extract_metadata_links(paper)] == [
        "https://github.com/RoboOpus/ACG-WAM",
        "https://roboopus.github.io/ACG-WAM",
    ]


@pytest.mark.parametrize("host", ["pypi.org", "www.pypi.org", "pypi.python.org"])
def test_package_registry_is_excluded_in_metadata_and_saved_results(host):
    from pwp.extract.links import project_page_exclusion
    from pwp.page import can_explore, seed_link

    paper = Paper(
        arxiv_id="2410.12345",
        title="Example",
        comments=f"Project page: https://{host}/project/example/",
    )
    link = extract_metadata_links(paper)[0]
    assert seed_link(link).kind == "other"
    assert not can_explore(link)
    legacy = link.model_copy(update={"kind": "project_page"})
    assert select_resources(paper, [legacy])[0].kind == "other"
    assert project_page_exclusion("https://pypi.org.example/project/demo/") is None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.google-analytics.com/g/collect", True),
        ("https://www.youtube.com/youtubei/v1/log_event", True),
        ("https://www.youtube.com/embed/example", False),
        ("https://www.youtube.com/youtubei/v1/player", False),
        ("https://google-analytics.com.example/script.js", False),
    ],
)
def test_telemetry_filter_keeps_player_requests(url, expected):
    from pwp.extract.links import is_telemetry_url

    assert is_telemetry_url(url) is expected


def test_buttons_are_kept_but_references_and_footer_are_not():
    page = parse_project_page(
        """<h1>Our project</h1>
        <a href="https://github.com/team/ours"><img alt="Code"></a>
        <a href="https://gitlab.com/team/ours">GitLab</a>
        <a href="https://huggingface.co/datasets/team/data">Dataset</a>
        <h2>Related work</h2><a href="https://github.com/team/unrelated">Code</a>
        <footer><a href="https://github.com/team/template">GitHub</a></footer>
    """,
        "https://project.example/",
    )
    admitted = [link for link in page.links if admissible_child(link)]
    assert {link.kind for link in admitted} == {"repository", "dataset"}
    assert len(admitted) == 3
    assert not any("unrelated" in link.url or "template" in link.url for link in page.links)


def test_reference_pages_and_repo_dependencies_are_not_followed():
    page = parse_project_page(
        """<a href="https://github.com/team/ours">Code</a>
        <a href="https://other.github.io/project/">Project page</a>
        <a href="https://github.com/team/baseline">Baseline</a>""",
        "https://ours.example/",
    )
    assert [link.url for link in page.links if admissible_child(link)] == [
        "https://github.com/team/ours"
    ]


def test_saved_resources_must_be_reachable_from_metadata():
    paper = Paper(arxiv_id="2410.12345", title="Example", comments="https://ours.example/")
    root = extract_metadata_links(paper)[0].model_copy(update={"kind": "project_page"})
    page = parse_project_page('<a href="https://github.com/team/ours">Code</a>', root.url)
    unrelated = parse_project_page(
        '<a href="https://github.com/team/unrelated">Code</a>', "https://unrelated.example/"
    )
    kept = select_resources(paper, [root, *page.links, *unrelated.links])
    assert len(kept) == 2
