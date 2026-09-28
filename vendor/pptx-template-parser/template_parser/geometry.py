"""Transform object rectangles into slide coordinates, including nested groups."""
import math
from lxml import etree

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def transform(points, cx, cy, angle=0, flip_h=False, flip_v=False):
    rad = math.radians(angle)
    c, s = math.cos(rad), math.sin(rad)
    result = []
    for x, y in points:
        x, y = (x - cx) * (-1 if flip_h else 1), (y - cy) * (-1 if flip_v else 1)
        result.append((cx + c*x - s*y, cy + s*x + c*y))
    return result


def object_polygon(obj, objects):
    g = obj["geometry"]
    x, y, w, h = (g.get(k) for k in ("x", "y", "width", "height"))
    if any(v is None for v in (x, y, w, h)):
        raise ValueError(f"Missing geometry: {obj['id']}")
    points = transform([(x,y), (x+w,y), (x+w,y+h), (x,y+h)], x+w/2, y+h/2, g.get("rotation", 0))
    parent = obj.get("parent_id")
    seen = {obj["id"]}
    while parent:
        if parent in seen or parent not in objects:
            raise ValueError("Invalid group ancestry")
        seen.add(parent)
        group = objects[parent]
        root = etree.fromstring(group["xml"].encode(), etree.XMLParser(resolve_entities=False, no_network=True))
        xf = root.find("p:grpSpPr/a:xfrm", NS)
        if xf is None:
            raise ValueError(f"Missing group transform: {parent}")
        def pair(tag, first, second):
            el = xf.find("a:"+tag, NS)
            if el is None:
                raise ValueError(f"Incomplete group transform: {parent}")
            return float(el.get(first)), float(el.get(second))
        ox, oy = pair("off", "x", "y")
        ew, eh = pair("ext", "cx", "cy")
        cx, cy = pair("chOff", "x", "y")
        cw, ch = pair("chExt", "cx", "cy")
        if cw == 0 or ch == 0:
            raise ValueError(f"Zero group extent: {parent}")
        points = [(ox+(px-cx)*ew/cw, oy+(py-cy)*eh/ch) for px, py in points]
        points = transform(points, ox+ew/2, oy+eh/2, float(xf.get("rot", 0))/60000,
                           xf.get("flipH") in {"1", "true"}, xf.get("flipV") in {"1", "true"})
        parent = group.get("parent_id")
    return points
