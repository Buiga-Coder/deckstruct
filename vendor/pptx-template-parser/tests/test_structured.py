import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from PIL import Image
from test_pipeline import test_directory
from template_parser.structured import response_schema, parse_response
from template_parser.vlm import analyze_slide, ModelOutputError
from template_parser.api import APIError


class StructuredTests(TestCase):
    def test_alibaba_schema_omits_unique_items_but_duplicates_are_rejected(self):
        from template_parser.assignments import compile_assignments
        self.slide['objects'][0]['paragraphs'] = [{}, {}]
        schema = response_schema(['o1'], self.slide['objects'])
        self.assertNotIn('"uniqueItems"', json.dumps(schema))
        self.answer['components'] = [{'id': 'text', 'type': 'text'}]
        self.answer['assignments']['o1'] = dict(component_id='text', action='replace',
            slots=[dict(role='body', content_type='text', paragraph_indices=[0, 0])])
        with self.assertRaisesRegex(ValueError, 'invalid paragraph_indices'):
            compile_assignments(parse_response(json.dumps(self.answer), ['o1']), self.slide)

    def test_object_specific_slot_limits(self):
        objects=[dict(id='one',kind='shape',text='x',paragraphs=[{}]),
                 dict(id='many',kind='shape',text='x',paragraphs=[{},{}]),
                 dict(id='image',kind='image',text=None),
                 dict(id='decor',kind='shape',text=None)]
        props=response_schema([o['id'] for o in objects], objects)['properties']['assignments']['properties']
        for oid,count in [('one',1),('many',2)]:
            replace=[b for b in props[oid]['anyOf'] if b['properties']['action']['enum']==['replace']]
            self.assertEqual(len(replace),2)
            whole,paragraph= [b['properties']['slots'] for b in replace]
            self.assertEqual(whole['maxItems'],1)
            self.assertEqual(whole['items']['properties']['paragraph_indices']['type'],'null')
            self.assertEqual(paragraph['maxItems'],count)
            self.assertEqual(paragraph['items']['properties']['paragraph_indices']['items']['enum'], list(range(count)))
        self.assertEqual(len(props['decor']['anyOf']),2)
        image=props['image']['anyOf'][-1]['properties']['slots']
        self.assertEqual(image['items']['properties']['content_type']['enum'],['image'])
        self.assertEqual(image['maxItems'],1)

    def setUp(self):
        self.slide = {'id':'s1','objects':[dict(id='o1',kind='shape',text='Hi',
            paragraphs=[],geometry={},parent_id=None)]}
        self.answer = dict(slide_id='s1',slide_role='content',usage='unknown',components=[],
            assignments={'o1':dict(component_id=None,action='unassigned',slots=[])},
            patterns=[],constraints=[],needs_review=True)

    def test_schema_requires_all_ids_and_forbids_extras(self):
        schema=response_schema(['a','b'])
        self.assertEqual(schema['properties']['assignments']['required'],['a','b'])
        self.assertFalse(schema['properties']['assignments']['additionalProperties'])
        for branch in schema['$defs']['Assignment']['anyOf']:
            self.assertNotIn('object_id',branch['properties'])
        def check(node):
            if isinstance(node,dict):
                if node.get('type')=='object':
                    self.assertEqual(set(node['required']),set(node['properties']))
                    self.assertFalse(node['additionalProperties'])
                for v in node.values(): check(v)
            elif isinstance(node,list):
                for v in node: check(v)
        check(schema)

    def test_action_branches_constrain_component_and_slots(self):
        branches = response_schema(['o1'])['$defs']['Assignment']['anyOf']
        props = {b['properties']['action']['enum'][0]: b['properties'] for b in branches}
        for action in ('replace', 'preserve'):
            self.assertEqual(props[action]['component_id']['type'], 'string')
        self.assertEqual(props['unassigned']['component_id']['type'], 'null')
        self.assertEqual(props['replace']['slots']['minItems'], 1)
        self.assertEqual(props['preserve']['slots']['maxItems'], 0)
        self.assertEqual(props['unassigned']['slots']['maxItems'], 0)

    def test_preserve_null_is_still_rejected_locally(self):
        from template_parser.assignments import compile_assignments
        self.answer['assignments']['o1']['action'] = 'preserve'
        with self.assertRaisesRegex(ValueError, 'unknown component_id'):
            compile_assignments(parse_response(json.dumps(self.answer), ['o1']), self.slide)

    def test_missing_unknown_and_duplicate_keys_rejected(self):
        for assignments in [{},{'ghost':{}},[]]:
            with self.assertRaises(ValueError):
                parse_response(json.dumps(dict(self.answer,assignments=assignments)),['o1'])
        with self.assertRaisesRegex(ValueError,'duplicate JSON key'):
            parse_response('{"assignments":{"o1":{},"o1":{}}}', ['o1'])

    def test_request_and_public_conversion(self):
        with test_directory() as root:
            preview=root/'s1.png'; Image.new('RGB',(2,2)).save(preview)
            response=BytesIO(json.dumps({'choices':[{'finish_reason':'stop',
                'message':{'content':json.dumps(self.answer)}}]}).encode())
            with patch('template_parser.vlm.urlopen',return_value=response) as call:
                result=analyze_slide(self.slide,preview,dict(base_url='https://example.com/v1',
                    model='test',structured_outputs=True))
            payload=json.loads(call.call_args.args[0].data)
            self.assertTrue(payload['response_format']['json_schema']['strict'])
            schema=payload['response_format']['json_schema']['schema']
            self.assertEqual(schema['properties']['assignments']['required'],['o1'])
            self.assertEqual(result.unassigned,['o1'])
            self.assertNotIn('assignments',result.model_dump())

    def test_unsupported_api_does_not_fallback(self):
        with test_directory() as root:
            preview=root/'s1.png'; Image.new('RGB',(2,2)).save(preview)
            error=HTTPError('https://example.com',400,'unsupported schema',{},BytesIO(b'{}'))
            with patch('template_parser.vlm.urlopen',side_effect=error) as call:
                with self.assertRaises(APIError):
                    analyze_slide(self.slide,preview,dict(base_url='https://example.com/v1',
                        model='test',structured_outputs=True))
                self.assertEqual(call.call_count,1)
