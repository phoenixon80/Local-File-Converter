"""File type detection by content, not by extension.

The primary detector is a pure-Python magic-signature sniffer (no native deps).
libmagic is used *only* when a subprocess probe proves it can be imported
safely: on Windows without a libmagic DLL, `import magic` aborts the
interpreter at the loader level, which no try/except can catch and which would
take the server down at startup. See probe_libmagic().
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- categories

CATEGORY_BY_EXT: dict[str, str] = {}


def _cat(category: str, *exts: str) -> None:
    for e in exts:
        CATEGORY_BY_EXT[e] = category


_cat("image", "png", "jpg", "gif", "webp", "bmp", "tiff", "heic", "avif", "ico", "svg")
_cat("video", "mp4", "mov", "webm", "avi", "mkv")
_cat("audio", "mp3", "wav", "flac", "ogg", "m4a")
_cat("document", "md", "html", "txt", "csv", "rst", "tex", "rtf")
_cat("office", "docx", "xlsx", "pptx", "odt", "ods", "odp", "pdf")
_cat("ebook", "epub", "mobi", "azw3", "fb2")


@dataclass(frozen=True)
class Detected:
    ext: str          # normalized extension, e.g. "png"
    category: str     # image|video|audio|document|office|ebook|unknown
    mime: str
    source: str       # "content" | "extension" | "libmagic"
    claimed_ext: str  # what the filename claimed
    mismatch: bool    # content type disagrees with the filename

    @property
    def description(self) -> str:
        return f"{self.ext.upper()} ({self.category})"


# ------------------------------------------------------------ signature table
# (offset, magic bytes, extension, mime)
_SIGNATURES: list[tuple[int, bytes, str, str]] = [
    (0, b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (0, b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (0, b"GIF87a", "gif", "image/gif"),
    (0, b"GIF89a", "gif", "image/gif"),
    (0, b"BM", "bmp", "image/bmp"),
    (0, b"II*\x00", "tiff", "image/tiff"),
    (0, b"MM\x00*", "tiff", "image/tiff"),
    (0, b"\x00\x00\x01\x00", "ico", "image/x-icon"),
    (0, b"%PDF-", "pdf", "application/pdf"),
    (0, b"{\\rtf", "rtf", "application/rtf"),
    (0, b"OggS", "ogg", "audio/ogg"),
    (0, b"fLaC", "flac", "audio/flac"),
    (0, b"ID3", "mp3", "audio/mpeg"),
    (0, b"BOOKMOBI", "mobi", "application/x-mobipocket-ebook"),
]


def _read_head(path: Path, n: int = 4096) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def _sniff_riff(head: bytes) -> tuple[str, str] | None:
    """RIFF containers: WAV, WEBP and AVI all share the 'RIFF' magic."""
    if head[:4] != b"RIFF":
        return None
    form = head[8:12]
    if form == b"WAVE":
        return "wav", "audio/x-wav"
    if form == b"WEBP":
        return "webp", "image/webp"
    if form == b"AVI ":
        return "avi", "video/x-msvideo"
    return None


_ISOBMFF_BRANDS = {
    "qt": ("mov", "video/quicktime"),
    "avif": ("avif", "image/avif"),
    "avis": ("avif", "image/avif"),
    "heic": ("heic", "image/heic"),
    "heix": ("heic", "image/heic"),
    "hevc": ("heic", "image/heic"),
    "hevx": ("heic", "image/heic"),
    "m4a": ("m4a", "audio/mp4"),
}


def _sniff_isobmff(head: bytes) -> tuple[str, str] | None:
    """ISO base media: MP4, MOV, M4A, HEIC and AVIF all use an 'ftyp' box.

    The major brand alone is not enough. Still-image files often declare the
    generic brand 'mif1' or 'msf1' and name the real format only in the
    compatible-brands list that follows, so an AVIF or HEIC would otherwise
    fall through to the MP4 default and be handed to a video tool.
    """
    if head[4:8] != b"ftyp":
        return None
    major = head[8:12].decode("ascii", "ignore").strip().lower()
    if major in _ISOBMFF_BRANDS:
        return _ISOBMFF_BRANDS[major]

    # Compatible brands run from offset 16 to the end of the ftyp box.
    box_size = int.from_bytes(head[0:4], "big")
    compatible = head[16:box_size if 16 < box_size <= len(head) else len(head)]
    text = compatible.decode("ascii", "ignore").lower()
    if "avif" in text:
        return "avif", "image/avif"
    if "heic" in text or "heix" in text or "hevc" in text:
        return "heic", "image/heic"
    if major in ("mif1", "msf1"):
        # A still-image container we cannot pin down further; HEIC is the
        # overwhelmingly common case and is at least the right family.
        return "heic", "image/heic"
    return "mp4", "video/mp4"


def _sniff_matroska(head: bytes) -> tuple[str, str] | None:
    """The EBML header is shared by MKV and WEBM; the DocType string separates them."""
    if head[:4] != b"\x1a\x45\xdf\xa3":
        return None
    if b"webm" in head[:200]:
        return "webm", "video/webm"
    return "mkv", "video/x-matroska"


_ODF_MIMES = {
    "application/epub+zip": ("epub", "application/epub+zip"),
    "application/vnd.oasis.opendocument.text": (
        "odt", "application/vnd.oasis.opendocument.text"),
    "application/vnd.oasis.opendocument.spreadsheet": (
        "ods", "application/vnd.oasis.opendocument.spreadsheet"),
    "application/vnd.oasis.opendocument.presentation": (
        "odp", "application/vnd.oasis.opendocument.presentation"),
}

_OOXML_MIMES = {
    "word/": ("docx", "application/vnd.openxmlformats-officedocument"
                      ".wordprocessingml.document"),
    "xl/": ("xlsx", "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
    "ppt/": ("pptx", "application/vnd.openxmlformats-officedocument"
                     ".presentationml.presentation"),
}


def _sniff_zip_container(path: Path) -> tuple[str, str] | None:
    """OOXML, OpenDocument and EPUB are all ZIP archives; look inside."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            # EPUB and ODF declare themselves in a "mimetype" entry.
            if "mimetype" in names:
                try:
                    declared = zf.read("mimetype").decode("ascii", "ignore").strip()
                except Exception:
                    declared = ""
                if declared in _ODF_MIMES:
                    return _ODF_MIMES[declared]
            for prefix, result in _OOXML_MIMES.items():
                if any(n.startswith(prefix) for n in names):
                    return result
            return "zip", "application/zip"
    except (zipfile.BadZipFile, OSError):
        return None


def _sniff_mpeg_audio(head: bytes) -> tuple[str, str] | None:
    """MP3 frames with no ID3 tag: 11 sync bits at the start of the stream."""
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3", "audio/mpeg"
    return None


_TEXT_EXTS = {"md", "html", "txt", "csv", "rst", "tex", "svg", "json", "xml"}
_TEXT_MIMES = {"md": "text/markdown", "html": "text/html",
               "csv": "text/csv", "svg": "image/svg+xml"}


def _sniff_text(head: bytes, claimed_ext: str) -> tuple[str, str] | None:
    """Text formats carry no magic bytes: verify it decodes, then trust the extension."""
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    lowered = text.lstrip()[:512].lower()
    if "<svg" in text[:2048].lower():
        return "svg", "image/svg+xml"
    if lowered.startswith(("<!doctype html", "<html")):
        return "html", "text/html"
    if claimed_ext in _TEXT_EXTS:
        return claimed_ext, _TEXT_MIMES.get(claimed_ext, "text/plain")
    return "txt", "text/plain"


def sniff(path: Path, claimed_ext: str = "") -> tuple[str, str] | None:
    """Identify a file from its content. Returns (ext, mime), or None if unknown."""
    head = _read_head(path)
    if not head:
        return None
    for sniffer in (_sniff_riff, _sniff_isobmff, _sniff_matroska):
        hit = sniffer(head)
        if hit:
            return hit
    if head[:2] == b"PK":
        hit = _sniff_zip_container(path)
        if hit:
            return hit
    for offset, magic, ext, mime in _SIGNATURES:
        if head[offset:offset + len(magic)] == magic:
            return ext, mime
    hit = _sniff_mpeg_audio(head)
    if hit:
        return hit
    return _sniff_text(head, claimed_ext)


# ------------------------------------------------------------------ libmagic

_LIBMAGIC_OK: bool | None = None
_PROBE = "import magic; magic.from_buffer(b'abc', mime=True)"


def probe_libmagic() -> bool:
    """Is `import magic` both safe and functional in this interpreter?

    Runs out-of-process on purpose: a missing libmagic DLL aborts the process
    at the loader level, and no try/except can catch that. Probing in a
    subprocess keeps the abort away from the server. Cached per process.
    """
    global _LIBMAGIC_OK
    if _LIBMAGIC_OK is not None:
        return _LIBMAGIC_OK
    try:
        proc = subprocess.run([sys.executable, "-c", _PROBE],
                              capture_output=True, timeout=15)
        _LIBMAGIC_OK = proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        _LIBMAGIC_OK = False
    return _LIBMAGIC_OK


_MIME_TO_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/webp": "webp", "image/bmp": "bmp", "image/tiff": "tiff",
    "application/pdf": "pdf", "video/mp4": "mp4", "video/quicktime": "mov",
    "video/webm": "webm", "audio/mpeg": "mp3", "audio/x-wav": "wav",
    "application/epub+zip": "epub",
}

_GENERIC_MIMES = {"application/octet-stream", "text/plain", "application/zip", ""}


def _libmagic_mime(path: Path) -> str | None:
    if not probe_libmagic():
        return None
    try:
        import magic  # safe: the probe proved this import does not abort
        return magic.from_file(str(path), mime=True)
    except Exception:
        return None


# -------------------------------------------------------------------- public

_EXT_ALIASES = {"jpeg": "jpg", "tif": "tiff", "htm": "html", "markdown": "md"}
_EQUIVALENT = {("jpg", "jpeg"), ("tiff", "tif"), ("html", "htm"), ("md", "markdown")}
_ZIP_BASED = ("docx", "xlsx", "pptx", "odt", "ods", "odp", "epub")


def detect(path: Path, filename: str) -> Detected:
    """Detect a file's real type. Content wins; the extension is only a fallback."""
    claimed = Path(filename).suffix.lower().lstrip(".")
    claimed = _EXT_ALIASES.get(claimed, claimed)

    ext = mime = ""
    source = "extension"

    hit = sniff(path, claimed)
    if hit:
        ext, mime = hit
        source = "content"

    # libmagic is consulted only when the built-in sniffer came up empty.
    if not ext:
        lm = _libmagic_mime(path)
        if lm and lm not in _GENERIC_MIMES and lm in _MIME_TO_EXT:
            ext, mime, source = _MIME_TO_EXT[lm], lm, "libmagic"

    if not ext:
        ext, mime, source = claimed, "application/octet-stream", "extension"

    # A ZIP whose inner layout we could not name keeps a plausible claimed ext.
    if ext == "zip" and claimed in _ZIP_BASED:
        ext = claimed

    category = CATEGORY_BY_EXT.get(ext, "unknown")
    mismatch = bool(
        claimed and ext and claimed != ext
        and (ext, claimed) not in _EQUIVALENT
        and (claimed, ext) not in _EQUIVALENT
    )
    return Detected(ext=ext, category=category,
                    mime=mime or "application/octet-stream",
                    source=source, claimed_ext=claimed, mismatch=mismatch)
