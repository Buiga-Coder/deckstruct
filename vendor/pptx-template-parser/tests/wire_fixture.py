def wire_fixture(value):
    """Convert legacy test fixtures only, never production model responses."""
    assignments = []
    for c in value['components']:
        for oid in c['members']:
            slots = [{k: v for k, v in s.items() if k != 'object_id'}
                     for s in c.get('slots', []) if s['object_id'] == oid]
            assignments.append(dict(object_id=oid, component_id=c['id'],
                action='preserve' if oid in c.get('preserve', []) else 'replace', slots=slots))
    assignments.extend(dict(object_id=o, component_id=None, action='unassigned', slots=[])
                       for o in value['unassigned'])
    return {**{k: v for k, v in value.items() if k not in ('components', 'unassigned')},
            'components': [{k: c[k] for k in ('id', 'type')} for c in value['components']],
            'assignments': assignments}
