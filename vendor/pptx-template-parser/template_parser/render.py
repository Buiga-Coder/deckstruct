"""PowerPoint rendering and verification. Does not modify the source presentation."""
import json
import os
import shutil
import subprocess
import uuid
import time
import signal
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .extract import sha256, write_json


def validate_previews(folder: Path, slides: list, width: int, height: int) -> list:
    expected = {f"{slide['id']}.png" for slide in slides}
    if {p.name for p in folder.glob("*.png")} != expected:
        raise ValueError("Rendered slide count or filenames do not match the template")
    result = []
    for slide in slides:
        file = folder / f"{slide['id']}.png"
        with Image.open(file) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ValueError(f"Invalid dimensions or format: {file.name}")
            image.verify()
        result.append({"slide_id": slide["id"], "index": slide["index"],
                       "path": f"previews/{file.name}", "width_px": width, "height_px": height,
                       "sha256": sha256(file.read_bytes())})
    return result


def _powerpoint(source: Path, destination: Path, width: int, height: int, timeout: int):
    if os.name != "nt":
        raise RuntimeError("PowerPoint rendering requires Windows and desktop Microsoft PowerPoint")
    executable = shutil.which("powershell.exe")
    if not executable:
        raise RuntimeError("Windows PowerShell was not found")
    script = Path(__file__).with_name("scripts") / "render_powerpoint.ps1"
    command = [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(script), "-Source", str(source), "-Destination", str(destination),
               "-Width", str(width), "-Height", str(height)]
    try:
        completed = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                                   timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PowerPoint render timed out. No successful render was published; check PowerPoint for a pending dialog.") from exc
    if completed.returncode:
        raise RuntimeError("PowerPoint rendering failed. Check desktop PowerPoint installation/activation. "
                           + (completed.stderr or completed.stdout)[-2500:])
    return json.loads((destination / "engine.json").read_text(encoding="utf-8-sig"))


def _libreoffice(source, destination, width, height, timeout):
    office = shutil.which('soffice') or shutil.which('libreoffice')
    raster = shutil.which('pdftoppm')
    if not office or not raster:
        raise RuntimeError('Install libreoffice-impress and poppler-utils for LibreOffice rendering')
    work = destination/'conversion'
    work.mkdir()
    deadline = time.monotonic() + timeout
    def command(args):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise RuntimeError('LibreOffice rendering timed out')
        try:
            if os.name == 'nt':
                result = subprocess.run(args, capture_output=True, timeout=remaining)
            else:
                process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    stdout, stderr = process.communicate(timeout=remaining)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
                result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('LibreOffice rendering timed out') from exc
        if result.returncode:
            raise RuntimeError('Rendering subprocess failed: ' + Path(args[0]).name)
        return result
    try:
        profile = work/'profile'
        # Isolated profile avoids attaching to an existing desktop instance.
        pdf_filter = 'pdf:impress_pdf_Export:' + json.dumps({
            'ExportHiddenSlides': {'type':'boolean','value':'true'},
            'ExportNotesPages': {'type':'boolean','value':'false'}})
        command([office, '-env:UserInstallation='+profile.as_uri(), '--headless',
                 '--convert-to', pdf_filter, '--outdir', str(work), str(source)])
        pdf = work/(source.stem+'.pdf')
        if not pdf.is_file():
            raise RuntimeError('LibreOffice did not produce a PDF')
        command([raster, '-png', '-scale-to-x', str(width), '-scale-to-y', str(height),
                 str(pdf), str(work/'page')])
        pages = sorted(work.glob('page-*.png'), key=lambda p: int(p.stem.split('-')[-1]))
        if not pages:
            raise RuntimeError('PDF produced no PNG pages')
        for i, page in enumerate(pages, 1):
            if int(page.stem.split('-')[-1]) != i:
                raise ValueError('Nonsequential PDF pages')
            page.replace(destination/f's{i}.png')
        return {'name':'LibreOffice + Poppler', 'slide_count':len(pages)}
    finally:
        if work.resolve().parent == destination.resolve():
            shutil.rmtree(work)


def render_package(package: Path, width: int = 1600, timeout: int = 300, renderer: str = 'auto') -> dict:
    package = package.resolve()
    if not 256 <= width <= 4096 or timeout <= 0:
        raise ValueError("Width must be 256..4096 pixels and timeout must be positive")
    source = package / "source.pptx"
    manifest = json.loads((package / "template.json").read_text(encoding="utf-8"))
    if sha256(source.read_bytes()) != manifest["source"]["sha256"]:
        raise ValueError("source.pptx does not match template.json; extract a fresh package")
    slides = manifest["slides"]
    if not slides or any(slide["id"] != f"s{i}" or slide["index"] != i for i, slide in enumerate(slides, 1)):
        raise ValueError("Expected nonempty sequential slide IDs and indices")
    size = manifest["slide_size"]
    if size["width"] <= 0 or size["height"] <= 0:
        raise ValueError("Invalid slide dimensions")
    height = max(1, round(width * size["height"] / size["width"]))
    if height > 8192:
        raise ValueError("Rendered height exceeds 8192 pixels; use a smaller width")
    if (package / "previews").exists() or (package / "render_manifest.json").exists():
        raise ValueError("Render output already exists; use a fresh extracted package")
    staging = package / (".render-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        selected = ('powerpoint' if os.name == 'nt' else 'libreoffice') if renderer == 'auto' else renderer
        if selected not in ('powerpoint', 'libreoffice'):
            raise ValueError('renderer must be auto, powerpoint or libreoffice')
        engine = (_powerpoint if selected == 'powerpoint' else _libreoffice)(source, staging, width, height, timeout)
        previews = validate_previews(staging, slides, width, height)
        if engine["slide_count"] != len(slides):
            raise ValueError("Rendered slide count differs from extracted structure")
        if sha256(source.read_bytes()) != manifest["source"]["sha256"]:
            raise ValueError("Source changed during rendering")
        report = {"schema_version": "0.1", "source_sha256": manifest["source"]["sha256"],
                  "created_at": datetime.now(timezone.utc).isoformat(), "engine": engine,
                  "slides": previews, "warnings": ["Font substitution is not automatically detected; review initial renders visually."]}
        # Publish only a complete validated batch. Incomplete batches remain hidden.
        (staging / "engine.json").unlink(missing_ok=True)
        write_json(staging / "render_manifest.json", report)
        staging.rename(package / "previews")
        (package / "previews" / "render_manifest.json").replace(package / "render_manifest.json")
        return report
    finally:
        if staging.exists() and staging.resolve().parent == package and not staging.is_symlink():
            shutil.rmtree(staging)
