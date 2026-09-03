"""The conversion registry: which tool converts which pair.

Adding a format pair means adding it to the tables below. The routing logic
underneath never needs to change for a new pair.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from detect import CATEGORY_BY_EXT
from handlers import (calibre_handler, ffmpeg_handler, imagemagick_handler,
                      libreoffice_handler, pandoc_handler)

Handler = Callable[..., object]

MAX_CHAIN_HOPS = 2


@dataclass(frozen=True)
class Route:
    source: str
    target: str
    tool_key: str   # key into binaries.TOOLS, for availability checks
    handler: Handler


def _routes(tool_key: str, handler: Handler,
            pairs: list[tuple[str, str]]) -> list[Route]:
    return [Route(s, t, tool_key, handler) for s, t in pairs]


def _all_pairs(formats: list[str]) -> list[tuple[str, str]]:
    """Every ordered pair of distinct formats in a family."""
    return [(a, b) for a in formats for b in formats if a != b]


def _fan(sources: list[str], targets: list[str]) -> list[tuple[str, str]]:
    """Every source to every target, skipping no-op pairs."""
    return [(s, t) for s in sources for t in targets if s != t]


# --------------------------------------------------------------- the tables

RASTER = ["png", "jpg", "webp", "bmp", "tiff", "gif", "avif"]

VIDEO_IN = ["mp4", "mov", "webm", "avi", "mkv"]
VIDEO_OUT = ["mp4", "mov", "webm", "mkv"]
AUDIO_IN = ["mp3", "wav", "flac", "ogg", "m4a"]
AUDIO_OUT = ["mp3", "wav", "flac", "ogg"]

# Formats Pandoc reads and writes as plain markup. Office and ebook targets
# are handled by the dedicated tools where those do a better job.
MARKUP_IN = ["md", "html", "rst", "txt", "docx", "epub"]
MARKUP_OUT = ["md", "html", "rst", "txt", "docx", "epub"]

OFFICE_TEXT = ["docx", "odt", "rtf"]
OFFICE_SHEET = ["xlsx", "ods", "csv"]
OFFICE_SLIDES = ["pptx", "odp"]

EBOOK = ["epub", "mobi", "azw3", "fb2"]


ROUTES: list[Route] = [
    # --- ImageMagick: raster images ------------------------------------
    *_routes("imagemagick", imagemagick_handler.convert_image, [
        *_all_pairs(RASTER),
        # A raster wrapped in a PDF page. (PDF *input* would need Ghostscript,
        # which is a separate install, so it is deliberately not offered.)
        *_fan(RASTER, ["pdf"]),
        # HEIC decoding ships with ImageMagick's Windows build.
        *_fan(["heic"], ["png", "jpg", "webp", "tiff", "avif", "pdf"]),
        *_fan(["svg"], ["png", "jpg", "webp"]),
        *_fan(["ico"], ["png", "jpg"]),
        *_fan(RASTER, ["ico"]),
    ]),

    # --- FFmpeg: video and audio ---------------------------------------
    *_routes("ffmpeg", ffmpeg_handler.convert_media,
             _fan(VIDEO_IN, VIDEO_OUT)),
    *_routes("ffmpeg", ffmpeg_handler.to_gif,
             _fan(VIDEO_IN, ["gif"])),
    # Video to audio drops the video stream rather than trying to encode it.
    *_routes("ffmpeg", ffmpeg_handler.extract_audio,
             _fan(VIDEO_IN, AUDIO_OUT) + _fan(AUDIO_IN, AUDIO_OUT)),

    # --- Pandoc: markup -------------------------------------------------
    *_routes("pandoc", pandoc_handler.convert_markup,
             _fan(MARKUP_IN, MARKUP_OUT)),

    # --- LibreOffice: office documents ----------------------------------
    *_routes("libreoffice", libreoffice_handler.convert_office, [
        *_all_pairs(OFFICE_TEXT),
        *_all_pairs(OFFICE_SHEET),
        *_all_pairs(OFFICE_SLIDES),
        *_fan(OFFICE_TEXT + OFFICE_SHEET + OFFICE_SLIDES, ["pdf"]),
    ]),

    # --- Calibre: ebooks -------------------------------------------------
    *_routes("calibre", calibre_handler.convert_ebook, [
        *_all_pairs(EBOOK),
        *_fan(EBOOK, ["pdf"]),
    ]),
]


def _build_route_map(routes: list[Route]) -> dict[tuple[str, str], Route]:
    """Index the routes, refusing to let two tools silently claim one pair.

    Two entries for the same pair would mean the winner depends on table
    order, which is exactly the kind of thing that goes unnoticed until a
    conversion quietly starts using the wrong tool.
    """
    mapping: dict[tuple[str, str], Route] = {}
    for route in routes:
        key = (route.source, route.target)
        existing = mapping.get(key)
        if existing is not None and existing.handler is not route.handler:
            raise RuntimeError(
                f"Duplicate route for {key}: {existing.tool_key} and "
                f"{route.tool_key} both claim it. Pick one."
            )
        mapping[key] = route
    return mapping


ROUTE_MAP = _build_route_map(ROUTES)


# ------------------------------------------------------------------ chaining

# When more than one intermediate could bridge a pair, prefer the formats that
# lose the least on the way through. A photo routed via BMP keeps its pixels;
# via JPEG it would pick up compression artefacts it can never shed.
_HUB_PREFERENCE = [
    "png", "tiff", "webp", "jpg",          # images: lossless first
    "docx", "html", "md", "epub", "odt",   # documents
    "wav", "flac", "mp3",                  # audio: lossless first
    "mp4", "mkv",                          # video
]


def _hub_rank(ext: str) -> int:
    try:
        return _HUB_PREFERENCE.index(ext)
    except ValueError:
        return len(_HUB_PREFERENCE)


class UnsupportedConversion(Exception):
    """No route exists for this pair. Carries the alternatives worth offering."""

    def __init__(self, source: str, target: str, alternatives: list[str]) -> None:
        self.source = source
        self.target = target
        self.alternatives = alternatives
        if alternatives:
            message = (f"Cannot convert {source.upper()} to {target.upper()}. "
                       f"Supported targets for {source.upper()}: "
                       f"{', '.join(a.upper() for a in alternatives)}.")
        else:
            message = (f"Cannot convert {source.upper()} to {target.upper()}. "
                       f"No conversions are available for {source.upper()} files.")
        super().__init__(message)
        self.message = message


def find_route(source_ext: str, target_ext: str) -> Route:
    """Resolve a direct conversion pair, or raise UnsupportedConversion."""
    route = ROUTE_MAP.get((source_ext, target_ext))
    if route is None:
        raise UnsupportedConversion(source_ext, target_ext, targets_for(source_ext))
    return route


def find_chain(source_ext: str, target_ext: str) -> list[Route] | None:
    """A two-hop path from source to target, or None.

    Capped at two hops on purpose: longer chains compound quality loss and
    take long enough that a clear failure serves the user better than a slow
    surprise.
    """
    mids = []
    for (src, mid), first in ROUTE_MAP.items():
        if src != source_ext or mid == target_ext or mid == source_ext:
            continue
        second = ROUTE_MAP.get((mid, target_ext))
        if second is not None:
            mids.append((_hub_rank(mid), mid, first, second))
    if not mids:
        return None
    mids.sort(key=lambda item: (item[0], item[1]))
    _, _, first, second = mids[0]
    return [first, second]


def plan(source_ext: str, target_ext: str) -> list[Route]:
    """The steps needed to get from source to target: one route, or two.

    Raises UnsupportedConversion when neither a direct route nor a two-hop
    chain exists.
    """
    direct = ROUTE_MAP.get((source_ext, target_ext))
    if direct is not None:
        return [direct]
    chain = find_chain(source_ext, target_ext)
    if chain is not None:
        return chain
    raise UnsupportedConversion(source_ext, target_ext, targets_for(source_ext))


# -------------------------------------------------------------------- lookup

def direct_targets(source_ext: str) -> list[str]:
    return sorted({r.target for r in ROUTES if r.source == source_ext})


def chained_targets(source_ext: str) -> list[str]:
    """Targets reachable only by a two-hop chain."""
    direct = set(direct_targets(source_ext))
    reachable: set[str] = set()
    for mid in direct:
        for target in direct_targets(mid):
            if target != source_ext and target not in direct:
                reachable.add(target)
    return sorted(reachable)


def targets_for(source_ext: str) -> list[str]:
    """Every format a source can reach, directly or via one intermediate."""
    return sorted(set(direct_targets(source_ext)) | set(chained_targets(source_ext)))


def matrix() -> dict[str, dict]:
    """The full conversion matrix, for GET /supported."""
    sources = sorted({r.source for r in ROUTES})
    result = {}
    for source in sources:
        direct = direct_targets(source)
        result[source] = {
            "category": CATEGORY_BY_EXT.get(source, "unknown"),
            "targets": targets_for(source),
            "direct": direct,
            "chained": chained_targets(source),
            "tools": sorted({r.tool_key for r in ROUTES if r.source == source}),
        }
    return result
