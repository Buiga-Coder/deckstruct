"""Deployment adapter; upstream parser remains an unmodified, pinned snapshot."""
import json
import os
import re
import shutil
import uuid
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree
from template_parser.server import Jobs, make_server
from template_parser.batch import atomic_json, run_batch
from template_parser.extract import extract_template, sha256
from template_parser.render import render_package
from template_parser.exporter import export_package

NS = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
ROOT = Path(os.environ.get('PARSER_STORAGE', '/data/jobs'))


def configuration():
    # Optional provider-specific settings are server-owned, never supplied by a browser.
    path = Path(os.environ.get('VLM_CONFIG_PATH', '/run/secrets/vlm.json'))
    config = json.loads(path.read_text()) if path.is_file() else {}
    for field, env in [('base_url', 'VLM_BASE_URL'), ('model', 'VLM_MODEL')]:
        if os.environ.get(env):
            config[field] = os.environ[env]
    config.setdefault('api_key_env', 'VLM_API_KEY')
    config.setdefault('timeout_seconds', 180)
    config.setdefault('max_tokens', 8192)
    config.setdefault('max_retries', 2)
    config.setdefault('validation_retries', 1)
    config['renderer'] = 'libreoffice'
    return config


def configured(config):
    return bool(config.get('base_url') and config.get('model') and os.environ.get(config['api_key_env']))


def summarize(manifest):
    colors, fonts, sizes = Counter(), Counter(), Counter()
    for section in ('themes', 'masters', 'layouts', 'slides'):
        for entry in manifest[section]:
            xmls = [entry['xml']] if entry.get('xml') else []
            # Theme XML and each slide/master XML already contain their child shapes.
            for xml in xmls:
                tree = etree.fromstring(xml.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
                for node in tree.findall('.//a:srgbClr', NS):
                    value = node.get('val', '')
                    if re.fullmatch(r'[0-9A-Fa-f]{6}', value):
                        colors['#' + value.upper()] += 1
                for tag in ('latin', 'ea', 'cs'):
                    for node in tree.findall(f'.//a:{tag}', NS):
                        family = node.get('typeface', '')
                        if family and not family.startswith('+'):
                            fonts[family] += 1
                for node in tree.findall('.//a:rPr', NS):
                    if node.get('sz', '').isdigit():
                        sizes[int(node.get('sz')) / 100] += 1
    total = sum(colors.values()) or 1
    return {
        'slides': len(manifest['slides']), 'masters': len(manifest['masters']),
        'slideSize': manifest['slide_size'], 'sourceSha256': manifest['source']['sha256'],
        'palette': [{'hex': color, 'occurrences': count, 'weight': count / total}
                    for color, count in colors.most_common(32)],
        'fonts': [{'family': name, 'occurrences': count} for name, count in fonts.most_common(30)],
        'fontSizes': [{'pt': size, 'occurrences': count} for size, count in sizes.most_common(30)],
        'layouts': [{'id': x['id'], 'name': x['name']} for x in manifest['layouts']],
        'grid': None, 'tokens': {},
        'notes': ['Цвета и шрифты прочитаны из XML и тем PPTX. Частота — число объявлений, не площадь цвета.',
                  'Наследование всех стилей и общая сетка не вычислены. LibreOffice может заменять отсутствующие шрифты.'],
    }


class IntegratedJobs(Jobs):
    def submit(self, data):
        if shutil.disk_usage(self.root).free < 2 * 1024 ** 3:
            raise OverflowError('Storage reserve reached')
        return super().submit(data)

    def resume(self, job_id):
        state = self.state(job_id)
        if state['status'] == 'awaiting_configuration':
            if not configured(configuration()):
                raise ValueError('Configure VLM before resuming')
            self.update(job_id, status='interrupted')
        return super().resume(job_id)

    def process(self, job_id):
        try:
            folder = self.folder(job_id)
            package = folder / 'package'
            if not package.exists():
                self.update(job_id, status='extracting')
                extract_template(folder / 'upload.pptx', package)
            manifest = json.loads((package / 'template.json').read_text())
            atomic_json(folder / 'summary.json', summarize(manifest))
            self.update(job_id, slides=len(manifest['slides']))
            if not (package / 'previews').exists():
                self.update(job_id, status='rendering')
                render_package(package, renderer='libreoffice')
            config = configuration()
            if not configured(config):
                self.update(job_id, status='awaiting_configuration', error='VLM_NOT_CONFIGURED')
                return
            self.update(job_id, status='analyzing', error=None)
            delay = max(0, min(60, float(os.environ.get('VLM_SLIDE_DELAY', '10'))))
            report = run_batch(package, config, delay=delay)
            self.update(job_id, status='exporting')
            version = uuid.uuid4().hex
            exported = export_package(package, folder / 'versions' / version)
            self.update(job_id, status='completed' if report['status'] == 'finished' else 'partial',
                        version=version, counts=exported['counts'],
                        archive_sha256=sha256(Path(exported['archive']).read_bytes()), error=None)
        except Exception as exc:
            self.update(job_id, status='failed', error=type(exc).__name__)
        finally:
            self.capacity.release()


def main():
    token = os.environ['PARSER_API_TOKEN']
    if len(token) < 32:
        raise ValueError('PARSER_API_TOKEN must contain at least 32 characters')
    jobs = IntegratedJobs(ROOT, configuration())
    # Reuse authenticated upstream HTTP routes on the private Docker network.
    prototype = make_server(jobs, token, port=0)
    upstream = prototype.RequestHandlerClass
    prototype.server_close()

    class Handler(upstream):
        def dispatch(self, method):
            parts = urlsplit(self.path).path.strip('/').split('/')
            if method == 'GET' and parts == ['healthz']:
                return self.reply(200, {'ok': True, 'vlmConfigured': configured(configuration())})
            if not self.authorized():
                return self.reply(401, {'error': 'Unauthorized'})
            if method == 'GET' and len(parts) >= 3 and parts[0] == 'jobs':
                try:
                    folder = jobs.folder(parts[1])
                    if parts[2:] == ['summary']:
                        return self.reply(200, json.loads((folder / 'summary.json').read_text()))
                    if len(parts) == 4 and parts[2] == 'previews' and re.fullmatch(r's[1-9][0-9]*\.png', parts[3]):
                        return self.reply(200, (folder / 'package' / 'previews' / parts[3]).read_bytes(), 'image/png')
                except FileNotFoundError:
                    return self.reply(404, {'error': 'Not ready'})
            return super().dispatch(method)

    server = ThreadingHTTPServer(('0.0.0.0', 8765), Handler)
    print('Parser listening on private network :8765', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
