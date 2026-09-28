"""Structural validation only; never select slides for a generator."""
import json
from collections import Counter
from .extract import sha256
from .composition import compose_slide
from .schemas import SlideSemantics, validate_semantics
from .objects import ExtractedObject, validate_object_links

def inspect_package(package):
    manifest = json.loads((package/'template.json').read_text(encoding='utf-8'))
    source_hash = sha256((package/'source.pptx').read_bytes())
    if source_hash != manifest['source']['sha256']:
        raise ValueError('Source hash mismatch')
    rows = []
    for source in manifest['slides']:
        sid = source['id']
        path = package/'semantics'/f'{sid}.json'
        row = {'slide_id': sid, 'semantics_path': f'semantics/{sid}.json'}
        if not path.exists():
            row.update(status='unavailable', reasons=[{'code':'NO_RESULT', 'object_ids':[]}])
        else:
            raw = path.read_bytes()
            row['semantics_sha256'] = sha256(raw)
            try:
                envelope = json.loads(raw)
                slide = compose_slide(manifest, source)
                if envelope.get('source_sha256') != source_hash or envelope.get('preview_sha256') != sha256((package/'previews'/f'{sid}.png').read_bytes()):
                    raise ValueError('Input hash mismatch')
                if envelope.get('schema_version') != '0.2' or envelope.get('object_scope') != 'composed_v1' or envelope.get('object_catalog') != slide['objects']:
                    raise ValueError('Incompatible object catalog or schema')
                result = SlideSemantics.model_validate(envelope['result'])
                typed_objects = [ExtractedObject.model_validate(obj) for obj in envelope['object_catalog']]
                validate_object_links(typed_objects)
                validate_semantics(result, slide)
                row.update(status='valid', usage=result.usage, needs_review=result.needs_review, unassigned=result.unassigned)
                row.update(model=envelope.get('model'), prompt_version=envelope.get('prompt_version'))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                row.update(status='invalid', reasons=[{'code':'INVALID_OR_STALE_RESULT', 'object_ids':[]}], detail=str(exc))
        rows.append(row)
    return {'schema_version':'0.3', 'source_sha256':source_hash,
            'counts':dict(Counter(r['status'] for r in rows)),
            'valid_slide_ids':[r['slide_id'] for r in rows if r['status']=='valid'], 'slides':rows}
