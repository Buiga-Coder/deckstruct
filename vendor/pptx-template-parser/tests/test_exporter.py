import json
import zipfile
from unittest import TestCase
from unittest.mock import patch
from test_pipeline import test_directory
import test_batch
from template_parser.batch import run_batch
from template_parser.exporter import export_package
from template_parser.contracts import check_package


class ExportTests(TestCase):
    def test_partial_export_without_secrets_and_no_overwrite(self):
        with test_directory() as root:
            package = root/'input'
            package.mkdir()
            fixture = test_batch.BatchTests()
            fixture.setup_package(package)
            (package/'config.local.json').write_text('SECRET')
            with patch('template_parser.batch.analyze_slide', side_effect=fixture.answer):
                run_batch(package, {'model':'test'}, delay=0)
            output = root/'export'
            result = export_package(package, output)
            self.assertEqual(result['exported'], 3)
            catalog = json.loads((output/'catalog.json').read_text())
            self.assertFalse(catalog['partial'])
            self.assertEqual(len(catalog['slides']), 3)
            self.assertTrue(catalog['slides'][0]['needs_review'])
            self.assertNotIn('policy_version', catalog)
            with zipfile.ZipFile(result['archive']) as z:
                self.assertIsNone(z.testzip())
                self.assertNotIn('config.local.json', z.namelist())
                self.assertIn('checksums.json', z.namelist())
            with self.assertRaises(ValueError):
                export_package(package, output)
            self.assertEqual(check_package(output)['counts'], {'valid': 3})
            (output/'source.pptx').write_bytes(b'corrupted')
            with self.assertRaises(ValueError):
                check_package(output)
