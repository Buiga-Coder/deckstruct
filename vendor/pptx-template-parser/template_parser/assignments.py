"""Internal VLM contract. Public SlideSemantics remains unchanged."""
from typing import Literal
from pydantic import Field
from .schemas import StrictModel, Slot, SlideSemantics, Hypothesis, validate_semantics


class EditSlot(StrictModel):
    role: str
    content_type: Literal['text', 'image', 'table', 'chart']
    paragraph_indices: list[int] | None = Field(default=None, min_length=1)


class Assignment(StrictModel):
    object_id: str
    component_id: str | None
    action: Literal['replace', 'preserve', 'unassigned']
    slots: list[EditSlot] = Field(default_factory=list)


class ComponentLabel(StrictModel):
    id: str
    type: str


class AssignmentResponse(StrictModel):
    slide_id: str
    slide_role: str
    usage: Literal['generation_template', 'instruction', 'asset_library', 'example', 'unknown']
    components: list[ComponentLabel]
    assignments: list[Assignment]
    patterns: list[Hypothesis] = Field(default_factory=list)
    constraints: list[Hypothesis] = Field(default_factory=list)
    needs_review: bool


def compile_assignments(response: AssignmentResponse, slide: dict, *, normalizations=None) -> SlideSemantics:
    errors = []
    components = {}
    for c in response.components:
        if c.id in components:
            errors.append(f'duplicate component id: {c.id}')
        components[c.id] = dict(id=c.id, type=c.type, members=[], slots=[], preserve=[])
    leaves = {o['id'] for o in slide['objects'] if o['kind'] != 'group'}
    seen = set()
    unassigned = []
    for a in response.assignments:
        if a.object_id in seen:
            errors.append(f'duplicate assignment: {a.object_id}')
        seen.add(a.object_id)
        if a.object_id not in leaves:
            errors.append(f'not an existing leaf object: {a.object_id}')
        if (a.action == 'replace') != bool(a.slots):
            errors.append(f'{a.object_id}: only replace requires nonempty slots; other actions require empty slots')
        if a.action == 'unassigned':
            if a.component_id is not None:
                errors.append(f'{a.object_id}: unassigned requires component_id=null')
            unassigned.append(a.object_id)
            continue
        if a.component_id not in components:
            errors.append(f'{a.object_id}: unknown component_id {a.component_id!r}; declare this component with id/type or choose an existing component: {sorted(components)}. Do not guess or drop the object.')
            continue
        c = components[a.component_id]
        c['members'].append(a.object_id)
        if a.action == 'preserve':
            c['preserve'].append(a.object_id)
        else:
            c['slots'].extend(Slot(object_id=a.object_id, **s.model_dump()) for s in a.slots)
    if leaves - seen:
        errors.append(f'missing assignments; decide replace/preserve/unassigned for: {sorted(leaves - seen)}')
    if errors:
        raise ValueError('\n'.join(errors))
    empty = [c['id'] for c in components.values() if not c['members']]
    result = SlideSemantics(**response.model_dump(exclude={'assignments', 'components'}),
                            components=[c for c in components.values() if c['members']], unassigned=unassigned)
    validate_semantics(result, slide)
    if empty and normalizations is not None:
        normalizations.append({'action': 'remove_unused_component_definitions', 'component_ids': empty})
    return result
