import json
from unittest import TestCase
from unittest.mock import patch
from PIL import Image
from test_pipeline import test_directory
from template_parser.batch import run_batch
from template_parser.extract import sha256
from template_parser.schemas import SlideSemantics
from template_parser.api import APIError
from template_parser.vlm import ModelOutputError


class BatchTests(TestCase):
    def setup_package(self, root):
        (root/'source.pptx').write_bytes(b'fixture')
        (root/'previews').mkdir()
        slides = []
        for n in (1, 2, 3):
            sid = f's{n}'
            slides.append({'id': sid, 'objects': []})
            Image.new('RGB', (2, 2)).save(root/'previews'/f'{sid}.png')
        (root/'template.json').write_text(json.dumps({'source': {'sha256': sha256(b'fixture')}, 'slides': slides}), encoding='utf-8')

    def answer(self, slide, *args, **kwargs):
        return SlideSemantics(slide_id=slide['id'], slide_role='empty', usage='unknown', components=[], unassigned=[], needs_review=True)

    def test_resume_and_invalidation(self):
        with test_directory() as root:
            self.setup_package(root)
            with patch('template_parser.batch.analyze_slide', side_effect=self.answer) as call:
                first = run_batch(root, {'model': 'test'}, delay=0)
                self.assertEqual(first['counts'], {'completed': 3})
                second = run_batch(root, {'model': 'test'}, delay=0)
                self.assertEqual(second['counts'], {'skipped': 3})
                self.assertEqual(call.call_count, 3)
                path = root/'semantics/s2.json'
                data = json.loads(path.read_text())
                data['prompt_version'] = 'old'
                path.write_text(json.dumps(data))
                third = run_batch(root, {'model': 'test'}, delay=0)
                self.assertEqual(third['counts'], {'skipped': 2, 'completed': 1})
                self.assertEqual(call.call_count, 4)

    def test_bad_model_continues_api_failure_stops(self):
        with test_directory() as root:
            self.setup_package(root)
            with patch('template_parser.batch.analyze_slide', side_effect=[ModelOutputError('bad'), APIError('429')]) as call:
                report = run_batch(root, {'model': 'test'}, delay=0)
            self.assertEqual([r['status'] for r in report['slides']], ['error', 'api_error', 'pending'])
            self.assertEqual(call.call_count, 2)
            self.assertTrue((root/'analysis_report.json').exists())

    def test_interrupt_keeps_saved_results(self):
        with test_directory() as root:
            self.setup_package(root)
            with patch('template_parser.batch.analyze_slide', side_effect=[self.answer({'id':'s1'}), KeyboardInterrupt()]):
                report = run_batch(root, {'model':'test'}, delay=0)
            self.assertEqual(report['status'], 'interrupted')
            self.assertTrue((root/'semantics/s1.json').exists())
            self.assertEqual(report['slides'][1]['status'], 'interrupted')
