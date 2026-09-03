# Local File Converter

Drag a file into a browser tab, get it back in another format. Everything runs
on `localhost` and nothing is uploaded anywhere.

This app is a **router**, not a converter. It identifies what a file actually
is, picks the right tool for the job, runs it, and hands back the result. The
conversion work itself is done by FFmpeg, ImageMagick, Pandoc, LibreOffice and
Calibre.

---

## Quick start

```powershell
.\run.ps1
```

Then open <http://127.0.0.1:8000>. On macOS or Linux use `./run.sh`.

The script creates a virtualenv and installs dependencies on first run. To do
it by hand:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m uvicorn main:app --port 8000
```

## The five external tools

The app checks for these on startup and at `GET /health`; the page shows a
banner naming anything missing. Nothing crashes when a tool is absent —
conversions that need it are refused with an explanation and an install hint.

| Tool | Command | Handles | Install (Windows) |
|---|---|---|---|
| ImageMagick | `magick` | images | `winget install ImageMagick.ImageMagick` |
| FFmpeg | `ffmpeg` | video, audio | `winget install Gyan.FFmpeg` |
| Pandoc | `pandoc` | markup | `winget install JohnMacFarlane.Pandoc` |
| LibreOffice | `soffice` | office documents | `winget install TheDocumentFoundation.LibreOffice` |
| Calibre | `ebook-convert` | ebooks | `winget install calibre.calibre` |

On macOS: `brew install imagemagick ffmpeg pandoc`, plus the LibreOffice and
Calibre casks.

### A Windows trap worth knowing about

Windows ships its own `convert.exe` in `System32` — it converts FAT volumes to
NTFS and has nothing to do with ImageMagick. Resolving ImageMagick by the name
`convert`, which is the usual advice on Linux, finds that instead. `binaries.py`
prefers `magick` and explicitly refuses any `convert` that resolves inside
`System32`. On non-Windows platforms `convert` is still accepted as the
ImageMagick v6 name.

## Supported conversions

192 routes. `GET /supported` returns the authoritative matrix; this is the
shape of it.

| Family | Sources | Targets |
|---|---|---|
| Images | png, jpg, webp, bmp, tiff, gif, avif, heic*, svg*, ico | png, jpg, webp, bmp, tiff, gif, avif, ico, pdf |
| Video | mp4, mov, webm, avi, mkv | mp4, mov, webm, mkv, gif, and any audio target |
| Audio | mp3, wav, flac, ogg, m4a | mp3, wav, flac, ogg |
| Markup | md, html, rst, txt, docx, epub | md, html, rst, txt, docx, epub |
| Office | docx, odt, rtf, xlsx, ods, csv, pptx, odp | the rest of their own family, plus pdf |
| Ebooks | epub, mobi, azw3, fb2 | epub, mobi, azw3, fb2, pdf |

`*` input only. Anything not directly convertible is still reachable if one
intermediate format bridges it — Markdown to PDF, EPUB to ODT, a video frame to
PNG — and the picker labels those as two-step.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MAX_UPLOAD_MB` | `500` | Uploads above this are rejected before any process starts |
| `TEMP_MAX_AGE_SECONDS` | `3600` | How long finished work is kept in `temp/` |
| `TIMEOUT_IMAGEMAGICK` | `120` | Per-tool subprocess timeout, in seconds |
| `TIMEOUT_FFMPEG` | `600` | |
| `TIMEOUT_PANDOC` | `180` | |
| `TIMEOUT_LIBREOFFICE` | `300` | |
| `TIMEOUT_CALIBRE` | `300` | |

Port is a command-line argument: `.\run.ps1 -Port 9000`.

## API

| Endpoint | Purpose |
|---|---|
| `GET /health` | Which tools are installed, which are missing, and the upload cap |
| `GET /supported` | The full conversion matrix, split into direct and two-step routes |
| `POST /convert` | Multipart `file`, optional `target_format`. Without a target it only identifies the file and reports what it can become. With one it starts a job and returns a `job_id` |
| `GET /status/{job_id}` | `queued` / `running` / `done` / `failed`, plus progress, stage, and error text |
| `GET /download/{job_id}` | The converted file, once the job is `done` |

```bash
# identify a file and see what it can become
curl -F "file=@photo.png" http://127.0.0.1:8000/convert

# start a conversion
curl -F "file=@photo.png" -F "target_format=webp" http://127.0.0.1:8000/convert
curl http://127.0.0.1:8000/status/<job_id>
curl -o out.webp http://127.0.0.1:8000/download/<job_id>
```

## How it decides what a file is

`detect.py` reads the file's leading bytes and identifies it from its actual
content. The extension is only a fallback for formats that have no signature at
all (Markdown, CSV, plain text). Renaming `notes.txt` to `movie.mp4` does not
fool it, and the UI says so rather than handing a text file to FFmpeg.

Container formats that share a magic number are disambiguated properly: `RIFF`
splits into WAV / WEBP / AVI, an ISO-BMFF `ftyp` box into MP4 / MOV / M4A /
HEIC, EBML into MKV / WEBM, and a ZIP is opened to tell DOCX from XLSX from
PPTX from EPUB from ODT.

### Why not python-magic

The spec called for `python-magic`. It is supported but optional, and it is not
the primary detector, for a concrete reason: `python-magic` is a binding to
libmagic, and on Windows without a libmagic DLL `import magic` does not raise
an `ImportError` — it aborts the interpreter at the DLL loader, which no
`try`/`except` can catch. Making the server depend on that import means the
server dies at startup on any machine missing the DLL.

So `detect.py` implements the signature sniffing directly, and consults
libmagic only when a subprocess probe has proved the import is safe *and*
functional. Install `python-magic` alongside a real libmagic and it gets picked
up automatically as a fallback for anything the built-in table does not know.

## Conversion routing

`registry.py` holds a declarative table of `(source, target) -> handler`.
Adding a format pair means adding it to a list; no routing code changes.

Two guarantees the table enforces:

- **No silent duplicates.** If two tools claimed the same pair, which one ran
  would depend on table order. Building the route map raises instead.
- **Two-step chaining, capped at two.** When no tool converts a pair directly,
  the router looks for one intermediate format that bridges it — Markdown to
  PDF goes `md → docx` (Pandoc) then `docx → pdf` (LibreOffice). Where several
  intermediates would work, it prefers the one that loses the least: PNG or
  TIFF over JPEG for images, WAV or FLAC over MP3 for audio. Anything needing
  three hops is refused with a clear message rather than silently producing
  something degraded.

The format picker groups two-step routes under their own heading, so it is
always visible when a conversion is passing through an intermediate.

## Handler rules

Every handler in `handlers/` follows the same contract:

- Arguments are built as a list. `shell=True` appears nowhere in the codebase.
- The output path is always explicit.
- stdout and stderr are captured; a non-zero exit becomes a `ConversionError`
  carrying a one-line summary for the UI and the full output for the details
  pane.
- Every subprocess call has a timeout — 120s for images, 180s for Pandoc, 300s
  for LibreOffice and Calibre, 600s for FFmpeg, since a video transcode is
  legitimately slower than a PNG resize.
- A zero exit code is not taken as proof of success: the output file must exist
  and be non-empty. Several of these tools will happily report success and
  write nothing.

Children are also tied to the server's lifetime. On a clean shutdown the
server kills whatever is still running. For the unclean case, every child is
placed in a Windows job object created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
(`process_group.py`): the job's only handle belongs to the server process, so
when that process dies — including under `taskkill /F`, where no application
code runs at all — the kernel closes the handle and kills everything inside.
On POSIX children get their own process group, which covers clean shutdown but
cannot survive a `kill -9` of the parent.

Two tool-specific details:

- **LibreOffice** cannot be told what to name its output; it writes
  `<input-stem>.<ext>` into `--outdir`. The handler converts into a private
  directory and moves the result to the exact requested path. It also passes
  `-env:UserInstallation=<per-job profile>`, because concurrent headless
  invocations otherwise collide over the shared user profile and one of them
  silently produces nothing.
- **ImageMagick 7** is invoked as `magick in out`. Routing through the v6
  compatibility shim (`magick convert`) still works but prints a deprecation
  warning to stderr on every single run, which then gets mistaken for the
  error message when something actually fails.

## Jobs and temp files

Jobs live in an in-memory dict behind a lock — right-sized for a single-user
local tool, and deliberately not durable. Each job gets its own directory under
`temp/`, so concurrent jobs cannot collide over intermediate filenames.

Because files outlive the process and the job store does not, `temp/` is swept
on startup as well as every ten minutes; anything older than an hour goes.
Restarting mid-job is safe: the job is gone, its files are cleaned up on the
way back in, and nothing is left half-written.

## Project layout

```
converter-app/
  main.py                       FastAPI app and routes
  registry.py                   conversion table, chaining, route lookup
  detect.py                     content-based file type detection
  binaries.py                   locating the five external tools
  process_group.py              tying child processes to the server's lifetime
  jobs.py                       job store and temp-file lifecycle
  handlers/
    base.py                     shared subprocess rules
    imagemagick_handler.py
    ffmpeg_handler.py
    pandoc_handler.py
    libreoffice_handler.py
    calibre_handler.py
  static/                       index.html, app.js, style.css
  temp/                         uploads and outputs, swept hourly
  run.ps1 / run.sh
```

`binaries.py` and `process_group.py` are the two modules not in the original
spec's layout. Tool resolution needed a single home once both `/health` and the
handlers had to agree on whether a tool exists; process supervision needed one
once the answer turned out to be platform-specific.

## What has been verified

All 192 declared routes were run end to end through the live HTTP API — upload,
detect, convert, poll, download — with the downloaded bytes checked against the
expected magic number for each target format, four jobs at a time. 192/192
pass, with nothing skipped.

The HEIC sources needed a fixture none of the five tools can produce (they all
read HEIC; none writes it). One was generated out-of-band with `pillow-heif`,
which bundles a HEIF encoder:

```bash
pip install pillow pillow-heif     # test-time only, not an app dependency
python -c "from PIL import Image; import pillow_heif; pillow_heif.register_heif_opener(); Image.new('RGB',(160,120),'blue').save('sample.heic', format='HEIF')"
```

Also checked:

- Two-hop chains produce real output: `md → pdf` (via DOCX), `md → mobi` (via
  EPUB), `docx → azw3`, `epub → odt`.
- A text file renamed `.png` and one renamed `.mp4` are both identified as text
  and never handed to ImageMagick or FFmpeg.
- A corrupted PNG is rejected with ImageMagick's actual complaint, not a
  deprecation warning that happened to be printed first.
- An unsupported pair returns HTTP 400 listing what that source *can* become.
- Uploads over the cap get HTTP 413, and the partial file is removed.
- A conversion that exceeds its timeout is killed, the job is marked failed
  with "Conversion timed out", and no orphaned process is left behind.
- Server shutdown kills in-flight conversions; the job thread unwinds as a
  failure rather than hanging.
- **`taskkill /F` on the server mid-transcode leaves no orphan.** Verified by
  starting a 25-second transcode, force-killing the server, and confirming the
  ffmpeg process count went back to zero.
- FFmpeg jobs report real progress — a 25-second transcode stepped
  5 → 17 → 30 → 42 → 60 → 71 → 83 → 100%.
- Restarting sweeps stale temp files (197 leftover entries removed on the way
  up, `.gitkeep` preserved) and conversions work immediately afterwards.
- Filenames with Windows-illegal characters, reserved device names (`con.png`),
  path traversal (`../../escape.png`) and 300-character names all convert.
- Five files dropped at once convert independently, and one failing does not
  block the other four.

## Limitations

- **PDF is write-only.** Converting *to* PDF works from images, office
  documents and ebooks. Converting *from* PDF is not offered: rasterising a PDF
  needs Ghostscript, which is a separate install this app does not assume.
- **HEIC is input-only.** ImageMagick reports HEIC as read-only (`r--`) via its
  libheif delegate, so HEIC is offered as a source and never as a target. AVIF
  goes through the same delegate and is read/write, so it works both ways.
- **Pandoc cannot write PDF directly** without a LaTeX engine. Markdown to PDF
  is routed through DOCX and LibreOffice instead, and if you ask Pandoc for a
  PDF in a way that reaches it directly, the error explains that rather than
  repeating Pandoc's `pdflatex not found`.
- **Only FFmpeg reports real progress.** Video and audio jobs show a true
  percentage, read from `ffmpeg -progress` against the duration `ffprobe`
  reports. ImageMagick, Pandoc, LibreOffice and Calibre expose nothing
  comparable, so their jobs show stage transitions and a moving bar rather than
  a number that would be invented. The same fallback applies to a video whose
  duration cannot be read — a stream-recorded WEBM, for instance, often carries
  no duration in its header, and there is then nothing honest to divide by.
- Chains are capped at two hops by design.
