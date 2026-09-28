import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
from PIL import Image
from test_pipeline import test_directory
from template_parser.vlm import analyze_slide, PROMPT_VERSION
from wire_fixture import wire_fixture


class MixedContentPromptTests(TestCase):
    def test_original_content_and_universal_instruction_sent_for_each_input(self):
        for texts in [('Insert heading here',), ('Quarterly sales results',),
                      ('Insert heading here', 'Revenue increased by 20%')]:
            with self.subTest(texts=texts), test_directory() as root:
                objects = [dict(id=f'o{i}',kind='shape',parent_id=None,geometry={},text=t,
                                paragraphs=[{'text':t}]) for i,t in enumerate(texts)]
                answer = {'slide_id':'s1','slide_role':'content','usage':'generation_template',
                          'components':[{'id':f'c{i}','type':'text','members':[o['id']],
                           'slots':[{'object_id':o['id'],'role':'body','content_type':'text'}]}
                           for i,o in enumerate(objects)], 'unassigned':[], 'needs_review':False}
                preview=root/'s1.png'
                Image.new('RGB',(2,2)).save(preview)
                response=BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(wire_fixture(answer))}}]}).encode())
                with patch('template_parser.vlm.urlopen', return_value=response) as call:
                    analyze_slide({'id':'s1','objects':objects},preview,{'base_url':'http://localhost:8000/v1','model':'mock'})
                payload=json.loads(call.call_args.args[0].data)
                prompt=payload['messages'][0]['content']
                self.assertIn('empty, fully populated, or mixed',prompt)
                self.assertIn('Do not turn all text or images into slots',prompt)
                inventory=json.loads(payload['messages'][1]['content'][0]['text'])
                self.assertEqual([o['text'] for o in inventory['objects']],list(texts))
                self.assertEqual(PROMPT_VERSION,'decompose_v11')
