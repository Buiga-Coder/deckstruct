from unittest import TestCase
from template_parser.composition import compose_slide


class CompositionTests(TestCase):
    def fixture(self):
        slide = {'id': 's1', 'layout_id': 'l1', 'objects': [{'id': 's1_title', 'kind': 'shape'}]}
        layout = {'id': 'l1', 'master_id': 'm1', 'objects': [
            {'id': 'logo', 'kind': 'image'},
            {'id': 'placeholder', 'kind': 'shape', 'placeholder': {'idx': 0}}]}
        master = {'id': 'm1', 'objects': [{'id': 'decoration', 'kind': 'shape'}]}
        return {'layouts': [layout], 'masters': [master]}, slide

    def test_inherited_logo_and_no_duplicate_placeholders(self):
        manifest, slide = self.fixture()
        result = compose_slide(manifest, slide)
        self.assertEqual([o['id'] for o in result['objects']], ['decoration', 'logo', 's1_title'])
        self.assertEqual(result['objects'][1]['origin']['scope'], 'layout')
        self.assertEqual(len(slide['objects']), 1)

    def test_hide_master_graphics_at_both_levels(self):
        manifest, slide = self.fixture()
        manifest['layouts'][0]['xml'] = '<layout showMasterSp="false"/>'
        self.assertEqual([o['id'] for o in compose_slide(manifest, slide)['objects']], ['logo', 's1_title'])
        slide['xml'] = '<slide showMasterSp="0"/>'
        self.assertEqual([o['id'] for o in compose_slide(manifest, slide)['objects']], ['s1_title'])

    def test_hidden_group_children_excluded(self):
        manifest, slide = self.fixture()
        slide['objects'] += [
            {'id': 'g', 'kind': 'group', 'xml': '<p:grpSp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:nvGrpSpPr><p:cNvPr hidden="1"/></p:nvGrpSpPr></p:grpSp>'},
            {'id': 'child', 'kind': 'image', 'parent_id': 'g'}]
        ids = [o['id'] for o in compose_slide(manifest, slide)['objects']]
        self.assertNotIn('g', ids)
        self.assertNotIn('child', ids)
