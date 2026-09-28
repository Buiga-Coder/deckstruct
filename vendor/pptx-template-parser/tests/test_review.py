import json
from copy import deepcopy
from unittest import TestCase

from test_pipeline import test_directory
from template_parser.schemas import SlideSemantics, validate_semantics
from template_parser.geometry import object_polygon
from template_parser.review import create_review
from template_parser.extract import sha256
from PIL import Image


class ParagraphTests(TestCase):
    def setUp(self):
        self.slide = {"id":"s1", "objects":[{"id":"o1", "kind":"shape", "text":"Title\nBody", "paragraphs":[{"text":"Title"},{"text":"Body"}], "parent_id":None,
                       "geometry":{"x":0,"y":0,"width":100,"height":50,"rotation":0}}]}
        self.data = {"slide_id":"s1","slide_role":"card","usage":"generation_template","components":[{"id":"card","type":"card","members":["o1"],"slots":[
                    {"object_id":"o1","role":"title","content_type":"text","paragraph_indices":[0]},
                    {"object_id":"o1","role":"body","content_type":"text","paragraph_indices":[1]}],"preserve":[]}],"unassigned":[],"needs_review":False}

    def test_disjoint_and_legacy_slots(self):
        validate_semantics(SlideSemantics.model_validate(self.data),self.slide)
        self.data['components'][0]['slots'] = [{"object_id":"o1","role":"body","content_type":"text"}]
        validate_semantics(SlideSemantics.model_validate(self.data), self.slide)

    def test_reject_overlap_whole_and_invalid_indices(self):
        for target in ([0], [1,1], [-1], [2], None):
            with self.subTest(target=target):
                data=deepcopy(self.data)
                data['components'][0]['slots'][1]['paragraph_indices']=target
                with self.assertRaises(ValueError):
                    validate_semantics(SlideSemantics.model_validate(data), self.slide)

    def test_offline_review_escapes_text_and_verifies_hash(self):
        with test_directory() as root:
            (root/'previews').mkdir(); (root/'semantics').mkdir()
            Image.new('RGB',(200,100)).save(root/'previews/s1.png')
            manifest={'source':{'sha256':'source'},'slide_size':{'width':100,'height':50},'slides':[self.slide]}
            envelope={'source_sha256':'source','preview_sha256':sha256((root/'previews/s1.png').read_bytes()),'result':self.data}
            self.slide['objects'][0]['paragraphs'][0]['text']='<script>alert(1)</script>'
            (root/'template.json').write_text(json.dumps(manifest),encoding='utf-8')
            (root/'semantics/s1.json').write_text(json.dumps(envelope),encoding='utf-8')
            content=create_review(root,1).read_text(encoding='utf-8')
            self.assertIn('&lt;script&gt;',content)
            self.assertIn('data:image/png;base64,',content)
            self.assertIn('p1',content)
            self.assertNotIn('<script>alert(1)</script>',content)
            envelope['preview_sha256']='wrong'
            (root/'semantics/s1.json').write_text(json.dumps(envelope),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                create_review(root,1)


class GeometryTests(TestCase):
    def test_nested_group_scaling_and_translation(self):
        def group(oid, parent, off, extent, child_off, child_extent):
            return {'id':oid,'parent_id':parent,'xml':f'<p:grpSp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:grpSpPr><a:xfrm><a:off x="{off}" y="{off}"/><a:ext cx="{extent}" cy="{extent}"/><a:chOff x="{child_off}" y="{child_off}"/><a:chExt cx="{child_extent}" cy="{child_extent}"/></a:xfrm></p:grpSpPr></p:grpSp>'}
        objects={'g1':group('g1',None,100,200,0,100),'g2':group('g2','g1',10,20,0,10)}
        obj={'id':'o','parent_id':'g2','geometry':{'x':0,'y':0,'width':5,'height':5,'rotation':0}}
        self.assertEqual(object_polygon(obj,objects),[(120,120),(140,120),(140,140),(120,140)])

    def test_rotated_rectangle(self):
        obj={'id':'o','parent_id':None,'geometry':{'x':0,'y':0,'width':10,'height':10,'rotation':90}}
        points=object_polygon(obj,{})
        self.assertAlmostEqual(points[0][0],10)
        self.assertAlmostEqual(points[0][1],0)
