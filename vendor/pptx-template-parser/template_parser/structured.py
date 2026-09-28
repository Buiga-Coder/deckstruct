"""Per-slide constrained output; local validation remains authoritative."""
import json
from copy import deepcopy
from .assignments import AssignmentResponse


def response_schema(leaf_ids, objects=None):
    schema = deepcopy(AssignmentResponse.model_json_schema())
    assignment = schema['$defs']['Assignment']
    del assignment['properties']['object_id']
    assignment['required'].remove('object_id')
    branches = []
    for action in ('replace', 'preserve', 'unassigned'):
        branch = deepcopy(assignment)
        props = branch['properties']
        props['action'] = {'type': 'string', 'enum': [action]}
        props['component_id'] = ({'type': 'null'} if action == 'unassigned'
                                 else {'type': 'string', 'minLength': 1})
        if action == 'replace':
            props['slots']['minItems'] = 1
        else:
            props['slots']['maxItems'] = 0
        branches.append(branch)
    schema['$defs']['Assignment'] = {'anyOf': branches}
    schema['properties']['assignments'] = {
        'type': 'object', 'properties': {oid: {'$ref': '#/$defs/Assignment'} for oid in leaf_ids},
        'required': list(leaf_ids), 'additionalProperties': False}
    if objects is not None:
        catalog = {o['id']: o for o in objects}
        for oid in leaf_ids:
            obj = catalog[oid]
            variants = deepcopy(branches[1:])  # preserve/unassigned always remain possible
            kinds = (['text'] if obj.get('text') is not None else [])
            if obj['kind'] in ('image', 'table', 'chart'):
                kinds.append(obj['kind'])
            for kind in kinds:
                count = len(obj.get('paragraphs', []))
                for paragraph_mode in ([False, True] if kind == 'text' and count else [False]):
                    branch = deepcopy(branches[0])
                    slot = deepcopy(schema['$defs']['EditSlot'])
                    slot['properties']['content_type'] = {'type': 'string', 'enum': [kind]}
                    slot['properties']['paragraph_indices'] = ({'type': 'array',
                        'items': {'type': 'integer', 'enum': list(range(count))},
                        # Alibaba rejects uniqueItems; local validation checks duplicates.
                        'minItems': 1, 'maxItems': count}
                        if paragraph_mode else {'type': 'null'})
                    branch['properties']['slots'] = {'type': 'array', 'items': slot,
                        'minItems': 1, 'maxItems': count if paragraph_mode else 1}
                    variants.append(branch)
            schema['properties']['assignments']['properties'][oid] = {'anyOf': variants}

    def strict(node):
        if isinstance(node, dict):
            node.pop('default', None)
            if node.get('type') == 'object':
                node['additionalProperties'] = False
                node['required'] = list(node.get('properties', {}))
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)
    strict(schema)
    return schema


def parse_response(content, leaf_ids):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    data = json.loads(content, object_pairs_hook=unique)
    if not isinstance(data, dict) or not isinstance(data.get('assignments'), dict):
        raise ValueError('strict mode requires assignments as an object keyed by leaf IDs, not a list')
    assignments = data['assignments']
    missing = set(leaf_ids) - assignments.keys()
    extra = assignments.keys() - set(leaf_ids)
    if missing or extra:
        raise ValueError(f'assignments coverage: missing={sorted(missing)}, unknown={sorted(extra)}')
    converted = []
    for oid, value in assignments.items():
        if not isinstance(value, dict) or 'object_id' in value:
            raise ValueError(f'{oid}: assignment must be an object without object_id; ID is its dictionary key')
        converted.append(dict(value, object_id=oid))
    data['assignments'] = converted
    return AssignmentResponse.model_validate(data)
