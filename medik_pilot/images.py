import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, Iterator
from urllib.parse import unquote, urlparse


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *("COM{}".format(index) for index in range(1, 10)),
    *("LPT{}".format(index) for index in range(1, 10)),
}


def detect_image_type(content: bytes) -> tuple[str, str]:
    """Return a canonical extension and MIME type from the file signature."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if content.startswith(b"BM"):
        return ".bmp", "image/bmp"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    prefix = content[:2048].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if prefix.startswith(b"<svg") or (prefix.startswith(b"<?xml") and b"<svg" in prefix):
        return ".svg", "image/svg+xml"
    return "", ""


def filename_for_image_content(filename: str, content: bytes) -> tuple[str, str]:
    """Align a safe client filename and MIME type with the actual image bytes."""
    suffix, content_type = detect_image_type(content)
    if not suffix:
        return filename, ""
    stem = Path(filename).stem.strip(" ._") or "image"
    return "{}{}".format(stem, suffix), content_type


def client_image_filename(source_url: str, suggested_name: str = "", content_type: str = "") -> str:
    """Return a safe client filename while preserving the source basename."""
    raw_name = suggested_name or ("" if source_url.startswith("data:") else source_url)
    candidate = unquote(Path(urlparse(raw_name).path).name).strip()
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", candidate).strip(" .")
    suffix = Path(candidate).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
        suffix = guessed if guessed in IMAGE_EXTENSIONS else ".png"
        stem = Path(candidate).stem.strip(" ._") if candidate else ""
        candidate = "{}{}".format(stem or "image", suffix)
    if not candidate or len(candidate) > 180 or Path(candidate).stem.upper() in WINDOWS_RESERVED:
        candidate = "image-{}{}".format(
            hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16],
            suffix or ".png",
        )
    return candidate


def stored_image_name(content: bytes, client_filename: str) -> tuple[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    suffix = Path(client_filename).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".png"
    return "{}{}".format(digest, suffix), digest


def iter_image_assets(value: Any) -> Iterator[Dict[str, Any]]:
    """Yield image asset descriptors recursively from a collected payload."""
    if isinstance(value, dict):
        if value.get("asset_kind") == "image" and value.get("filename"):
            yield value
        for nested in value.values():
            yield from iter_image_assets(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_image_assets(nested)


def downloaded_image_filenames(value: Any) -> list[str]:
    seen = set()
    filenames = []
    for asset in iter_image_assets(value):
        filename = str(asset.get("filename") or "").strip()
        if asset.get("downloaded") is True and filename and filename not in seen:
            seen.add(filename)
            filenames.append(filename)
    return filenames
