"""Export validated partial packages without inference or secrets."""
import json
import shutil
import zipfile
from pathlib import Path
from .validation import inspect_package
from .batch import atomic_json
from .extract import sha256
from .contracts import write_schemas, check_package
from contextlib import contextmanager
import uuid


@contextmanager
def staging_directory(parent):
    parent = parent.resolve()
    path = parent / ('export-' + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        if path.resolve().parent != parent:
            raise ValueError('Unexpected staging cleanup path')
        shutil.rmtree(path)


def export_package(package: Path, output: Path):
    package, output = package.resolve(), output.resolve()
    archive = output.with_suffix('.zip')
    if output == package or package in output.parents or output in package.parents:
        raise ValueError('Export directory must be separate from the working package')
    if output.exists() or archive.exists():
        raise ValueError('Export destination exists; choose a new output name')
    report = inspect_package(package)
    manifest = json.loads((package/'template.json').read_text(encoding='utf-8'))
    output.parent.mkdir(parents=True, exist_ok=True)
    with staging_directory(output.parent) as temp:
        stage = Path(temp)/'package'
        stage.mkdir()
        shutil.copy2(package/'source.pptx', stage/'source.pptx')
        # Full extracted facts preserve layout/master relationships and asset references.
        shutil.copy2(package/'template.json', stage/'template.json')
        if (package/'assets').exists():
            shutil.copytree(package/'assets', stage/'assets')
        slides = []
        for row in report['slides']:
            if row['status'] != 'valid':
                continue
            sid = row['slide_id']
            source = package/'semantics'/f'{sid}.json'
            raw = source.read_bytes()
            if sha256(raw) != row['semantics_sha256']:
                raise ValueError('Semantics changed during export; rerun export')
            envelope = json.loads(raw)
            preview = package/'previews'/f'{sid}.png'
            if sha256(preview.read_bytes()) != envelope['preview_sha256']:
                raise ValueError('Preview changed during export')
            for folder, data in [('semantics', raw), ('previews', preview.read_bytes())]:
                (stage/folder).mkdir(exist_ok=True)
                (stage/folder/(sid + ('.json' if folder=='semantics' else '.png'))).write_bytes(data)
            slides.append({'slide_id':sid, 'semantics':f'semantics/{sid}.json',
                           'preview':f'previews/{sid}.png', 'components':envelope['result']['components'],
                           'usage':row['usage'], 'needs_review':row['needs_review'], 'unassigned':row['unassigned'],
                           'warnings':row.get('warnings', [])})
        if sha256((stage/'source.pptx').read_bytes()) != report['source_sha256']:
            raise ValueError('Source changed during export')
        atomic_json(stage/'validation_report.json', report)
        atomic_json(stage/'catalog.json', {'schema_version':'0.3',
                    'source':'source.pptx', 'template':'template.json',
                    'source_sha256':report['source_sha256'], 'total_slides':len(manifest['slides']),
                    'partial':len(slides)!=len(manifest['slides']), 'slides':slides})
        (stage/'README.md').write_text('''# Пакет генератора
Начните с catalog.json. Включены ВСЕ структурно валидные декомпозиции независимо
от usage, needs_review, unassigned и типов полей. Назначение слайдов и неопределённости
передаются как данные; выбор слайдов выполняет генератор. validation_report.json
описывает valid/invalid/unavailable. Невалидные результаты не выдаются за готовые.
Проверка структуры не доказывает смысловую правильность или вместимость текста.
Ищите object_id в object_catalog файла semantics/sN.json. source_part и shape_id
адресуют объект в исходном PPTX; shape_id не индекс. Дети групп ищутся рекурсивно.
paragraph_indices нумеруются с нуля, null означает весь текст объекта. Сохраняйте
форматирование runs и не меняйте preserve. Общие объекты layout/master не редактируйте.
Генератор должен работать с копией исходника и проверять переполнение текста.
Пути относительны корню пакета. assets содержит все ресурсы исходника для сохранения
ссылок, включая ресурсы исключённых слайдов. API-конфиги и диагностика не включены.
checksums.json — SHA-256 всех остальных файлов. Распакуйте ZIP целиком.
''', encoding='utf-8')
        write_schemas(stage/'schemas')
        check_package(stage)
        atomic_json(stage/'checksums.json', {p.relative_to(stage).as_posix():sha256(p.read_bytes()) for p in stage.rglob('*') if p.is_file()})
        staged_zip = Path(temp)/'package.zip'
        with zipfile.ZipFile(staged_zip, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in stage.rglob('*'):
                if p.is_file():
                    z.write(p, p.relative_to(stage))
        with zipfile.ZipFile(staged_zip) as z:
            if z.testzip() is not None:
                raise ValueError('Archive verification failed')
        shutil.move(str(stage), str(output))
        shutil.move(str(staged_zip), str(archive))
    return {'output':str(output), 'archive':str(archive), 'exported':len(slides), 'counts':report['counts']}
