"""Extract facts; never infer semantic roles from filenames or slide numbers."""
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

from lxml import etree
from pptx import Presentation
from .assets import extract_media, index_usages


NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_template(source: Path, output: Path) -> dict:
    source = source.resolve()
    if output.exists():
        raise ValueError("Output directory already exists; choose a new directory")
    raw = source.read_bytes()
    # Parse before creating output so invalid input leaves no partial package.
    prs = Presentation(source)
    output.mkdir(parents=True)
    (output / "source.pptx").write_bytes(raw)
    with ZipFile(source) as archive:
        assets, asset_catalog = extract_media(archive, output)

    def part_shapes(part, prefix):
        result = []

        def visit(shapes, parent=None):
            for order, shape in enumerate(shapes):
                element = shape._element
                local = etree.QName(element).localname
                kind = ("group" if local == "grpSp" else "table" if shape.has_table
                        else "chart" if shape.has_chart else "image" if local == "pic"
                        else "connector" if local == "cxnSp" else "shape" if local == "sp"
                        else "unknown")
                oid = f"{prefix}_o{shape.shape_id}"
                item = {
                    "id": oid, "shape_id": shape.shape_id, "name": shape.name,
                    "kind": kind, "parent_id": parent, "z_order": order,
                    "geometry": {"x": shape.left, "y": shape.top, "width": shape.width,
                                 "height": shape.height, "rotation": shape.rotation,
                                 "units": "EMU", "coordinate_space": parent or "slide"},
                    "source_part": str(part.part.partname).lstrip("/"),
                    "xml": etree.tostring(element, encoding="unicode"),
                    "text": None, "paragraphs": [], "assets": [],
                }
                if shape.is_placeholder:
                    item["placeholder"] = {"idx": shape.placeholder_format.idx,
                                           "type": str(shape.placeholder_format.type)}
                if shape.has_text_frame:
                    item["text"] = shape.text_frame.text
                    for paragraph in shape.text_frame.paragraphs:
                        runs = []
                        for run in paragraph.runs:
                            font = run.font
                            runs.append({"text": run.text, "font_name": font.name,
                                         "font_size_pt": font.size.pt if font.size is not None else None,
                                         "bold": font.bold, "italic": font.italic})
                        item["paragraphs"].append({"text": paragraph.text,
                                                   "level": paragraph.level, "runs": runs})
                for blip in element.findall(".//a:blip", NS) if kind != "group" else []:
                    rel_id = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                    if rel_id:
                        rel = part.part.rels[rel_id]
                        if not rel.is_external:
                            target = str(rel.target_part.partname).lstrip("/")
                            item["assets"].append({"source_part": target, **assets.get(target, {})})
                if kind == "table":
                    item["table"] = [[{"text": cell.text, "is_merge_origin": cell.is_merge_origin,
                                        "is_spanned": cell.is_spanned} for cell in row.cells]
                                     for row in shape.table.rows]
                result.append(item)
                if kind == "group":
                    visit(shape.shapes, oid)
        visit(part.shapes)
        return result

    masters, layouts, slides = [], [], []
    for mi, master in enumerate(prs.slide_masters, 1):
        mid = f"master{mi}"
        masters.append({"id": mid, "part": str(master.part.partname),
                        "xml": etree.tostring(master._element, encoding="unicode"),
                        "objects": part_shapes(master, mid)})
        for li, layout in enumerate(master.slide_layouts, 1):
            lid = f"{mid}_layout{li}"
            layouts.append({"id": lid, "master_id": mid, "part": str(layout.part.partname),
                            "name": layout.name, "objects": part_shapes(layout, lid),
                            "xml": etree.tostring(layout._element, encoding="unicode")})
    layout_ids = {layout["part"]: layout["id"] for layout in layouts}
    for index, slide in enumerate(prs.slides, 1):
        sid = f"s{index}"
        slides.append({"id": sid, "index": index, "source_part": str(slide.part.partname),
                       "layout_id": layout_ids[str(slide.slide_layout.part.partname)],
                       "xml": etree.tostring(slide._element, encoding="unicode"),
                       "objects": part_shapes(slide, sid), "semantics": None})
    themes = []
    with ZipFile(source) as archive:
        for name in archive.namelist():
            if name.startswith("ppt/theme/") and name.endswith(".xml"):
                themes.append({"part": name, "xml": archive.read(name).decode("utf-8")})
    manifest = {
        "schema_version": "0.2", "source": {"name": source.name, "sha256": sha256(raw)},
        "slide_size": {"width": prs.slide_width, "height": prs.slide_height, "units": "EMU"},
        "assets": assets, "asset_catalog": asset_catalog,
        "themes": themes, "masters": masters, "layouts": layouts,
        "slides": slides, "diagnostics": [
            {"code": "UNRESOLVED_STYLES", "message": "Raw XML retained; effective inherited styles and design tokens are not yet resolved."},
            {"code": "LOCAL_GROUP_COORDINATES", "message": "Children use group coordinate space; transformations retained in XML."},
            {"code": "SEMANTICS_PENDING", "message": "Run analyze with rendered previews and a configured VLM."}
        ],
    }
    with ZipFile(source) as archive:
        index_usages(archive, manifest)
    write_json(output / "template.json", manifest)
    return manifest
