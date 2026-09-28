"""Media metadata and declared usages. No semantic guessing or network fetches."""
import hashlib
import posixpath
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from lxml import etree
from PIL import Image, UnidentifiedImageError

REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = {"a": A, "p": P}
SHAPES = {f"{{{P}}}{name}" for name in ("sp", "pic", "grpSp", "cxnSp", "graphicFrame")}


def xml_root(blob):
    return etree.fromstring(blob, etree.XMLParser(resolve_entities=False, no_network=True))


def extract_media(archive: ZipFile, output: Path):
    """Keep the legacy part→file map and add a unique SHA-256 catalog."""
    (output / "assets").mkdir(exist_ok=True)
    (output / "asset_previews").mkdir(exist_ok=True)
    parts, catalog = {}, {}
    for name in sorted(archive.namelist()):
        if not name.startswith("ppt/media/") or name.endswith("/"):
            continue
        blob = archive.read(name)
        digest = hashlib.sha256(blob).hexdigest()
        if digest not in catalog:
            extension = Path(name).suffix.lower()
            relative = f"assets/{digest}{extension}"
            (output / relative).write_bytes(blob)
            entry = {"asset_id": digest, "sha256": digest, "path": relative,
                     "size_bytes": len(blob), "extension": extension,
                     "format": None, "width_px": None, "height_px": None,
                     "has_alpha_channel": None, "has_transparency": None,
                     "preview_path": None, "preview_status": "unsupported",
                     "source_parts": [], "description": None, "usages": []}
            try:
                with Image.open(BytesIO(blob)) as image:
                    entry.update(format=image.format, width_px=image.width, height_px=image.height,
                                 has_alpha_channel="A" in image.getbands())
                    rgba = image.convert("RGBA")
                    entry["has_transparency"] = rgba.getchannel("A").getextrema()[0] < 255
                    rgba.thumbnail((256, 256), Image.Resampling.LANCZOS)
                    preview = f"asset_previews/{digest}.png"
                    rgba.save(output / preview, "PNG")
                    entry.update(preview_path=preview, preview_status="ready")
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
                entry["preview_error"] = type(exc).__name__
            catalog[digest] = entry
        entry = catalog[digest]
        entry["source_parts"].append(name)
        parts[name] = {"asset_id": digest, "path": entry["path"], "sha256": digest}
    return parts, catalog


def index_usages(archive: ZipFile, manifest: dict):
    """Index XML references, including backgrounds, layouts and slide masters.

    These are declared occurrences, not a claim that every inherited occurrence is
    visible on every slide. Group geometry remains local as in the extraction.
    """
    names = set(archive.namelist())
    scopes, objects = {}, {}
    for kind, collection in (("slide", "slides"), ("layout", "layouts"), ("master", "masters")):
        for record in manifest[collection]:
            part = record.get("part", record.get("source_part")).lstrip("/")
            scopes[part] = (kind, record["id"])
            for obj in record["objects"]:
                objects[(part, obj["shape_id"])] = obj
    external = []
    for part in sorted(names):
        if not part.startswith("ppt/") or not part.endswith(".xml"):
            continue
        rel_path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        if rel_path not in names:
            continue
        rels = {rel.get("Id"): rel for rel in xml_root(archive.read(rel_path))}
        sequence = 0
        for node in xml_root(archive.read(part)).iter():
            for attribute, rid in node.attrib.items():
                if not attribute.startswith("{" + REL_NS + "}") or rid not in rels:
                    continue
                rel = rels[rid]
                if rel.get("TargetMode") == "External":
                    if rel.get("Type", "").rsplit("/", 1)[-1] in {"image", "video", "audio"}:
                        external.append({"source_part": part, "relationship_id": rid,
                                         "target": rel.get("Target"), "status": "not_downloaded"})
                    continue
                target = rel.get("Target", "")
                target = (target.lstrip("/") if target.startswith("/") else
                          posixpath.normpath(posixpath.join(posixpath.dirname(part), target)))
                asset = manifest["assets"].get(target)
                if asset is None:
                    continue
                sequence += 1
                ancestors = [node, *node.iterancestors()]
                owner = next((el for el in ancestors if el.tag in SHAPES), None)
                obj = None
                if owner is not None:
                    identifier = owner.find(".//p:cNvPr", NS)
                    if identifier is not None:
                        obj = objects.get((part, int(identifier.get("id"))))
                background = any(el.tag == f"{{{P}}}bg" for el in ancestors)
                fill = next((el for el in ancestors if etree.QName(el).localname == "blipFill"), None)
                crop = fill.find("a:srcRect", NS) if fill is not None else None
                xfrm = owner.find("p:spPr/a:xfrm", NS) if owner is not None else None
                scope, scope_id = scopes.get(part, ("other", None))
                usage = {
                    "usage_id": f"{part}#media{sequence}", "source_part": part,
                    "scope": scope, "scope_id": scope_id,
                    "slide_id": scope_id if scope == "slide" else None,
                    "object_id": obj["id"] if obj else None,
                    "parent_id": obj["parent_id"] if obj else None,
                    "geometry": obj["geometry"] if obj else None,
                    "usage_type": "background" if background else "picture" if obj and obj["kind"] == "image" else "fill_or_embedded_media",
                    "crop": {side: int(crop.get(side, 0)) / 100000 if crop is not None else 0
                             for side in ("l", "t", "r", "b")},
                    "flip_h": xfrm.get("flipH", "0") in {"1", "true"} if xfrm is not None else False,
                    "flip_v": xfrm.get("flipV", "0") in {"1", "true"} if xfrm is not None else False,
                    "role": None, "description": None, "confidence": None,
                }
                manifest["asset_catalog"][asset["asset_id"]]["usages"].append(usage)
    manifest["external_media"] = external
