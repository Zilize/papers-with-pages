"""Materialize image artifacts in caller-owned directories."""

import hashlib
import io
import os
import warnings
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from pwp.errors import RequestError
from pwp.models import MediaArtifact
from pwp.network import SafeFetcher, require_success


def image_info(data: bytes):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height, format_name = image.width, image.height, image.format
                image.verify()
                if format_name not in {"PNG", "JPEG", "WEBP", "GIF", "AVIF"}:
                    raise ValueError("Unsupported image format")
                return width, height, format_name
    except (
        ValueError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise RequestError("invalid_image", "Invalid or unsafe image data") from exc


def save_image(
    data: bytes,
    directory,
    *,
    source_url,
    kind="image",
    candidate_id=None,
    viewport=None,
    device_scale_factor=None,
) -> MediaArtifact:
    width, height, format_name = image_info(data)
    digest = hashlib.sha256(data).hexdigest()
    extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "AVIF": "avif"}[
        format_name
    ]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.{extension}"
    temporary = directory / f".{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    except OSError as exc:
        raise RequestError("artifact_write_failed", "Cannot write image artifact") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return MediaArtifact(
        id=digest,
        kind=kind,
        path=target.resolve(),
        source_url=source_url,
        candidate_id=candidate_id,
        content_type=Image.MIME[format_name],
        bytes=len(data),
        sha256=digest,
        width=width,
        height=height,
        captured_at=datetime.now(UTC),
        viewport_width=viewport[0] if viewport else None,
        viewport_height=viewport[1] if viewport else None,
        device_scale_factor=device_scale_factor,
    )


async def download_image(candidate, *, work_dir, fetcher=None, max_bytes=20 * 1024 * 1024):
    if candidate.media_type != "image" or candidate.delivery != "direct":
        raise ValueError("Only direct image candidates can be downloaded")
    if fetcher is None:
        async with SafeFetcher() as owned:
            return await download_image(
                candidate, work_dir=work_dir, fetcher=owned, max_bytes=max_bytes
            )
    response = await fetcher.get(candidate.url, max_bytes=max_bytes)
    require_success(response)
    return save_image(response.body, work_dir, source_url=candidate.url, candidate_id=candidate.id)


def create_thumbnail(artifact: MediaArtifact, *, work_dir, width=720) -> MediaArtifact:
    if width < 1:
        raise ValueError("Thumbnail width must be positive")
    data = artifact.path.read_bytes()
    image_info(data)
    with Image.open(io.BytesIO(data)) as image:
        image.thumbnail((width, max(1, round(image.height * width / image.width))))
        output = io.BytesIO()
        image.convert("RGBA" if "A" in image.getbands() else "RGB").save(output, format="WEBP")
    return save_image(
        output.getvalue(),
        work_dir,
        source_url=artifact.source_url,
        candidate_id=artifact.candidate_id,
        kind="thumbnail",
    )
