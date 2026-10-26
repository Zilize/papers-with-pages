import base64

import pytest
from helpers import FakeFetcher
from pwp import DiscoveryLimits, Paper, discover_resources, inspect_repo
from pwp.errors import RequestError
from pwp.models import ProjectPage
from pwp.network import FetchResult


def repo_responses(project_html):
    readme = """# A synthetic research project
[Project page](https://project.example/)
[Paper](https://arxiv.org/abs/2410.12345)
[Dataset](https://huggingface.co/datasets/example/data)
[Relative resource](docs/usage.md)
"""
    return {
        "https://api.github.com/repos/example/project": {
            "full_name": "example/project",
            "homepage": "https://project.example/",
            "description": "A synthetic research project",
            "default_branch": "main",
            "stargazers_count": 42,
            "license": {"spdx_id": "MIT"},
        },
        "https://raw.githubusercontent.com/example/project/main/README.md": FetchResult(
            "https://raw.githubusercontent.com/example/project/main/README.md",
            "https://raw.githubusercontent.com/example/project/main/README.md",
            200,
            {"content-type": "text/plain; charset=utf-8"},
            readme.encode(),
        ),
        "https://api.github.com/repos/example/project/readme": {
            "encoding": "base64",
            "content": base64.b64encode(readme.encode()).decode(),
            "path": "README.md",
        },
        "https://project.example/": project_html,
    }


async def test_repository_page_cycle_and_terminal_resources(project_html):
    fetcher = FakeFetcher(repo_responses(project_html))
    paper = Paper(
        arxiv_id="2410.12345",
        title="A synthetic research project",
        abstract="Code: https://github.com/example/project",
    )
    events = []
    result = await discover_resources(paper, fetcher=fetcher, on_event=events.append)
    assert len(fetcher.calls) == 3
    assert len(result.repositories) == len(result.pages) == 1
    assert not result.limit_reached
    dataset = next(link for link in result.resources if link.kind == "dataset")
    assert dataset.relation == "related"
    assert not any("huggingface" in call[0] for call in fetcher.calls)
    assert events[0].type == "step_started" and events[-1].type == "step_completed"


async def test_page_to_repository_and_relative_readme_links(project_html):
    fetcher = FakeFetcher(repo_responses(project_html))
    paper = Paper(
        arxiv_id="2410.12345",
        title="A synthetic research project",
        comments="https://project.example/",
    )
    result = await discover_resources(paper, fetcher=fetcher)
    repo = result.repositories[0]
    assert repo.stars == 42 and repo.license == "MIT"
    assert any(
        link.url == "https://github.com/example/project/blob/main/docs/usage.md"
        for link in repo.links
    )


async def test_depth_resource_and_request_limits(project_html):
    paper = Paper(
        arxiv_id="2410.12345", title="Test", abstract="https://github.com/example/project"
    )
    for limits, exhausted in [
        # The depth bound is the normal horizon, not an exhausted budget.
        (DiscoveryLimits(max_depth=0), False),
        (DiscoveryLimits(max_resources=1), True),
        (DiscoveryLimits(max_requests=1), True),
    ]:
        fetcher = FakeFetcher(repo_responses(project_html))
        result = await discover_resources(paper, fetcher=fetcher, limits=limits)
        assert result.limit_reached is exhausted
        assert len(fetcher.calls) <= limits.max_requests
        assert len(result.repositories) <= 1
        assert not result.pages


async def test_failed_readme_preserves_repo_metadata(project_html):
    responses = repo_responses(project_html)
    del responses["https://raw.githubusercontent.com/example/project/main/README.md"]
    responses["https://api.github.com/repos/example/project/readme"] = RequestError(
        "http_error", "HTTP 404"
    )
    repo = await inspect_repo("https://github.com/example/project", fetcher=FakeFetcher(responses))
    assert repo.stars == 42
    assert repo.homepage == "https://project.example/"
    assert repo.issues[0].code == "http_error"


async def test_no_links_no_network_and_failed_resource_is_retained():
    paper = Paper(arxiv_id="2410.12345", title="No links")
    fetcher = FakeFetcher({})
    empty = await discover_resources(paper, fetcher=fetcher)
    assert not empty.resources and not fetcher.calls
    failed = await discover_resources(
        paper.model_copy(update={"abstract": "https://broken.example/"}), fetcher=fetcher
    )
    assert len(failed.resources) == 1
    assert failed.steps[0].status == "failed"


async def test_multi_paper_page_does_not_confirm_every_link():
    html = """<a href="https://arxiv.org/abs/2410.12345">Paper</a>
    <a href="https://arxiv.org/abs/2410.22222">Another paper</a>
    <a href="https://huggingface.co/datasets/example/data">Dataset</a>"""
    result = await discover_resources(
        Paper(arxiv_id="2410.12345", title="Example", abstract="https://project.example/"),
        fetcher=FakeFetcher({"https://project.example/": html}),
    )
    assert all(link.relation == "candidate" for link in result.resources)


async def test_redirect_alias_prevents_second_visit(project_html):
    fetcher = FakeFetcher(
        {
            "https://old.example/": FetchResult(
                "https://old.example/",
                "https://new.example/",
                200,
                {"content-type": "text/html"},
                b'<a href="https://new.example/">Project page</a>',
            )
        }
    )
    result = await discover_resources(
        Paper(arxiv_id="2410.12345", title="Example", abstract="https://old.example/"),
        fetcher=fetcher,
    )
    assert len(fetcher.calls) == 1 and len(result.pages) == 1


async def test_rendered_page_loader_participates_in_discovery(project_html):
    calls = []

    async def loader(url):
        calls.append(url)
        return ProjectPage(url=url, final_url=url, title="Rendered project")

    result = await discover_resources(
        Paper(arxiv_id="2410.12345", title="Example", abstract="https://project.example/"),
        fetcher=FakeFetcher({}),
        page_loader=loader,
    )
    assert result.pages[0].title == "Rendered project"
    assert calls == ["https://project.example/"]


async def test_root_github_pages_is_explored_as_a_candidate():
    fetcher = FakeFetcher({"https://project.github.io/": "<h1>Project</h1>"})
    result = await discover_resources(
        Paper(
            arxiv_id="2410.12345", title="Example", abstract="Project: https://project.github.io/"
        ),
        fetcher=fetcher,
    )
    assert len(result.pages) == 1 and result.resources[0].relation == "candidate"


async def test_invalid_repository_response_is_an_explicit_issue():
    fetcher = FakeFetcher(
        {
            "https://api.github.com/repos/example/project": {
                "full_name": "example/project",
                "stargazers_count": "invalid",
            }
        }
    )
    result = await discover_resources(
        Paper(
            arxiv_id="2410.12345", title="Example", abstract="https://github.com/example/project"
        ),
        fetcher=fetcher,
    )
    assert result.steps[0].issue.code == "invalid_repository"


async def test_resource_callback_failure_is_not_converted_to_partial_result(project_html):
    from pwp import EventCallbackError

    def callback(event):
        if event.type == "resource_discovered":
            raise RequestError("consumer_failed", "The application's callback failed")

    with pytest.raises(EventCallbackError):
        await discover_resources(
            Paper(arxiv_id="2410.12345", title="Example", abstract="https://project.example/"),
            fetcher=FakeFetcher(repo_responses(project_html)),
            on_event=callback,
        )


async def test_shared_fetcher_does_not_spend_another_papers_budget(project_html):
    import asyncio

    entered = asyncio.Event()
    noise_done = asyncio.Event()

    class SharedFetcher(FakeFetcher):
        async def get(self, url, **kwargs):
            response = await super().get(url, **kwargs)
            if url == "https://project.example/":
                entered.set()
                await noise_done.wait()
            return response

    fetcher = SharedFetcher(repo_responses(project_html) | {"https://noise.example/": "ok"})
    paper = Paper(arxiv_id="2410.12345", title="Test", comments="https://project.example/")

    async def other_paper():
        await entered.wait()
        for _ in range(5):
            await fetcher.get("https://noise.example/")
        noise_done.set()

    result, _ = await asyncio.gather(
        discover_resources(paper, fetcher=fetcher, limits=DiscoveryLimits(max_requests=4)),
        other_paper(),
    )
    assert len(result.repositories) == 1
    assert not result.limit_reached
