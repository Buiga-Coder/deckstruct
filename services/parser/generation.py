"""Private generator adapter. Reuses immutable parser exports and its bounded queue."""
import json
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path, PurePosixPath

from template_parser.batch import atomic_json
from template_parser.extract import sha256
from presentation_builder.catalog import TemplateCatalog
from presentation_builder.content import ContentPackage
from presentation_builder.planner import LlmPlanner
from presentation_builder.ooxml import PresentationBuilder
from presentation_builder.audit import audit_pptx


def configure():
    for suffix in ('BASE_URL', 'MODEL', 'API_KEY'):
        value = os.environ.get('VLM_' + suffix)
        if value:
            os.environ.setdefault('PRESENTATION_LLM_' + suffix, value)
            os.environ.setdefault('PRESENTATION_VLM_' + suffix, value)
    os.environ.setdefault('PRESENTATION_LLM_FALLBACK_MODELS', '')
    os.environ.setdefault('PRESENTATION_VLM_FALLBACK_MODELS', '')


def extract_content(source, destination):
    """Bound file count, expanded bytes, paths and nested Office archives."""
    with zipfile.ZipFile(source) as z:
        members = z.infolist()
        if len(members) > 200 or sum(i.file_size for i in members) > 100 * 1024**2:
            raise ValueError('CONTENT_LIMIT')
        for item in members:
            name = PurePosixPath(item.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in item.filename or ':' in item.filename:
                raise ValueError('UNSAFE_ZIP')
            if item.is_dir():
                continue
            if item.file_size > 20 * 1024**2 or item.flag_bits & 1:
                raise ValueError('CONTENT_LIMIT')
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(item) as src, target.open('wb') as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            if target.suffix.lower() in ('.docx', '.xlsx', '.pptx'):
                with zipfile.ZipFile(target) as nested:
                    if len(nested.infolist()) > 10000 or sum(i.file_size for i in nested.infolist()) > 100 * 1024**2:
                        raise ValueError('CONTENT_LIMIT')


class Generations:
    def __init__(self, jobs, root=None, inputs=None):
        configure()
        self.jobs = jobs
        self.root = Path(root or jobs.root.parent / 'generations')
        self.inputs = Path(inputs or '/inputs')
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        for path in self.root.glob('*/state.json'):
            state = json.loads(path.read_text(encoding='utf-8'))
            if state['status'] in ('queued', 'running'):
                state.update(status='interrupted', message='Обработка прервана. Можно продолжить.')
                atomic_json(path, state)

    def folder(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{32}', job_id):
            raise FileNotFoundError()
        return self.root / job_id

    def state(self, job_id):
        return json.loads((self.folder(job_id) / 'state.json').read_text(encoding='utf-8'))

    def update(self, job_id, **values):
        with self.lock:
            state = self.state(job_id)
            state.update(values)
            atomic_json(self.folder(job_id) / 'state.json', state)

    def submit(self, data):
        job_id = data['job_id']
        folder = self.folder(job_id)
        with self.lock:
            if (folder / 'state.json').exists():
                return self.state(job_id)  # Idempotent API retry; never submit twice.
            count = data.get('slide_count')
            brief = data.get('brief')
            name = data.get('content_name', '')
            if type(count) is not int or not 3 <= count <= 20 or not isinstance(brief, str) or not 1 <= len(brief.strip()) <= 20000:
                raise ValueError('INVALID_REQUEST')
            if not re.fullmatch('[a-f0-9]{32}', name) or not (self.inputs / name).is_file():
                raise ValueError('CONTENT_MISSING')
            package = self.jobs.version(data['parser_job_id'], data['version'])
            expected = data.get('archive_sha256')
            if sha256(package.with_suffix('.zip').read_bytes()) != expected:
                raise ValueError('TEMPLATE_CHANGED')
            catalog = TemplateCatalog(package)
            if not catalog.templates:
                raise ValueError('NO_VALID_SLIDES')
            if shutil.disk_usage(self.root).free < 2 * 1024**3:
                raise OverflowError('Storage reserve reached')
            if not self.jobs.capacity.acquire(blocking=False):
                raise OverflowError('Queue full')
            try:
                folder.mkdir()
                shutil.copyfile(self.inputs / name, folder / 'content.zip')
                extract_content(folder / 'content.zip', folder / 'content')
                atomic_json(folder / 'input.json', data)
                atomic_json(folder / 'state.json', dict(job_id=job_id, status='queued', percent=0, message='В общей очереди анализа и генерации', variants=0))
                self.jobs.executor.submit(self.process, job_id)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                self.jobs.capacity.release()
                raise
        return self.state(job_id)

    def resume(self, job_id):
        with self.lock:
            state = self.state(job_id)
            if state['status'] not in ('failed', 'interrupted'):
                raise ValueError('INVALID_STATE')
            if not self.jobs.capacity.acquire(blocking=False):
                raise OverflowError('Queue full')
            state.update(status='queued', message='Продолжаем сохранённое задание')
            atomic_json(self.folder(job_id) / 'state.json', state)
            self.jobs.executor.submit(self.process, job_id)

    def process(self, job_id):
        folder = self.folder(job_id)
        try:
            data = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
            package = self.jobs.version(data['parser_job_id'], data['version'])
            self.update(job_id, status='running', percent=5, message='Читаем материалы и сохранённый анализ')
            content = ContentPackage(folder / 'content', captions_path=folder / 'captions.json' if (folder / 'captions.json').exists() else None)
            if not content.model_view()['items'] or all(x['kind'] == 'unavailable' for x in content.model_view()['items']):
                raise ValueError('EMPTY_CONTENT')
            if len(content.uncaptioned_images()) > 20:
                raise ValueError('IMAGE_LIMIT')
            if content.uncaptioned_images():
                self.update(job_id, percent=10, message='Анализируем изображения материалов')
                content.caption_images(model=os.environ['PRESENTATION_VLM_MODEL'])
                content.write_captions(folder / 'captions.json')
            catalog = TemplateCatalog(package)
            # Shared master/layout objects and charts cannot be edited by this builder.
            # Never advertise those slots to the planner or silently count them as filled.
            for sid, template in list(catalog.templates.items()):
                for key, slot in list(template.slots.items()):
                    obj = catalog.object(sid, slot.object_id)
                    if obj['source_part'].lstrip('/') != template.source_part or slot.content_type == 'chart':
                        del template.slots[key]
                if not template.slots:
                    del catalog.templates[sid]
            if not catalog.templates:
                raise ValueError('NO_EDITABLE_SLIDES')
            planner = LlmPlanner(catalog, model=os.environ.get('PRESENTATION_LLM_MODEL'))
            builder = PresentationBuilder(package)
            builder.catalog = catalog
            plans = []
            for variant in range(1, 4):
                self.update(job_id, percent=15 + (variant-1)*27, message=f'Создаём вариант {variant} из 3')
                plan_path = folder / f'variant-{variant}.plan.json'
                if plan_path.exists():
                    plan = json.loads(plan_path.read_text(encoding='utf-8'))
                    if planner._validate_grounded_plan(plan, content, data['slide_count']):
                        raise ValueError('SAVED_PLAN_INVALID')
                else:
                    plan = planner.plan(data['brief'], content, variant=variant, slide_count=data['slide_count'], previous_plans=plans)
                    atomic_json(plan_path, plan)
                plans.append(plan)
                pptx = folder / f'variant-{variant}.pptx'
                builder.build(plan, pptx)
                audit = audit_pptx(pptx)
                atomic_json(folder / f'variant-{variant}.audit.json', audit)
                if not audit['ok']:
                    raise ValueError('OUTPUT_AUDIT_FAILED')
            self.update(job_id, status='done', percent=100, variants=3, message='Готовы три редактируемых PPTX')
        except Exception as exc:
            # Persist only a bounded, redacted diagnostic privately; public message is fixed.
            detail = str(exc)
            for key in ('VLM_API_KEY', 'PRESENTATION_LLM_API_KEY', 'PRESENTATION_VLM_API_KEY'):
                if os.environ.get(key):
                    detail = detail.replace(os.environ[key], '[REDACTED]')
            atomic_json(folder / 'error.json', {'type': type(exc).__name__, 'detail': detail[:3000]})
            self.update(job_id, status='failed', message='Не удалось завершить генерацию. Сохранённые планы сохранены; можно повторить.', error=type(exc).__name__)
        finally:
            self.jobs.capacity.release()

    def download(self, job_id, variant):
        if variant not in ('1', '2', '3') or self.state(job_id)['status'] != 'done':
            raise FileNotFoundError()
        return self.folder(job_id) / f'variant-{variant}.pptx'
