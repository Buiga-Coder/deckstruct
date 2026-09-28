"""Versioned transport models and offline package checks."""
import json
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from .objects import ExtractedObject, validate_object_links
from .schemas import StrictModel, SlideSemantics, Component
from .extract import sha256
from .validation import inspect_package


class Envelope(StrictModel):
    schema_version: Literal['0.2']
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    preview_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    model: str
    prompt_version: str
    object_scope: Literal['composed_v1']
    object_catalog: list[ExtractedObject]
    result: SlideSemantics

    @model_validator(mode='after')
    def object_links(self):
        validate_object_links(self.object_catalog)
        return self


class CatalogSlide(StrictModel):
    slide_id: str
    semantics: str
    preview: str
    components: list[Component]
    usage: str
    needs_review: bool
    unassigned: list[str]
    warnings: list[str] = Field(default_factory=list)


class Catalog(StrictModel):
    schema_version: Literal['0.3']
    source: str
    template: str
    source_sha256: str
    total_slides: int
    partial: bool
    slides: list[CatalogSlide]


class ValidationRow(StrictModel):
    slide_id: str
    semantics_path: str
    status: Literal['valid', 'invalid', 'unavailable']
    semantics_sha256: str | None = None
    usage: str | None = None
    needs_review: bool | None = None
    unassigned: list[str] | None = None
    model: str | None = None
    prompt_version: str | None = None
    reasons: list[dict] | None = None
    detail: str | None = None


class ValidationReport(StrictModel):
    schema_version: Literal['0.3']
    source_sha256: str
    counts: dict[str, int]
    valid_slide_ids: list[str]
    slides: list[ValidationRow]


def write_schemas(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for name, model in [('catalog', Catalog), ('semantics', Envelope), ('validation_report', ValidationReport)]:
        schema = model.model_json_schema()
        schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
        (folder/f'{name}.schema.json').write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding='utf-8')


def local_path(root, relative):
    path = (root/relative).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f'Path escapes package: {relative}')
    return path


def check_package(package):
    package = package.resolve()
    report = inspect_package(package)
    ValidationReport.model_validate(report)
    manifest = json.loads((package/'template.json').read_text(encoding='utf-8'))
    for asset in manifest.get('assets', {}).values():
        if asset.get('path'):
            path = local_path(package, asset['path'])
            if not path.is_file() or (asset.get('sha256') and sha256(path.read_bytes()) != asset['sha256']):
                raise ValueError(f'Missing/corrupt asset: {asset["path"]}')
    for sid in report['valid_slide_ids']:
        Envelope.model_validate_json((package/'semantics'/f'{sid}.json').read_text(encoding='utf-8'))
    if (package/'catalog.json').exists():
        catalog = Catalog.model_validate_json((package/'catalog.json').read_text(encoding='utf-8'))
        ids = [s.slide_id for s in catalog.slides]
        if len(ids)!=len(set(ids)) or set(ids)!=set(report['valid_slide_ids']):
            raise ValueError('Catalog slide coverage mismatch')
        if catalog.source_sha256 != report['source_sha256'] or catalog.total_slides != len(manifest['slides']) or catalog.partial != (len(ids)!=len(manifest['slides'])):
            raise ValueError('Catalog metadata mismatch')
        for entry in catalog.slides:
            env = Envelope.model_validate_json(local_path(package, entry.semantics).read_text(encoding='utf-8'))
            if env.result.slide_id != entry.slide_id or env.result.components != entry.components or env.result.usage != entry.usage or env.result.needs_review != entry.needs_review or env.result.unassigned != entry.unassigned:
                raise ValueError('Catalog differs from semantics')
            if sha256(local_path(package, entry.preview).read_bytes()) != env.preview_sha256:
                raise ValueError('Catalog preview mismatch')
        stored = json.loads((package/'validation_report.json').read_text(encoding='utf-8'))
        ValidationReport.model_validate(stored)
        if stored != report:
            raise ValueError('Stored validation report differs from current files')
    if (package/'checksums.json').exists():
        for relative, checksum in json.loads((package/'checksums.json').read_text()).items():
            if sha256(local_path(package, relative).read_bytes()) != checksum:
                raise ValueError(f'Checksum mismatch: {relative}')
    return report
