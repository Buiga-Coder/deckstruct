"""Typed extraction facts; no inferred editing decisions."""
from typing import Literal
from pydantic import ConfigDict, Field, model_validator
from .schemas import StrictModel


class Fact(StrictModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Geometry(Fact):
    x: int | None
    y: int | None
    width: int | None = Field(ge=0)
    height: int | None = Field(ge=0)
    rotation: float
    units: Literal['EMU']
    coordinate_space: str


class Run(Fact):
    text: str
    font_name: str | None
    font_size_pt: float | None
    bold: bool | None
    italic: bool | None


class Paragraph(Fact):
    text: str
    level: int = Field(ge=0, le=8)
    runs: list[Run]


class AssetReference(Fact):
    source_part: str
    asset_id: str
    path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class Origin(Fact):
    scope: Literal['slide', 'layout', 'master']
    part_id: str
    source_part: str


class Placeholder(Fact):
    idx: int = Field(ge=0)
    type: str


class TableCell(Fact):
    text: str
    is_merge_origin: bool
    is_spanned: bool


class ExtractedObject(Fact):
    id: str = Field(min_length=1)
    shape_id: int = Field(ge=1)
    name: str
    kind: Literal['group', 'table', 'chart', 'image', 'connector', 'shape', 'unknown']
    parent_id: str | None
    z_order: int = Field(ge=0)
    geometry: Geometry
    source_part: str
    xml: str
    text: str | None
    paragraphs: list[Paragraph]
    assets: list[AssetReference]
    origin: Origin
    placeholder: Placeholder | None = None
    table: list[list[TableCell]] | None = None

    @model_validator(mode='after')
    def consistent(self):
        if self.geometry.coordinate_space != (self.parent_id or 'slide'):
            raise ValueError('Coordinate space must match parent_id')
        if self.origin.source_part != self.source_part:
            raise ValueError('Origin source_part mismatch')
        if (self.kind == 'table') != (self.table is not None):
            raise ValueError('Table data required only for table objects')
        return self


def validate_object_links(objects):
    by_id = {obj.id: obj for obj in objects}
    if len(by_id) != len(objects):
        raise ValueError('Duplicate object ID')
    addresses = {(obj.source_part, obj.shape_id) for obj in objects}
    if len(addresses) != len(objects):
        raise ValueError('Duplicate source object address')
    for obj in objects:
        seen = {obj.id}
        child = obj
        while child.parent_id is not None:
            parent = by_id.get(child.parent_id)
            if parent is None or parent.kind != 'group' or parent.source_part != obj.source_part:
                raise ValueError(f'Invalid group parent for {obj.id}')
            if parent.id in seen:
                raise ValueError('Group cycle')
            seen.add(parent.id)
            child = parent
