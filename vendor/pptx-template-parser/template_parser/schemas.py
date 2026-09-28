from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Slot(StrictModel):
    object_id: str
    role: str
    content_type: Literal["text", "image", "table", "chart"]
    paragraph_indices: list[int] | None = Field(default=None, min_length=1,
        description="Zero-based paragraphs for text slots; null replaces the whole object. Unselected paragraphs stay unchanged.")


class Component(StrictModel):
    id: str
    type: str
    members: list[str] = Field(min_length=1)
    slots: list[Slot] = Field(default_factory=list)
    preserve: list[str] = Field(default_factory=list)


class Hypothesis(StrictModel):
    description: str
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class SlideSemantics(StrictModel):
    slide_id: str
    slide_role: str
    usage: Literal["generation_template", "instruction", "asset_library", "example", "unknown"]
    components: list[Component]
    unassigned: list[str]
    patterns: list[Hypothesis] = Field(default_factory=list)
    constraints: list[Hypothesis] = Field(default_factory=list)
    needs_review: bool


def validate_semantics(result: SlideSemantics, slide: dict) -> None:
    """Collect actionable violations without guessing a corrected decomposition."""
    errors: list[str] = []
    def report(where, message, ids):
        errors.append(f"{where}: {message}: {sorted(ids)}")

    if result.slide_id != slide["id"]:
        errors.append(f"slide_id must be {slide['id']!r}")
    objects = {obj["id"]: obj for obj in slide["objects"]}
    leaves = {key for key, obj in objects.items() if obj["kind"] != "group"}
    assigned: set[str] = set()
    component_ids: set[str] = set()
    for index, component in enumerate(result.components):
        where = f"components[{index}] ({component.id})"
        if component.id in component_ids:
            errors.append(f"{where}: duplicate component id")
        component_ids.add(component.id)
        members = set(component.members)
        if len(members) != len(component.members):
            report(where, "duplicate members", {x for x in members if component.members.count(x) > 1})
        if members - leaves:
            report(where, "members must reference existing leaf objects, not group containers", members - leaves)
        if members & assigned:
            report(where, "objects already belong to another component", members & assigned)
        assigned |= members
        slots = {slot.object_id for slot in component.slots}
        preserved = set(component.preserve)
        if len(preserved) != len(component.preserve):
            report(where, "duplicate preserve entries", {x for x in preserved if component.preserve.count(x) > 1})
        if slots & preserved:
            report(where, "objects cannot be both slots and preserved", slots & preserved)
        if members - (slots | preserved):
            report(where, "members need a slot or preserve entry", members - (slots | preserved))
        if (slots | preserved) - members:
            report(where, "slot/preserve references must belong to members", (slots | preserved) - members)
        targets: dict[str, set[int] | None] = {}
        for slot in component.slots:
            obj = objects.get(slot.object_id)
            if obj is None:
                report(where, "unknown slot object", {slot.object_id})
                continue
            indices = slot.paragraph_indices
            if indices is not None:
                if slot.content_type != "text":
                    report(where, "paragraph_indices allowed only for text", {slot.object_id})
                count = len(obj.get("paragraphs", []))
                if len(indices) != len(set(indices)) or any(i < 0 or i >= count for i in indices):
                    errors.append(f"{where}: invalid paragraph_indices {indices} for {slot.object_id}; use unique indices in range(0, {count})")
            selected = set(indices) if indices is not None else None
            if slot.object_id in targets:
                previous = targets[slot.object_id]
                if previous is None or selected is None or previous & selected:
                    report(where, "overlapping slot targets", {slot.object_id})
                targets[slot.object_id] = None if previous is None or selected is None else previous | selected
            else:
                targets[slot.object_id] = selected
            supported = obj.get("text") is not None if slot.content_type == "text" else obj["kind"] == slot.content_type
            if not supported or obj["kind"] == "group":
                errors.append(f"{where}: incompatible content_type {slot.content_type!r} for {slot.object_id} (kind={obj['kind']})")
    unassigned = set(result.unassigned)
    if len(unassigned) != len(result.unassigned):
        report("unassigned", "duplicate entries", {x for x in unassigned if result.unassigned.count(x) > 1})
    if unassigned - leaves:
        report("unassigned", "use existing leaf objects only, not group containers", unassigned - leaves)
    if assigned & unassigned:
        report("unassigned", "objects already assigned to components; remove duplicate classification", assigned & unassigned)
    if leaves - (assigned | unassigned):
        report("coverage", "missing leaf objects; assign to a component or unassigned", leaves - (assigned | unassigned))
    for index, rule in enumerate(result.patterns + result.constraints):
        unknown = set(rule.evidence_ids) - objects.keys()
        if unknown:
            report(f"rule[{index}]", "unknown evidence_ids", unknown)
    if unassigned and not result.needs_review:
        errors.append("needs_review must be true when unassigned is nonempty")
    if errors:
        raise ValueError("\n".join(errors))
