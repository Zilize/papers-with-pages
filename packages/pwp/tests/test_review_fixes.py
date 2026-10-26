"""Regression tests for crawler accuracy, robustness, and performance fixes."""

import importlib
import time

import pytest
from helpers import FakeFetcher
from pwp import ArxivClient, Paper, PaperResult, classify_link, inspect_repo, resolve_many
from pwp.discover import related_to_paper
from pwp.errors import MetadataError, RequestError
from pwp.events import EventCallbackError
from pwp.extract import extract_metadata_links
from pwp.extract.links import extract_text_links, project_page_exclusion
from pwp.link_policy import terminal_page_issue
from pwp.network import FetchResult, RequestBudget, SafeFetcher, request_scope
from pwp.page import linked_paper_ids, parse_project_page, seed_link
from pwp.visuals import discover_visuals


def _ok(url, body=b"ok", headers=None):
    return FetchResult(url, url, 200, headers or {"content-type": "text/plain"}, body)


async def test_assets_skip_robots_pacing_and_page_budgets(monkeypatch):
    async with SafeFetcher(budget=RequestBudget(1)) as fetcher:

        async def robots(url):
            pytest.fail("Browser subresources must not be gated by robots policies")

        async def read(session, url, *args):
            return _ok(url)

        monkeypatch.setattr(fetcher, "_check_robots", robots)
        monkeypatch.setattr(fetcher, "_read_response", read)
        monkeypatch.setattr(fetcher, "_get_session", lambda **kwargs: _none())
        assets = RequestBudget(3)
        page_scope = RequestBudget(1)
        with request_scope(page_scope):
            for _ in range(3):
                await fetcher.get("https://cdn.example/lib.css", asset_budget=assets)
            with pytest.raises(RequestError, match="budget"):
                await fetcher.get("https://cdn.example/lib.css", asset_budget=assets)
        assert assets.used == 3
        assert fetcher.budget.used == 0 and page_scope.used == 0
        assert "cdn.example" not in fetcher._next


async def _none():
    return None


async def test_robots_connection_failure_backs_off(monkeypatch):
    calls = []
    async with SafeFetcher(max_retries=0) as fetcher:

        async def read(session, url, *args):
            calls.append(url)
            raise RequestError("fetch_failed", "HTTP request failed", retryable=True)

        monkeypatch.setattr(fetcher, "_read_response", read)
        monkeypatch.setattr(fetcher, "_get_session", lambda **kwargs: _none())
        for _ in range(2):
            with pytest.raises(RequestError) as caught:
                await fetcher.get("https://down.example/page")
            assert caught.value.issue.code == "robots_unavailable"
    assert calls == ["https://down.example/robots.txt"]


async def test_idle_host_state_is_bounded(monkeypatch):
    async with SafeFetcher(respect_robots=False, min_interval=0) as fetcher:
        fetcher.MAX_TRACKED_HOSTS = 4

        async def read(session, url, *args):
            return _ok(url)

        monkeypatch.setattr(fetcher, "_read_response", read)
        monkeypatch.setattr(fetcher, "_get_session", lambda **kwargs: _none())
        for index in range(20):
            await fetcher.get(f"https://host{index}.example/")
        assert len(fetcher._locks) <= fetcher.MAX_TRACKED_HOSTS + 1
        assert len(fetcher._host_slots) <= fetcher.MAX_TRACKED_HOSTS + 1


def test_html_charset_is_sniffed_when_headers_omit_it():
    body = '<meta charset="gbk"><title>项目主页</title>'.encode("gbk")
    page = FetchResult("https://a.example/", "https://a.example/", 200, {}, body)
    assert "项目主页" in page.text
    declared = FetchResult(
        "https://a.example/", "https://a.example/", 200, {"content-type": "text/html"}, body
    )
    assert "项目主页" in declared.text
    utf8 = FetchResult("https://a.example/", "https://a.example/", 200, {}, "é".encode())
    assert utf8.text == "é"


def test_long_button_rows_keep_resource_buttons():
    buttons = "".join(
        f'<a href="https://x{index}.example/">Slides {index}</a>' for index in range(8)
    )
    page = parse_project_page(
        f"""<div><a href="https://github.com/team/ours">Code</a>
        <a href="https://ours.github.io/project/">Project Page</a>{buttons}</div>""",
        "https://ours.github.io/project/",
    )
    urls = {link.url for link in page.links}
    assert "https://github.com/team/ours" in urls
    assert "https://ours.github.io/project/" in urls
    assert not any("x1.example" in url for url in urls)


def test_large_pages_parse_in_linear_time():
    links = "".join(f'<li><a href="https://e{i}.org/p">x{i}</a></li>' for i in range(3000))
    headings = "".join(f"<h3>Section {i}</h3><p>text</p>" for i in range(2000))
    started = time.perf_counter()
    page = parse_project_page(
        f"<html><body>{headings}<ul>{links}</ul></body></html>", "https://a.example/"
    )
    assert time.perf_counter() - started < 3
    assert page.links


def test_link_sections_still_follow_the_nearest_heading():
    page = parse_project_page(
        """<h2>Overview</h2><p><a href="https://github.com/team/ours">Code</a></p>
        <h2>Related work</h2><p><a href="https://github.com/team/other">Code</a></p>
        <h2>Resources</h2><p><a href="https://huggingface.co/datasets/team/d">Dataset</a></p>""",
        "https://ours.example/",
    )
    assert {link.url for link in page.links} == {
        "https://github.com/team/ours",
        "https://huggingface.co/datasets/team/d",
    }


def test_video_sources_merge_and_secondary_images_rank_lower():
    visuals = discover_visuals(
        """<h2>Results</h2><img src="results.png" width="900">
        <h2>Abstract</h2><img src="teaser.png" width="900">
        <video><source src="clip.mp4"><source src="clip.webm"></video>""",
        "https://ours.example/",
    )
    videos = [item for item in visuals if item.media_type == "video"]
    assert len(videos) == 1
    assert [source.url for source in videos[0].alternatives] == ["https://ours.example/clip.webm"]
    images = [item.url for item in visuals if item.media_type == "image"]
    assert images.index("https://ours.example/teaser.png") < images.index(
        "https://ours.example/results.png"
    )


@pytest.mark.parametrize(
    ("url", "kind", "platform"),
    [
        ("https://huggingface.co/papers/2410.12345", "paper", "hugging_face"),
        ("https://huggingface.co/team/model", "model", "hugging_face"),
        ("https://gitee.com/team/repo", "repository", "gitee"),
        ("https://codeberg.org/team/repo", "repository", "codeberg"),
        ("https://zenodo.org/records/123", "dataset", "zenodo"),
        ("https://www.kaggle.com/datasets/team/data", "dataset", "kaggle"),
        ("https://www.modelscope.cn/models/team/model", "model", "modelscope"),
        ("https://colab.research.google.com/drive/abc", "demo", "colab"),
        ("https://github.com/sponsors/someone", "other", "github"),
        ("https://github.com/team/ours", "repository", "github"),
    ],
)
def test_resource_platforms(url, kind, platform):
    link = classify_link(url)
    assert (link.kind, link.platform) == (kind, platform)


def test_file_hosts_are_not_promoted_to_project_pages():
    assert project_page_exclusion("https://drive.google.com/file/d/x") == "file_host"
    paper = Paper(
        arxiv_id="2410.12345", title="Example", abstract="Data: https://drive.google.com/file/d/x"
    )
    assert seed_link(extract_metadata_links(paper)[0]).kind == "other"


def test_scheme_less_repository_links_are_extracted():
    links = extract_text_links(
        "Code: github.com/team/ours. Weights at huggingface.co/team/model, see "
        "https://example.org/github.com/not-a-root and user@github.com/x.",
        source="abstract",
    )
    assert [link.url for link in links] == [
        "https://github.com/team/ours",
        "https://huggingface.co/team/model",
        "https://example.org/github.com/not-a-root",
    ]


def test_root_github_pages_with_resource_context_are_promoted():
    paper = Paper(
        arxiv_id="2410.12345",
        title="Example",
        abstract="Code and models are fully open-sourced: https://ours.github.io/",
    )
    assert seed_link(extract_metadata_links(paper)[0]).kind == "project_page"


def test_hugging_face_paper_pages_identify_the_paper():
    links = [classify_link("https://huggingface.co/papers/2410.12345v2")]
    assert linked_paper_ids(links) == ["2410.12345"]


def test_paper_association_tolerates_cited_prior_work():
    paper = Paper(arxiv_id="2410.12345", title="A synthetic research project")
    assert related_to_paper(paper, ["2410.12345"])
    assert related_to_paper(paper, ["2410.12345", "2301.00001"], "A Synthetic Research Project")
    assert not related_to_paper(paper, ["2410.12345", "2301.00001"], "Lab publications")
    assert not related_to_paper(paper, ["2301.00001"], "A synthetic research project")


def test_terminal_issues_use_status_with_legacy_message_fallback():
    assert terminal_page_issue("http_error", "HTTP 404")
    assert terminal_page_issue("http_error", "Not found", 404)
    assert not terminal_page_issue("http_error", "HTTP 503", 503)
    assert not terminal_page_issue("http_error", "unexpected")


async def test_malformed_lookup_batch_isolates_the_bad_reference(monkeypatch):
    client_module = importlib.import_module("pwp.harvest.client")
    requests = []

    async def get(url, params):
        requests.append(params["id_list"])
        return params["id_list"].encode()

    def parse(content):
        ids = content.decode().split(",")
        if "2410.99999" in ids:
            raise MetadataError("invalid_entry", "Invalid Atom entry")
        return [Paper(arxiv_id=identifier, title="T", source="arxiv_api") for identifier in ids]

    monkeypatch.setattr(client_module, "parse_atom", parse)
    async with ArxivClient() as client:
        monkeypatch.setattr(client._transport, "get", get)
        results = await client.get_papers(["2410.00001", "2410.99999", "2410.00002", "2410.00003"])
    assert [result.issue.code if result.issue else "ok" for result in results] == [
        "ok",
        "invalid_entry",
        "ok",
        "ok",
    ]
    assert len(requests) > 1


async def test_resolve_many_isolates_unexpected_paper_failures(monkeypatch):
    module = importlib.import_module("pwp.resolve")

    async def work(paper, **kwargs):
        if paper == "broken":
            raise RuntimeError("parser exploded")
        return PaperResult(requested=paper)

    monkeypatch.setattr(module, "resolve", work)
    results = [result async for result in resolve_many(["ok", "broken", "fine"], render=False)]
    by_id = {result.requested: result for result in results}
    assert set(by_id) == {"ok", "broken", "fine"}
    assert by_id["broken"].status == "failed"
    assert by_id["broken"].steps[0].issue.code == "internal_error"

    async def callback_failure(paper, **kwargs):
        raise EventCallbackError("Progress callback failed")

    monkeypatch.setattr(module, "resolve", callback_failure)
    with pytest.raises(EventCallbackError):
        async for _ in resolve_many(["ok"], render=False):
            pass


async def test_readme_falls_back_to_api_and_rate_limits_suggest_a_token():
    import base64

    readme = "[Project page](https://project.example/)"
    responses = {
        "https://api.github.com/repos/team/ours": {"full_name": "team/ours"},
        "https://api.github.com/repos/team/ours/readme": {
            "encoding": "base64",
            "content": base64.b64encode(readme.encode()).decode(),
            "path": "docs/README.rst",
        },
    }
    fetcher = FakeFetcher(responses)
    repo = await inspect_repo("https://github.com/team/ours", fetcher=fetcher)
    assert repo.readme_url == "https://github.com/team/ours/blob/main/docs/README.rst"
    assert any(link.url == "https://project.example/" for link in repo.links)
    assert fetcher.calls[1][0].startswith("https://raw.githubusercontent.com/")

    limited = FetchResult(
        "https://api.github.com/repos/team/ours",
        "https://api.github.com/repos/team/ours",
        403,
        {"x-ratelimit-remaining": "0"},
        b"",
    )
    with pytest.raises(RequestError, match="GitHub token") as caught:
        await inspect_repo(
            "https://github.com/team/ours",
            fetcher=FakeFetcher({"https://api.github.com/repos/team/ours": limited}),
        )
    assert caught.value.issue.http_status == 403


def test_cli_reads_github_token_from_environment(monkeypatch):
    from pwp.cli import _parser

    monkeypatch.setenv("PWP_GITHUB_TOKEN", "test-token")
    assert _parser().parse_args(["resolve", "2410.12345"]).github_token == "test-token"
