"""Resumable sequential analysis. Saved semantics are the source of resume state."""
import json
import time
from collections import Counter
from pathlib import Path
from PIL import Image
from .extract import sha256
from .composition import compose_slide
from .schemas import SlideSemantics, validate_semantics
from .vlm import analyze_slide, PROMPT_VERSION, ModelOutputError
from .api import APIError


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def run_batch(package: Path, config: dict, *, delay=10):
    manifest = json.loads((package / 'template.json').read_text(encoding='utf-8'))
    if sha256((package / 'source.pptx').read_bytes()) != manifest['source']['sha256']:
        raise ValueError('source.pptx hash differs from template.json')
    report = {'schema_version': '0.1', 'status': 'running', 'model': config['model'],
              'prompt_version': PROMPT_VERSION, 'source_sha256': manifest['source']['sha256'],
              'slides': [{'slide_id': s['id'], 'status': 'pending', 'human_reviewed': False}
                         for s in manifest['slides']]}
    report_path = package / 'analysis_report.json'
    def save():
        report['counts'] = dict(Counter(row['status'] for row in report['slides']))
        atomic_json(report_path, report)
    save()
    sent = False
    try:
        for source, row in zip(manifest['slides'], report['slides']):
            sid = source['id']
            try:
                slide = compose_slide(manifest, source)
                preview = package / 'previews' / f'{sid}.png'
                with Image.open(preview) as image:
                    image.verify()
                    if image.format != 'PNG':
                        raise ValueError('Preview must be PNG')
                expected = {'source_sha256': manifest['source']['sha256'],
                            'preview_sha256': sha256(preview.read_bytes()),
                            'model': config['model'], 'prompt_version': PROMPT_VERSION,
                            'object_scope': 'composed_v1', 'schema_version': '0.2'}
                target = package / 'semantics' / f'{sid}.json'
                current = None
                try:
                    existing = json.loads(target.read_text(encoding='utf-8'))
                    if all(existing.get(k) == v for k, v in expected.items()) and existing.get('object_catalog') == slide['objects']:
                        candidate = SlideSemantics.model_validate(existing['result'])
                        validate_semantics(candidate, slide)
                        current = candidate
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                if current is not None:
                    row.update(status='skipped', needs_review=current.needs_review)
                else:
                    if sent:
                        time.sleep(delay)
                    row['status'] = 'processing'
                    save()
                    sent = True
                    current = analyze_slide(slide, preview, config, rejected_dir=package/'diagnostics'/sid)
                    validate_semantics(current, slide)
                    atomic_json(target, {**expected, 'object_catalog': slide['objects'], 'result': current.model_dump()})
                    row.update(status='completed', needs_review=current.needs_review)
            except APIError as exc:
                row.update(status='api_error', error=str(exc))
                # Opt-in deployment policy: isolate a transient slide failure, without
                # repeating an ambiguously completed paid request. Stop after 3
                # consecutive transient failures to avoid a failing-provider loop.
                transient = any(x in str(exc) for x in ('category=timeout', 'category=network_error', 'HTTP 500:', 'HTTP 502:', 'HTTP 503:', 'HTTP 504:'))
                recent = report['slides'][max(0, report['slides'].index(row)-2):report['slides'].index(row)+1]
                if config.get('continue_transient_errors') and transient and not (len(recent) == 3 and all(x['status'] == 'api_error' for x in recent)):
                    save()
                    continue
                report['status'] = 'stopped_api_error'
                save()
                print(f'{sid}: API error; batch stopped. See {report_path}')
                return report
            except (ModelOutputError, ValueError, OSError, KeyError, TypeError) as exc:
                row.update(status='error', error=str(exc))
            save()
            print(f"{sid}: {row['status']}" + (' (needs review)' if row.get('needs_review') else ''))
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        for row in report['slides']:
            if row['status'] == 'processing':
                row['status'] = 'interrupted'
        save()
        return report
    report['status'] = 'finished_with_errors' if any(r['status'] in ('error', 'api_error') for r in report['slides']) else 'finished'
    save()
    return report
