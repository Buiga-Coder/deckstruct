"""Compose inherited drawing objects without mutating extracted source facts."""
from copy import deepcopy
from lxml import etree


def visible_part(part):
    xml = part.get('xml')
    return not xml or etree.fromstring(xml.encode()).get('showMasterSp', '1') not in ('0', 'false')


def compose_slide(manifest, slide):
    result = deepcopy(slide)
    layout = next((p for p in manifest.get('layouts', []) if p['id'] == slide.get('layout_id')), None)
    master = next((p for p in manifest.get('masters', []) if layout and p['id'] == layout['master_id']), None)
    layers = []
    if visible_part(slide):
        if layout and visible_part(layout) and master:
            layers.append(('master', master))
        if layout:
            layers.append(('layout', layout))
    layers.append(('slide', slide))
    objects = []
    for scope, part in layers:
        excluded = set()
        for source in part['objects']:
            xml = source.get('xml')
            hidden = False
            if xml:
                node = etree.fromstring(xml.encode())
                props = node.find('.//{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr')
                hidden = props is not None and props.get('hidden', '0') in ('1', 'true')
            # Layout/master placeholders are inheritance definitions, not independent instances.
            if hidden or (scope != 'slide' and 'placeholder' in source) or source.get('parent_id') in excluded:
                excluded.add(source['id'])
                continue
            obj = deepcopy(source)
            obj['origin'] = {'scope': scope, 'part_id': part['id'], 'source_part': source.get('source_part')}
            objects.append(obj)
    result['objects'] = objects
    return result
