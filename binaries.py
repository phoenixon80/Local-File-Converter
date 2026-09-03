"""Locating the external conversion tools.

Every handler resolves its binary through here so that "is the tool present?"
has exactly one answer, shared by /health and by the handlers themselves.
"""
from __future__ import annotations

import glob
import os
import shutil
import sys
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

# Extra places to look when a tool installs itself without putting the binary
# on PATH, which is the norm for the Windows installers of these projects.
# Entries are glob patterns: these installers bake the version into the
# directory name, so matching a literal path would break on every update.
_WINDOWS_HINTS: dict[str, tuple[str, ...]] = {
    "magick": (
        r"C:\Program Files\ImageMagick-*\magick.exe",
        r"C:\Program Files (x86)\ImageMagick-*\magick.exe",
    ),
    "soffice": (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ),
    "ebook-convert": (
        r"C:\Program Files\Calibre*\ebook-convert.exe",
        r"C:\Program Files (x86)\Calibre*\ebook-convert.exe",
    ),
    "pandoc": (
        r"C:\Program Files\Pandoc\pandoc.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Pandoc\pandoc.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\pandoc.exe"),
    ),
    "ffmpeg": (
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    ),
}


@dataclass(frozen=True)
class Tool:
    key: str            # internal name used by handlers, e.g. "imagemagick"
    display: str        # human name for the UI
    commands: tuple[str, ...]  # candidate executables, in preference order
    handles: str        # what breaks without it, for the missing-tool banner
    install_hint: str


TOOLS: tuple[Tool, ...] = (
    Tool("imagemagick", "ImageMagick", ("magick", "convert"),
         "image conversions (png, jpg, webp, bmp, tiff)",
         "winget install ImageMagick.ImageMagick"),
    Tool("ffmpeg", "FFmpeg", ("ffmpeg",),
         "video and audio conversions (mp4, mov, webm, mp3, wav, gif)",
         "winget install Gyan.FFmpeg"),
    Tool("pandoc", "Pandoc", ("pandoc",),
         "markup conversions (md, html, docx, epub)",
         "winget install JohnMacFarlane.Pandoc"),
    Tool("libreoffice", "LibreOffice", ("soffice",),
         "office document conversions (docx, xlsx, pptx, odt, pdf)",
         "winget install TheDocumentFoundation.LibreOffice"),
    Tool("calibre", "Calibre", ("ebook-convert",),
         "ebook conversions (epub, mobi, azw3)",
         "winget install calibre.calibre"),
)

TOOLS_BY_KEY = {t.key: t for t in TOOLS}


def _is_windows_convert_trap(path: str, command: str) -> bool:
    """Windows ships its own convert.exe (the NTFS volume converter) in
    System32. It is not ImageMagick, and running it on a file would be both
    useless and alarming, so never accept it as an ImageMagick binary."""
    if not IS_WINDOWS or command != "convert":
        return False
    return "system32" in path.lower().replace("/", "\\")


def resolve(key: str) -> str | None:
    """Absolute path to the executable for a tool key, or None if not installed."""
    tool = TOOLS_BY_KEY[key]
    for command in tool.commands:
        found = shutil.which(command)
        if found and not _is_windows_convert_trap(found, command):
            return found
    if IS_WINDOWS:
        for pattern in _WINDOWS_HINTS.get(tool.commands[0], ()):
            # Newest match last alphabetically is the best guess for versioned
            # install directories (ImageMagick-7.1.2-29 beats -7.1.1-0).
            matches = sorted(p for p in glob.glob(pattern) if os.path.isfile(p))
            if matches:
                return matches[-1]
    return None


def require(key: str) -> str:
    """Resolve a tool or raise a message aimed at the user, not at a log file."""
    found = resolve(key)
    if found is None:
        tool = TOOLS_BY_KEY[key]
        raise FileNotFoundError(
            f"{tool.display} is not installed, so {tool.handles} are unavailable. "
            f"Install it with: {tool.install_hint}"
        )
    return found


def health() -> dict[str, dict]:
    """Presence of every tool, for GET /health."""
    report = {}
    for tool in TOOLS:
        path = resolve(tool.key)
        report[tool.key] = {
            "display": tool.display,
            "present": path is not None,
            "path": path,
            "handles": tool.handles,
            "install_hint": tool.install_hint,
        }
    return report
