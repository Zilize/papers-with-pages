from pwp import discover_visuals, parse_bibtex, parse_project_page, select_main_video


def test_main_video_prefers_title_action_and_merges_embed_identity():
    html = """<h1>Project</h1>
    <a href="https://youtu.be/abcDEF12345">Video</a>
    <video class="teaser" src="teaser.mp4"></video>
    <h2>Results and comparisons</h2>
    <iframe src="https://www.youtube.com/embed/abcDEF12345"></iframe>
    <video class="demo" width="1920" height="1080" src="result.mp4"></video>"""
    candidates = discover_visuals(html, "https://project.example/")
    main = select_main_video(candidates)
    assert main.provider_id == "abcDEF12345"
    assert main.main_video_score == 100
    assert main.main_video_reasons == ["explicit video action near the title"]
    assert len(main.alternatives) == 1
    assert len(candidates) == 3


def test_main_video_rejects_hidden_footer_results_and_observed_only():
    html = """<div hidden><video class="teaser" src="hidden.mp4"></video></div>
    <footer><a href="https://youtu.be/abcDEF12345">Video</a></footer>
    <h2>Ablations</h2><video class="teaser" src="ablation.mp4"></video>"""
    candidates = discover_visuals(
        html, "https://project.example/", observed_media=["https://project.example/hidden.mp4"]
    )
    assert select_main_video(candidates) is None


def test_main_video_requires_semantic_or_early_large_player_signal():
    assert (
        select_main_video(
            discover_visuals('<video src="a.mp4"></video>', "https://project.example/")
        )
        is None
    )
    candidates = discover_visuals(
        '<h1>Project</h1><video width="960" height="540" src="a.mp4"></video>',
        "https://project.example/",
    )
    assert select_main_video(candidates).main_video_score == 60
    candidates = discover_visuals(
        '<h2>Implementation</h2><video width="960" height="540" src="a.mp4"></video>',
        "https://project.example/",
    )
    assert select_main_video(candidates) is None


def test_page_resources_citations_and_provenance(project_html):
    page = parse_project_page(project_html, "https://project.example/")
    assert page.title == "A synthetic research project"
    assert page.related_paper_ids == ["2410.12345"]
    assert page.authors == ["Alex Example"]
    assert page.institutions == ["Example Institute"]
    assert page.citations[0].entries[0]["year"] == "2024"
    assert page.canonical_url == "https://project.example/"
    assert all("template" not in link.url for link in page.links)
    code = next(link for link in page.links if link.kind == "repository")
    assert code.evidence[0].anchor == "Code"
    assert code.evidence[0].source_url == page.final_url


def test_main_visuals_unify_videos_embeds_and_images(project_html):
    visuals = discover_visuals(
        project_html,
        "https://project.example/",
        observed_media=["https://project.example/demo.mp4"],
    )
    assert len(visuals) == 3
    assert [item.rank for item in visuals] == [1, 2, 3]
    assert all("logo" not in item.url and "poster.png" not in item.url for item in visuals)
    youtube = next(item for item in visuals if item.provider == "youtube")
    assert youtube.media_type == "video" and youtube.delivery == "embed"
    assert youtube.provider_id == "abcDEF12345"
    assert len(youtube.alternatives) == 1
    direct = next(
        item for item in visuals if item.delivery == "direct" and item.media_type == "video"
    )
    assert direct.poster_url == "https://project.example/poster.png"
    assert len(direct.evidence) == 2


def test_base_tag_relative_urls_and_hidden_visuals():
    html = """<base href="/assets/"><a href="../code">Project page</a>
      <img src="fallback.png" srcset="small.png 320w, large.png 1200w">
      <img src="hidden.png" style="display: none"><img src="tiny.png" width="20">
      <img src="javascript:alert(1)"><iframe src="http://localhost/private"></iframe>"""
    page = parse_project_page(html, "https://project.example/index.html")
    assert page.links[0].url == "https://project.example/code"
    assert page.links[0].kind == "project_page"
    assert [visual.url for visual in page.visuals] == ["https://project.example/assets/large.png"]


def test_empty_and_invalid_citations_are_explicit():
    assert parse_bibtex("not bibtex").issue.code == "invalid_bibtex"
    assert discover_visuals("<h1>No media</h1>", "https://project.example/") == []


def test_numbered_demo_heading_and_camel_case_player():
    candidates = discover_visuals(
        '<h2>01 Task Execution Demo</h2><section id="demo">'
        '<video id="demoVideo" src="demo.mp4"></video></section>',
        "https://project.example/",
    )
    assert select_main_video(candidates).main_video_score == 80


def test_player_fallback_excludes_supplementary_hidden_and_network_only_media():
    candidates = discover_visuals(
        '<video src="first.mp4"></video><h2>Results</h2>'
        '<video src="experiment.mp4"></video><footer><video src="footer.mp4"></video></footer>',
        "https://project.example/",
        observed_media=[
            "https://project.example/network.mp4",
            "https://project.example/experiment.mp4",
        ],
    )
    assert select_main_video(candidates) is None
    assert select_main_video(candidates, allow_player_fallback=True).url.endswith("first.mp4")
    assert select_main_video(candidates[1:], allow_player_fallback=True) is None


def test_multiple_papers_stay_page_facts():
    page = parse_project_page(
        """<a href="https://arxiv.org/abs/2410.12345">Paper one</a>
      <a href="https://arxiv.org/abs/2410.22222">Paper two</a>""",
        "https://project.example/",
    )
    assert len(page.related_paper_ids) == 2


def test_lazy_picture_empty_srcset_and_unknown_embed():
    html = """<img src="data:image/png;base64,x" data-src="lazy.png" srcset=" , ">
      <picture><source srcset="large.webp 1000w"><img src="small.jpg"></picture>
      <iframe title="Interactive demo" src="https://demo.example/"></iframe>"""
    visuals = discover_visuals(html, "https://project.example/")
    assert {item.url for item in visuals} == {
        "https://project.example/lazy.png",
        "https://project.example/large.webp",
        "https://demo.example/",
    }
    assert next(item for item in visuals if item.delivery == "embed").media_type == "unknown"
