import copy
import unittest
from template_parser.assignments import AssignmentResponse, compile_assignments


class AssignmentTests(unittest.TestCase):
    def test_misplaced_review_flag_has_compact_actionable_feedback(self):
        from pydantic import ValidationError
        from template_parser.vlm import validation_feedback
        self.answer['assignments'][2]['needs_review'] = True
        self.answer['assignments'][0]['needs_review'] = False
        with self.assertRaises(ValidationError) as caught:
            AssignmentResponse.model_validate(self.answer)
        feedback = validation_feedback(caught.exception)
        self.assertIn('Remove needs_review from ALL assignments', feedback)
        self.assertIn('root needs_review=true', feedback)
        self.assertIn('Found 2 misplaced fields', feedback)

    def setUp(self):
        self.slide = {'id': 's1', 'objects': [
            {'id': 'text', 'kind': 'shape', 'text': 'Title\nBody', 'paragraphs': [{}, {}]},
            {'id': 'badge', 'kind': 'shape', 'text': None},
            {'id': 'photo', 'kind': 'image', 'text': None},
            {'id': 'group', 'kind': 'group'}]}
        self.answer = dict(slide_id='s1', slide_role='content', usage='generation_template',
            components=[dict(id='card', type='content_card')], needs_review=True,
            assignments=[dict(object_id='text', component_id='card', action='replace', slots=[
                dict(role='title', content_type='text', paragraph_indices=[0]),
                dict(role='body', content_type='text', paragraph_indices=[1])]),
                dict(object_id='badge', component_id='card', action='preserve', slots=[]),
                dict(object_id='photo', component_id=None, action='unassigned', slots=[])])

    def compile(self, answer=None):
        return compile_assignments(AssignmentResponse.model_validate(answer or self.answer), self.slide)

    def test_public_contract_and_no_mutation(self):
        before = copy.deepcopy(self.answer)
        result = self.compile()
        self.assertEqual(result.components[0].members, ['text', 'badge'])
        self.assertEqual(result.components[0].preserve, ['badge'])
        self.assertEqual([s.object_id for s in result.components[0].slots], ['text', 'text'])
        self.assertEqual(result.unassigned, ['photo'])
        self.assertNotIn('assignments', result.model_dump())
        self.assertEqual(self.answer, before)

    def test_rejects_missing_duplicate_unknown_and_group(self):
        for oid in ['text', 'ghost', 'group']:
            bad = copy.deepcopy(self.answer)
            bad['assignments'].append(dict(object_id=oid, component_id=None, action='unassigned'))
            with self.assertRaises(ValueError):
                self.compile(bad)
        self.answer['assignments'].pop()
        with self.assertRaisesRegex(ValueError, 'missing assignments.*photo'):
            self.compile()

    def test_rejects_invalid_actions_components_and_slot_types(self):
        for change in [dict(action='preserve'), dict(component_id='ghost'),
                       dict(slots=[]), dict(slots=[dict(role='x', content_type='image')]),
                       dict(slots=[dict(role='x', content_type='text', paragraph_indices=[99])]),
                       dict(slots=[dict(role='x', content_type='text'), dict(role='y', content_type='text')])]:
            bad = copy.deepcopy(self.answer)
            bad['assignments'][0].update(change)
            with self.assertRaises(ValueError):
                self.compile(bad)
        for extra in [dict(id='card', type='card')]:
            bad = copy.deepcopy(self.answer)
            bad['components'].append(extra)
            with self.assertRaises(ValueError):
                self.compile(bad)

    def test_unassigned_requires_review_and_null_component(self):
        self.answer['needs_review'] = False
        with self.assertRaisesRegex(ValueError, 'needs_review'):
            self.compile()
        self.answer['needs_review'] = True
        self.answer['assignments'][-1]['component_id'] = 'card'
        with self.assertRaisesRegex(ValueError, 'component_id=null'):
            self.compile()

    def test_unused_components_removed_with_audit(self):
        self.answer['components'].append(dict(id='unused',type='icon'))
        audit=[]
        parsed=AssignmentResponse.model_validate(self.answer)
        result=compile_assignments(parsed,self.slide,normalizations=audit)
        self.assertEqual([c.id for c in result.components],['card'])
        self.assertEqual(audit[0]['component_ids'],['unused'])
        self.assertEqual(len(parsed.components),2)
