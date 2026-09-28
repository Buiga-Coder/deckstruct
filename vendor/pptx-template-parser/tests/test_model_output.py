import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from PIL import Image
from wire_fixture import wire_fixture
from test_pipeline import test_directory
from template_parser.vlm import analyze_slide, ModelOutputError, unwrap_json, completion_metadata, missing_edit_roles
from template_parser.schemas import SlideSemantics


class ModelOutputTests(TestCase):
    def test_metadata_excludes_reasoning_and_arbitrary_payload(self):
        result = completion_metadata({'usage': {'completion_tokens': 12,
            'completion_tokens_details': {'reasoning_tokens': 10}, 'private': 'secret'}},
            {'finish_reason': 'stop', 'message': {'reasoning': 'secret', 'content': None}})
        self.assertTrue(result['has_reasoning'])
        self.assertEqual(result['usage']['reasoning_tokens'], 10)
        self.assertNotIn('secret', json.dumps(result))

    def test_null_content_is_reported_without_repair(self):
        with self.assertRaisesRegex(ModelOutputError, 'completion metadata.*finish_reason'):
            self.run_case([None])

    def setUp(self):
        self.slide = {"id":"s1","objects":[{"id":"o1","kind":"shape","text":"Hello",
            "paragraphs":[{"text":"Hello"}],"geometry":{},"parent_id":None}]}
        self.good = {"slide_id":"s1","slide_role":"title","usage":"generation_template",
            "components":[{"id":"title","type":"title","members":["o1"],
                "slots":[{"object_id":"o1","role":"title","content_type":"text"}],"preserve":[]}],
            "unassigned":[],"needs_review":False}

    def response(self, text):
        if isinstance(text, str):
            fenced = text.strip().startswith('```json')
            try:
                parsed = json.loads(unwrap_json(text))
                if 'unassigned' in parsed:
                    text = json.dumps(wire_fixture(parsed))
                    if fenced:
                        text = '```json\n' + text + '\n```'
            except (ValueError, TypeError):
                pass
        return BytesIO(json.dumps({"choices":[{"finish_reason":"stop","message":{"content":text}}]}).encode())

    def run_case(self, replies, config=None):
        with test_directory() as root:
            preview=root/'s1.png'
            Image.new('RGB',(2,2)).save(preview)
            with patch('template_parser.vlm.urlopen',side_effect=[self.response(t) for t in replies]) as mock:
                result=analyze_slide(self.slide,preview,{"base_url":"http://localhost:8000/v1","model":"test",**(config or {})},rejected_dir=root/'rejected')
                return result,mock.call_count,len(list((root/'rejected').glob('*.json')))

    def test_fenced_json_is_accepted_without_repair(self):
        result,calls,rejected=self.run_case(['\n```json\n'+json.dumps(self.good)+'\n```'])
        self.assertEqual(result.slide_id,'s1')
        self.assertEqual((calls,rejected),(1,0))

    def test_invalid_shape_slot_requests_correction(self):
        bad=json.loads(json.dumps(self.good))
        bad['components'][0]['slots'][0]['content_type']='shape'
        result,calls,rejected=self.run_case([json.dumps(bad),json.dumps(self.good)])
        self.assertEqual((calls,rejected),(2,1))
        self.assertEqual(result.components[0].slots[0].content_type,'text')

    def test_missing_roles_are_reported_without_assigning_them(self):
        bad = json.loads(json.dumps(self.good))
        bad['components'][0]['members'].append('badge')
        parsed = SlideSemantics.model_validate(bad)
        before = parsed.model_dump()
        self.assertEqual(missing_edit_roles(parsed),
                         [{'component_id': 'title', 'object_ids': ['badge']}])
        self.assertEqual(parsed.model_dump(), before)
        self.assertEqual(missing_edit_roles(None), [])

    def test_unresolved_member_is_rejected_after_correction(self):
        bad = json.loads(json.dumps(self.good))
        bad['components'][0]['slots'] = []
        with self.assertRaisesRegex(ModelOutputError, 'replace requires nonempty slots'):
            self.run_case([json.dumps(bad), json.dumps(bad)])

    def test_invalid_json_stops_at_configured_limit(self):
        with self.assertRaises(ModelOutputError):
            self.run_case(['not JSON'],{'validation_retries':0})

    def test_prose_and_multiple_blocks_are_not_silently_extracted(self):
        for value in ['Here is JSON: {"x":1}', '```json\n{}\n```\nOther text']:
            self.assertEqual(unwrap_json(value),value)

    def test_semantic_invalid_reference_is_repaired(self):
        bad=json.loads(json.dumps(self.good))
        bad['components'][0]['members']=['unknown']
        result,calls,rejected=self.run_case([json.dumps(bad),json.dumps(self.good)])
        self.assertEqual((calls,rejected),(2,1))

    def test_groups_are_context_only_in_api_inventory(self):
        self.slide['objects'][0]['parent_id'] = 'g2'
        for object_id, parent_id in [('g1', None), ('g2', 'g1')]:
            self.slide['objects'].append({'id': object_id, 'kind': 'group',
                'parent_id': parent_id, 'geometry': {}, 'text': None, 'paragraphs': []})
        with test_directory() as root:
            preview = root / 's1.png'
            Image.new('RGB', (2, 2)).save(preview)
            with patch('template_parser.vlm.urlopen', return_value=self.response(json.dumps(self.good))) as mock:
                analyze_slide(self.slide, preview, {'base_url': 'http://localhost:8000/v1', 'model': 'test'})
                payload = json.loads(mock.call_args.args[0].data)
        inventory = json.loads(payload['messages'][1]['content'][0]['text'])
        self.assertEqual(inventory['allowed_member_ids'], ['o1'])
        self.assertEqual([obj['id'] for obj in inventory['objects']], ['o1'])
        groups = {obj['id']: obj for obj in inventory['groups_context_only']}
        self.assertEqual(groups['g1']['child_ids'], ['g2'])
        self.assertEqual(groups['g2']['child_ids'], ['o1'])
        self.assertEqual(inventory['objects'][0]['parent_id'], 'g2')

    def test_missing_assignment_feedback_and_public_result(self):
        bad = wire_fixture(self.good)
        bad['assignments'] = []
        with test_directory() as root:
            preview = root / 's1.png'
            Image.new('RGB', (2, 2)).save(preview)
            with patch('template_parser.vlm.urlopen', side_effect=[
                    self.response(json.dumps(bad)), self.response(json.dumps(self.good))]) as mock:
                result = analyze_slide(self.slide, preview,
                    {'base_url': 'http://localhost:8000/v1', 'model': 'test'})
                correction = json.loads(mock.call_args.args[0].data)['messages'][-1]['content']
        self.assertIn("missing assignments", correction)
        self.assertIn('o1', correction)
        self.assertEqual(result.components[0].members, ['o1'])
