import asyncio
import importlib
import io
import json

import pytest
from helpers import FakeFetcher
from PIL import Image
from pwp import (
    Paper,
    PaperResult,
    create_thumbnail,
    discover_visuals,
    download_image,
    from_jsonl,
    resolve,
    resolve_many,
    to_jsonl,
)
from pwp.cli import main
from pwp.errors import PwpError, RequestError
from pwp.media import image_info


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (640, 360), "#4488ff").save(output, format="PNG")
    return output.getvalue()


async def test_image_download_hash_and_thumbnail(tmp_path):
    candidate = discover_visuals('<img src="hero.png">', "https://project.example/")[0]
    fetcher = FakeFetcher({candidate.url: png_bytes()})
    artifact = await download_image(candidate, work_dir=tmp_path, fetcher=fetcher)
    assert artifact.path.exists() and artifact.width == 640 and artifact.height == 360
    assert artifact.path.stem == artifact.sha256
    thumbnail = create_thumbnail(artifact, work_dir=tmp_path, width=320)
    assert (thumbnail.width, thumbnail.height) == (320, 180)
    assert thumbnail.kind == "thumbnail"
    assert thumbnail.candidate_id == candidate.id
    assert not list(tmp_path.glob("*.tmp"))


async def test_video_download_is_not_silently_enabled(tmp_path):
    candidate = discover_visuals('<video src="demo.mp4"></video>', "https://project.example/")[0]
    with pytest.raises(ValueError):
        await download_image(candidate, work_dir=tmp_path, fetcher=FakeFetcher({}))
    with pytest.raises(RequestError):
        image_info(b"<html>not an image</html>")


async def test_resolve_without_links_keeps_paper(tmp_path):
    paper = Paper(arxiv_id="2410.12345", title="No links")
    result = await resolve(paper, render=False, fetcher=FakeFetcher({}))
    assert result.paper == paper and result.status == "completed"
    assert not result.resources
    assert list(from_jsonl(to_jsonl([result])))[0] == result


async def test_partial_result_retains_page_when_render_fails(monkeypatch, tmp_path, project_html):
    module = importlib.import_module("pwp.resolve")

    async def fail(*args, **kwargs):
        raise PwpError("render_unavailable", "Synthetic missing browser")

    monkeypatch.setattr(module, "render_page", fail)
    result = await resolve(
        Paper(arxiv_id="2410.12345", title="Example", abstract="https://project.example/"),
        work_dir=tmp_path,
        fetcher=FakeFetcher({"https://project.example/": project_html}),
    )
    assert result.status == "partial" and result.pages
    assert any(step.issue and step.issue.code == "render_unavailable" for step in result.steps)
    assert result.visuals


async def test_terminal_main_document_is_not_retried_as_static_page(tmp_path):
    from pwp.network import FetchResult

    url = "https://project.example/"
    fetcher = FakeFetcher(
        {url: FetchResult(url, url, 404, {"content-type": "text/html"}, b"Missing")}
    )
    result = await resolve(
        Paper(arxiv_id="2410.12345", title="Example", comments=url),
        work_dir=tmp_path,
        fetcher=fetcher,
    )
    assert [call[0] for call in fetcher.calls] == [url]
    assert not result.pages and not result.media
    assert result.resources[0].kind == "other"
    assert any(step.issue and step.issue.message == "HTTP 404" for step in result.steps)


async def test_rendered_links_discovered_and_capture_not_repeated(monkeypatch, tmp_path):
    module = importlib.import_module("pwp.resolve")
    from pwp.capture import RenderResult
    from pwp.page import parse_project_page

    calls = []

    async def render(url, **kwargs):
        calls.append(url)
        html = '<a href="https://huggingface.co/datasets/example/data">Dataset</a>'
        return RenderResult(parse_project_page(html, url), html)

    monkeypatch.setattr(module, "render_page", render)
    result = await resolve(
        Paper(arxiv_id="2410.12345", title="Example", abstract="https://project.example/"),
        work_dir=tmp_path,
        fetcher=FakeFetcher({}),
    )
    assert calls == ["https://project.example/"]
    assert any(item.kind == "dataset" for item in result.resources)


async def test_resolve_many_bounded_and_cancelled_on_close(monkeypatch):
    module = importlib.import_module("pwp.resolve")
    pending = 0
    maximum = 0
    closed = []

    async def work(paper, **kwargs):
        nonlocal pending, maximum
        pending += 1
        maximum = max(maximum, pending)
        try:
            if paper != "first":
                await asyncio.Event().wait()
            return PaperResult(requested=paper)
        finally:
            pending -= 1
            closed.append(paper)

    monkeypatch.setattr(module, "resolve", work)
    stream = resolve_many(["first", "second", "third"], concurrency=2, render=False)
    result = await anext(stream)
    assert result.requested == "first"
    await stream.aclose()
    assert pending == 0 and maximum <= 2
    assert "third" not in closed


def test_cli_extract_keeps_no_link_records(tmp_path):
    source = tmp_path / "papers.jsonl"
    target = tmp_path / "links.jsonl"
    source.write_text(
        Paper(arxiv_id="2410.12345", title="No links").model_dump_json() + "\n", encoding="utf-8"
    )
    assert main(["extract", str(source), "-o", str(target)]) == 0
    result = json.loads(target.read_text())
    assert result["paper"]["arxiv_id"] == "2410.12345" and result["links"] == []
    before = source.read_bytes()
    assert main(["extract", str(source), "-o", str(source)]) == 1
    assert source.read_bytes() == before


def test_cli_export_and_invalid_jsonl(tmp_path):
    source, target = tmp_path / "input.jsonl", tmp_path / "output.json"
    source.write_text(PaperResult(requested="2410.12345").model_dump_json(), encoding="utf-8")
    assert main(["export", str(source), "--format", "json", "-o", str(target)]) == 0
    assert json.loads(target.read_text())[0]["requested"] == "2410.12345"
    with pytest.raises(ValueError, match="line 2"):
        list(from_jsonl([PaperResult(requested="ok").model_dump_json(), "broken"]))


def test_cli_extract_accepts_harvest_batches_without_dropping_failures(tmp_path, oai_xml):
    from pwp.harvest import parse_oai

    source, target = tmp_path / "batches.jsonl", tmp_path / "links.jsonl"
    source.write_text(parse_oai(oai_xml).model_dump_json(), encoding="utf-8")
    assert main(["extract", str(source), "--input-model", "batch", "-o", str(target)]) == 0
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert [row["record"]["status"] for row in rows] == ["active", "deleted", "invalid"]
