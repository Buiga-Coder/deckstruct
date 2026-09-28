import os
import shutil
import subprocess
from unittest import TestCase, skipUnless
from unittest.mock import patch
from PIL import Image
from pptx import Presentation
from test_pipeline import test_directory
from template_parser.extract import extract_template
from template_parser.render import render_package


class LibreOfficeTests(TestCase):
    def test_conversion_and_cleanup(self):
        with test_directory() as root:
            source=root/'input.pptx'
            prs=Presentation(); prs.slides.add_slide(prs.slide_layouts[6]); prs.save(source)
            package=root/'package'; extract_template(source,package)
            calls=[]
            def command(args, **kwargs):
                calls.append(args)
                if '--convert-to' in args:
                    from pathlib import Path
                    Path(args[args.index('--outdir')+1],'source.pdf').write_bytes(b'pdf')
                else:
                    Image.new('RGB',(800,600)).save(args[-1]+'-1.png')
                return subprocess.CompletedProcess(args,0)
            # Mock the POSIX Popen path on Linux, subprocess.run path on Windows.
            from unittest.mock import Mock
            def popen(args, **kwargs):
                result=command(args)
                proc=Mock(returncode=result.returncode)
                proc.communicate.return_value=(b'',b'')
                return proc
            with patch('template_parser.render.shutil.which', side_effect=lambda name:name), patch('template_parser.render.subprocess.run',side_effect=command), patch('template_parser.render.subprocess.Popen',side_effect=popen):
                result=render_package(package,800,renderer='libreoffice')
            self.assertEqual(result['engine']['slide_count'],1)
            self.assertIn('ExportHiddenSlides',calls[0][calls[0].index('--convert-to')+1])
            self.assertFalse((package/'previews/conversion').exists())

    @skipUnless(os.name!='nt' and shutil.which('soffice') and shutil.which('pdftoppm'), 'Requires Linux LibreOffice and Poppler')
    def test_real_linux_renderer(self):
        with test_directory() as root:
            prs=Presentation()
            prs.slides.add_slide(prs.slide_layouts[6])
            hidden=prs.slides.add_slide(prs.slide_layouts[6]); hidden._element.set('show','0')
            source=root/'input.pptx'; prs.save(source)
            package=root/'package'; extract_template(source,package)
            report=render_package(package,800,renderer='libreoffice')
            self.assertEqual(len(report['slides']),2)
