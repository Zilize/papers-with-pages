"""App-owned metadata, immutable attempts, and atomic display pointers."""

import hashlib
import io
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PIL import Image
from pwp.extract.links import classify_link, project_page_exclusion
from pwp.media import image_info
from pwp.models import HarvestRecord, Paper, PaperResult

from app.presentation import prepare

_IO_LOCK = threading.RLock()
# Bump when gallery entries change shape or presentation rules change.
GALLERY_INDEX_VERSION = 1
# Results are immutable per run, so these files identify a paper's gallery entry.
_SIGNATURE_FILES = ("metadata.json", "current.json", "latest.json")
# Gallery cards are at most ~480 CSS px wide; 960 covers 2x displays.
THUMBNAIL_WIDTHS = (480, 960)


def _file_operation(operation):
    """Retry short Windows sharing conflicts without hiding persistent failures."""
    for delay in (0.01, 0.03, 0.06, 0.1, None):
        try:
            return operation()
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def now() -> str:
    return datetime.now(UTC).isoformat()


def paper_key(identifier: str) -> str:
    return identifier.encode("utf-8").hex()


def fingerprint(paper: Paper) -> str:
    payload = paper.model_dump(mode="json", exclude={"source", "oai_datestamp"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def read_json(path: Path, default=None):
    try:
        with _IO_LOCK:
            text = _file_operation(lambda: path.read_text(encoding="utf-8"))
        return json.loads(text)
    except FileNotFoundError:
        return default


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Gallery reads run in a thread while collection replaces pointers.
        # Windows cannot always replace a destination with an open reader.
        with _IO_LOCK:
            _file_operation(lambda: os.replace(temporary, path))
    finally:
        temporary.unlink(missing_ok=True)


def _signature(folder: Path) -> dict:
    signature = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name in _SIGNATURE_FILES:
                stat = entry.stat()
                signature[entry.name] = [stat.st_mtime_ns, stat.st_size]
    return signature


def thumbnail_path(source: Path, width: int) -> Path:
    return source.with_name(f"{source.stem}.w{width}.webp")


def write_thumbnail(source: Path, width: int = THUMBNAIL_WIDTHS[0]) -> Path:
    """Write a WebP of one gallery width beside a content-addressed image once."""
    if width not in THUMBNAIL_WIDTHS:
        raise ValueError(f"Unsupported thumbnail width: {width}")
    target = thumbnail_path(source, width)
    if target.exists():
        return target
    data = source.read_bytes()
    image_info(data)
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        if image.width > width:
            # One Lanczos pass keeps screenshot text sharper than reduce-then-bicubic.
            height = max(1, round(image.height * width / image.width))
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=90, method=6)
    # Hash-named targets sit deep in assets; keep temporary names short for Windows MAX_PATH.
    temporary = target.with_name(f".{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(output.getvalue())
        with _IO_LOCK:
            _file_operation(lambda: os.replace(temporary, target))
    finally:
        temporary.unlink(missing_ok=True)
    return target


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        for name in ("papers", "assets", "runs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def metadata(self, key: str):
        return read_json(self.root / "papers" / key / "metadata.json")

    def merge(self, record: HarvestRecord) -> str | None:
        if record.status == "invalid":
            return None
        identifier = (
            record.paper.arxiv_id
            if record.paper
            else record.identifier.removeprefix("oai:arXiv.org:")
        )
        key = paper_key(identifier)
        previous = self.metadata(key)
        stamp = str(record.datestamp or "")
        if previous and previous["datestamp"] > stamp:
            return key
        paper = record.paper.model_dump(mode="json") if record.paper else None
        if record.status == "deleted" and previous:
            paper = previous["paper"]
        atomic_json(
            self.root / "papers" / key / "metadata.json",
            {
                "paper": paper,
                "deleted": record.status == "deleted",
                "datestamp": stamp,
                "fingerprint": fingerprint(record.paper) if record.paper else None,
            },
        )
        return key

    def current(self, key: str):
        return read_json(self.root / "papers" / key / "current.json")

    def reusable(self, key: str) -> bool:
        metadata, current = self.metadata(key), self.current(key)
        latest = read_json(self.root / "papers" / key / "latest.json")
        reusable = bool(
            metadata
            and not metadata["deleted"]
            and current
            and current["status"] == "completed"
            and current["fingerprint"] == metadata["fingerprint"]
            and latest
            and latest["run_id"] == current["run_id"]
        )
        if not reusable:
            return False
        result = read_json(self.root / "papers" / key / "results" / f"{current['run_id']}.json")
        if result is None:
            return False
        transient = {
            "asset_timeout",
            "robots_unavailable",
            "rate_limited",
            "backoff_active",
            "fetch_failed",
            "asset_route_failed",
            "render_incomplete",
            "capture_failed",
            "video_frame_unavailable",
            "visual_assets_incomplete",
        }
        return not any(
            issue and (issue.get("retryable") or issue["code"] in transient)
            for step in result.get("steps", [])
            for issue in (step.get("issues") or [step.get("issue")])
        )

    def save_result(self, key: str, run_id: str, result: PaperResult) -> None:
        payload = result.model_dump(mode="json")
        for media in payload["media"]:
            path = Path(media["path"]).resolve()
            media["path"] = path.relative_to(self.root / "assets").as_posix()
        folder = self.root / "papers" / key
        atomic_json(folder / "results" / f"{run_id}.json", payload)
        pointer = {
            "run_id": run_id,
            "status": result.status,
            "fingerprint": self.metadata(key)["fingerprint"],
            "updated_at": now(),
        }
        atomic_json(folder / "latest.json", pointer)
        if result.status == "completed" or (
            self.current(key) is None and result.status == "partial"
        ):
            atomic_json(folder / "current.json", pointer)

    def detail(self, key: str):
        metadata, current = self.metadata(key), self.current(key)
        if not metadata or metadata["deleted"] or not metadata["paper"]:
            return None
        result = (
            read_json(self.root / "papers" / key / "results" / f"{current['run_id']}.json")
            if current
            else None
        )
        if result:
            for resource in result["resources"]:
                exclusion = project_page_exclusion(resource["url"])
                if resource["kind"] == "project_page" and exclusion not in {
                    None,
                    "ambiguous_homepage",
                }:
                    resource["kind"] = classify_link(resource["url"]).kind
        latest = read_json(self.root / "papers" / key / "latest.json")
        presentation = prepare(metadata["paper"], result)
        return {
            "key": key,
            "paper": metadata["paper"],
            "result": result,
            **presentation,
            "updated_at": current["updated_at"] if current else None,
            "stale": bool(
                current
                and (
                    current["fingerprint"] != metadata["fingerprint"]
                    or (latest and latest["run_id"] != current["run_id"])
                )
            ),
        }

    def gallery_entry(self, key: str):
        detail = self.detail(key)
        if detail is None:
            return {"saved": False, "item": None}
        result = detail["result"]
        resources = (
            [r for r in result["resources"] if r["kind"] not in {"paper", "other"}]
            if result
            else []
        )
        if not resources:
            return {"saved": True, "item": None}
        return {
            "saved": True,
            "item": {
                "key": detail["key"],
                "paper": detail["paper"],
                "resources": resources,
                "cover": detail["cover"],
                "thumbnail": detail["thumbnail"],
                "videos": detail["videos"],
                "problems": detail["problems"],
                "warnings": detail["warnings"],
                "quality": detail["quality"],
                "stale": detail["stale"],
                "status": result["status"],
            },
        }

    def gallery(self):
        # Users may clear the collection while the local server remains open.
        for name in ("papers", "assets", "runs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        index_path = self.root / "gallery-index.json"
        index = read_json(index_path) or {}
        cached = index.get("entries", {}) if index.get("version") == GALLERY_INDEX_VERSION else {}
        entries = {}
        for folder in (self.root / "papers").iterdir():
            if not folder.is_dir():
                continue
            # Taken before reading, so a concurrent write always leaves a newer signature.
            signature = _signature(folder)
            previous = cached.get(folder.name)
            if previous and previous["signature"] == signature:
                entries[folder.name] = previous
            else:
                entries[folder.name] = {"signature": signature, **self.gallery_entry(folder.name)}
        if entries != cached:
            atomic_json(index_path, {"version": GALLERY_INDEX_VERSION, "entries": entries})
        items = [entry["item"] for entry in entries.values() if entry["item"]]
        items.sort(key=lambda i: (str(i["paper"]["published_at"] or ""), i["key"]), reverse=True)
        return {"items": items, "saved": sum(entry["saved"] for entry in entries.values())}

    def save_run(self, state):
        atomic_json(self.root / "runs" / f"{state['id']}.json", state)

    def latest_run(self):
        paths = sorted((self.root / "runs").glob("*.json"), reverse=True)
        return read_json(paths[0]) if paths else None

    def recover_runs(self):
        for path in (self.root / "runs").glob("*.json"):
            state = read_json(path)
            if state["status"] == "running":
                state.update(status="interrupted", phase="interrupted", finished_at=now())
                state["error"] = "The app stopped. Run this date range again to continue."
                atomic_json(path, state)
