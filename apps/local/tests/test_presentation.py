import pytest
from app.presentation import prepare
from pwp import Paper


@pytest.mark.parametrize(
    "code,message,excluded",
    [
        ("http_error", "HTTP 404", True),
        ("http_error", "HTTP 403", True),
        ("robots_denied", "Denied", True),
        ("blocked_dns", "Denied", True),
        ("redirect_loop", "Loop", True),
        ("not_html", "Not HTML", True),
        ("http_error", "HTTP 429", False),
        ("http_error", "HTTP 503", False),
        ("robots_unavailable", "Unavailable", False),
        ("render_failed", "Timeout", False),
    ],
)
def test_main_document_failure_policy_for_saved_results(code, message, excluded):
    url = "https://project.example/"
    paper = Paper(arxiv_id="2410.12345", title="Example", comments=url)
    result = {
        "resources": [{"url": url, "kind": "project_page"}],
        "media": [],
        "visuals": [],
        "steps": [
            {
                "step": "render_page",
                "status": "failed",
                "resource_url": url,
                "issue": {"code": code, "message": message},
            }
        ],
    }
    view = prepare(paper.model_dump(mode="json"), result)
    assert (result["resources"][0]["kind"] == "other") is excluded
    assert bool(view["problems"]) is not excluded
    assert result["steps"][0]["issue"]["code"] == code


def test_asset_failure_does_not_exclude_main_document():
    url = "https://project.example/"
    paper = Paper(arxiv_id="2410.12345", title="Example", comments=url)
    result = {
        "resources": [{"url": url, "kind": "project_page"}],
        "media": [],
        "visuals": [],
        "steps": [
            {
                "step": "render_page",
                "status": "failed",
                "resource_url": url,
                "issue": {
                    "code": "http_error",
                    "message": "HTTP 404",
                    "resource_url": url + "missing.png",
                },
            }
        ],
    }
    assert prepare(paper.model_dump(mode="json"), result)["problems"]
    assert result["resources"][0]["kind"] == "project_page"


def test_successful_document_overrides_earlier_failure():
    url = "https://project.example/"
    paper = Paper(arxiv_id="2410.12345", title="Example", comments=url)
    result = {
        "resources": [{"url": url, "kind": "project_page"}],
        "media": [],
        "visuals": [],
        "pages": [{"url": url, "final_url": url, "http_status": 200}],
        "steps": [
            {
                "step": "render_page",
                "status": "failed",
                "resource_url": url,
                "issue": {"code": "http_error", "message": "HTTP 404"},
            }
        ],
    }
    prepare(paper.model_dump(mode="json"), result)
    assert result["resources"][0]["kind"] == "project_page"


def test_hero_only_main_page_video_and_supplementary_warnings():
    paper = Paper(arxiv_id="2410.12345", title="Example", comments="https://project.example/")
    result = {
        "resources": [{"url": "https://project.example/", "kind": "project_page"}],
        "media": [
            {
                "kind": "image",
                "path": "teaser.png",
                "source_url": "https://project.example/teaser.png",
            },
            {
                "kind": "hero_screenshot",
                "path": "other.png",
                "source_url": "https://other.example/",
            },
            {
                "kind": "hero_screenshot",
                "path": "hero.png",
                "source_url": "https://project.example/",
            },
        ],
        "visuals": [
            {
                "id": "main-video",
                "main_video_score": 80,
                "media_type": "video",
                "url": "https://project.example/demo.mp4",
                "source_page_url": "https://project.example/",
            },
            {
                "media_type": "video",
                "url": "https://other.example/demo.mp4",
                "source_page_url": "https://other.example/",
            },
        ],
        "steps": [
            {
                "step": "render_page",
                "resource_url": "https://project.example/",
                "issue": {"code": "asset_timeout", "message": "Timeout"},
            }
        ],
    }
    view = prepare(paper.model_dump(mode="json"), result)
    assert view["cover"] == "/assets/hero.png"
    assert len(view["videos"]) == 1
    result["visuals"].append({**result["visuals"][0], "id": "secondary", "main_video_score": 60})
    assert len(prepare(paper.model_dump(mode="json"), result)["videos"]) == 1
    assert len(result["visuals"]) == 3
    assert not view["problems"] and len(view["warnings"]) == 1
    result["media"] = result["media"][:1]
    view = prepare(paper.model_dump(mode="json"), result)
    assert view["cover"] is None
    assert view["problems"]
    for visual in result["visuals"]:
        visual.pop("main_video_score", None)
    assert not prepare(paper.model_dump(mode="json"), result)["videos"]
    result["steps"][0]["issues"] = [
        {"code": "asset_timeout", "message": "Timeout", "resource_url": "https://cdn.example/a.js"},
        {"code": "capture_failed", "message": "Screenshot timed out"},
    ]
    view = prepare(paper.model_dump(mode="json"), result)
    assert view["problems"][0]["message"] == "Screenshot timed out"
    assert view["problems"][1]["url"] == "https://cdn.example/a.js"


def test_saved_unranked_player_is_available_without_recollecting():
    paper = Paper(arxiv_id="2410.12345", title="Example", comments="https://project.example/")
    result = {
        "resources": [{"url": "https://project.example/", "kind": "project_page"}],
        "media": [],
        "steps": [],
        "visuals": [
            {
                "id": "demo",
                "media_type": "video",
                "url": "https://project.example/demo.mp4",
                "source_page_url": "https://project.example/",
                "main_video_score": 0,
                "evidence": ["video element; main-visual label"],
            }
        ],
    }
    assert len(prepare(paper.model_dump(mode="json"), result)["videos"]) == 1
    result["visuals"][0]["main_video_score"] = -80
    assert not prepare(paper.model_dump(mode="json"), result)["videos"]
