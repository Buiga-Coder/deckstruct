"""Self-contained HTML review with SVG overlays; no model calls or image edits."""
import base64
import html
import json
from pathlib import Path

from PIL import Image

from .extract import sha256
from .geometry import object_polygon
from .schemas import SlideSemantics, validate_semantics
from .composition import compose_slide


def create_review(package: Path, slide_index: int, output: Path | None = None) -> Path:
    manifest = json.loads((package / "template.json").read_text(encoding="utf-8"))
    if not 1 <= slide_index <= len(manifest["slides"]):
        raise ValueError("Slide index out of range")
    slide = manifest["slides"][slide_index-1]
    sid = slide["id"]
    envelope = json.loads((package / "semantics" / f"{sid}.json").read_text(encoding="utf-8"))
    composed = envelope.get('object_scope') == 'composed_v1'
    if composed:
        slide = compose_slide(manifest, slide)
    preview = (package / "previews" / f"{sid}.png").read_bytes()
    if envelope["source_sha256"] != manifest["source"]["sha256"] or envelope["preview_sha256"] != sha256(preview):
        raise ValueError("Semantics source/preview hash mismatch; review would show a different input")
    result = SlideSemantics.model_validate(envelope["result"])
    validate_semantics(result, slide)
    with Image.open(package / "previews" / f"{sid}.png") as image:
        width, height = image.size
    sx, sy = width / manifest["slide_size"]["width"], height / manifest["slide_size"]["height"]
    objects = {obj["id"]: obj for obj in slide["objects"]}
    polygons, warnings = {}, []
    for oid, obj in objects.items():
        if obj["kind"] == "group":
            continue
        try:
            polygons[oid] = [(x*sx, y*sy) for x, y in object_polygon(obj, objects)]
        except (ValueError, KeyError, TypeError) as exc:
            warnings.append(f"Не показана рамка {oid}: {exc}")
    e = lambda value: html.escape(str(value), quote=True)
    overlays, panels = [], []
    for number, component in enumerate(result.components, 1):
        targets = {slot.object_id for slot in component.slots}
        points = [p for oid in component.members for p in polygons.get(oid, [])]
        if points:
            xs, ys = zip(*points)
            x, y = min(xs), min(ys)
            overlays.append(f'<g class="component"><rect x="{x}" y="{y}" width="{max(xs)-x}" height="{max(ys)-y}"/><text x="{max(2,x+3)}" y="{max(16,y-5)}">C{number}</text></g>')
        rows = []
        for oid in component.members:
            obj = objects[oid]
            kind = "slot" if oid in targets else "preserve"
            if oid in polygons:
                coords = " ".join(f"{x:.2f},{y:.2f}" for x,y in polygons[oid])
                overlays.append(f'<polygon class="{kind}" points="{coords}"><title>{e(oid)}</title></polygon>')
            fields = [slot for slot in component.slots if slot.object_id == oid]
            details = []
            for slot in fields:
                indices = slot.paragraph_indices
                label = "весь объект" if indices is None else "абзацы " + ", ".join(map(str, indices))
                details.append(f'<li><b>{e(slot.role)}</b>: {e(label)}</li>')
            paragraphs = obj.get("paragraphs", [])
            para_rows = []
            for index, paragraph in enumerate(paragraphs):
                editable = any(slot.paragraph_indices is None or index in slot.paragraph_indices for slot in fields)
                para_rows.append(f'<li class="paragraph"><code>p{index}</code> {e(paragraph["text"])} <small>{"заменяемый" if editable else "сохраняется"}</small></li>')
            origin = obj.get('origin', {}).get('scope', 'slide')
            rows.append(f'<div class="object"><code>{e(oid)}</code> <span>{"Заменяемый" if fields else "Постоянный"}</span><small>Источник: {e(origin)}</small><ul>{"".join(details)}</ul><ol>{"".join(para_rows)}</ol></div>')
        panels.append(f'<section><h2>C{number} · {e(component.id)}</h2><p>{e(component.type)}</p>{"".join(rows)}</section>')
    for oid in result.unassigned:
        if oid in polygons:
            coords = " ".join(f"{x:.2f},{y:.2f}" for x,y in polygons[oid])
            overlays.append(f'<polygon class="unassigned" points="{coords}"><title>{e(oid)}</title></polygon>')
    rules = ''.join(f'<li>{e(rule.description)} <small>уверенность модели: {rule.confidence}; {e(", ".join(rule.evidence_ids))}</small></li>' for rule in result.patterns + result.constraints)
    body = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Декомпозиция {e(sid)}</title><style>
body{{margin:24px;background:#f5f7fa;color:#172033;font:15px/1.5 system-ui,sans-serif}}main{{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(320px,1fr);gap:24px}}svg{{width:100%;background:#111}}.visual{{position:sticky;top:12px;align-self:start}}section{{background:white;border:1px solid #dbe1e9;padding:16px;margin-bottom:12px;border-radius:8px}}h1{{font-size:24px}}h2{{font-size:17px;margin:0}}.object{{border-top:1px solid #eee;padding-top:10px;margin-top:10px}}small{{display:block;color:#566277}}code{{background:#edf1f7;padding:2px 4px}}ul,ol{{padding-left:20px}}.slot{{fill:#22c55e;fill-opacity:.07;stroke:#22c55e;stroke-width:2}}.preserve{{fill:none;stroke:#60a5fa;stroke-width:2}}.unassigned{{fill:none;stroke:#f87171;stroke-width:3}}.component rect{{fill:none;stroke:#fbbf24;stroke-width:3;stroke-dasharray:8 4}}.component text{{fill:#fff;stroke:#111;stroke-width:4;paint-order:stroke;font:bold 16px system-ui}}label{{display:inline-block;margin:0 14px 12px 0}}.warning{{color:#9a3412}}@media(max-width:900px){{main{{display:block}}.visual{{position:static}}}}
</style><h1>Декомпозиция слайда {e(sid)}</h1><p>Модель: {e(envelope.get('model'))} · Промпт: {e(envelope.get('prompt_version'))} · Назначение: {e(result.slide_role)}</p>
<p>Рамки показывают объекты и компоненты. Точные границы абзацев на изображении не вычисляются: индексы и текст указаны справа. {"Учтены объекты макета и образца." if composed else "Старый результат: элементы макета и образца не размечены. Нужен повторный analyze."}</p>
<p>Модель запросила проверку: {"да" if result.needs_review else "нет (это не подтверждение качества)"}. Нераспределённые: {e(', '.join(result.unassigned) or 'нет')}.</p>
<main><div class="visual"><nav><label><input type="checkbox" checked data-layer="component">Компоненты — жёлтый</label><label><input type="checkbox" checked data-layer="slot">Слоты — зелёный</label><label><input type="checkbox" checked data-layer="preserve">Постоянные — синий</label></nav>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Слайд с рамками компонентов"><image width="{width}" height="{height}" href="data:image/png;base64,{base64.b64encode(preview).decode()}"/>{''.join(overlays)}</svg>
<p class="warning">{e(' '.join(warnings))}</p></div><div>{''.join(panels)}<section><h2>Гипотезы модели</h2><ul>{rules}</ul></section></div></main>
<script>document.querySelectorAll('[data-layer]').forEach(input=>input.addEventListener('change',()=>document.querySelectorAll('svg .'+input.dataset.layer).forEach(el=>el.style.display=input.checked?'':'none')));</script></html>'''
    target = output or package / "reviews" / f"{sid}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target
