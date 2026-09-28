import json
from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile
from unittest import TestCase
from unittest.mock import patch

from PIL import Image
from pptx import Presentation
from pptx.oxml.xmlchemy import OxmlElement
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.util import Inches

from test_pipeline import test_directory
from template_parser.extract import extract_template
from template_parser.render import render_package, validate_previews


class AssetTests(TestCase):
    def test_dedup_crop_transparency_groups_master_and_background(self):
        with test_directory() as root:
            prs = Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            blob = BytesIO()
            Image.new("RGBA", (40, 20), (10, 20, 30, 120)).save(blob, "PNG")
            blob.seek(0)
            pic = slide.shapes.add_picture(blob, 0, 0)
            pic.crop_left = 0.25
            pic._element.spPr.xfrm.set("flipH", "1")
            group = slide.shapes.add_group_shape()
            blob.seek(0)
            group.shapes.add_picture(blob, Inches(1), Inches(1))
            rid = pic._element.blipFill.blip.rEmbed
            # Native image-filled background.
            bg = OxmlElement("p:bg")
            bg_pr = OxmlElement("p:bgPr")
            bg_pr.append(deepcopy(pic._element.blipFill))
            bg.append(bg_pr)
            slide._element.cSld.insert(0, bg)
            # Same image declared by a slide master.
            master = prs.slide_masters[0]
            master_rid = master.part.relate_to(slide.part.related_part(rid), RT.IMAGE)
            master_pic = deepcopy(pic._element)
            master_pic.nvPicPr.cNvPr.set("id", "777")
            master_pic.blipFill.blip.rEmbed = master_rid
            master.shapes._spTree.append(master_pic)
            source = root / "fixture.pptx"
            prs.save(source)
            with ZipFile(source, "a") as archive:
                archive.writestr("ppt/media/duplicate.png", blob.getvalue())
                archive.writestr("ppt/media/unsupported.bin", b"not an image")
            manifest = extract_template(source, root / "package")
            catalog = list(manifest["asset_catalog"].values())
            self.assertEqual(len(catalog), 2)
            image = next(a for a in catalog if a["format"] == "PNG")
            self.assertEqual((image["width_px"], image["height_px"]), (40, 20))
            self.assertTrue(image["has_transparency"])
            self.assertEqual(len(image["source_parts"]), 2)
            self.assertEqual(len(image["usages"]), 4)
            self.assertTrue(any(u["usage_type"] == "background" for u in image["usages"]))
            self.assertTrue(any(u["scope"] == "master" for u in image["usages"]))
            self.assertTrue(any(u["parent_id"] for u in image["usages"]))
            self.assertTrue(any(u["crop"]["l"] == 0.25 and u["flip_h"] for u in image["usages"]))
            self.assertTrue((root / "package" / image["preview_path"]).exists())
            self.assertTrue(any(a["preview_status"] == "unsupported" for a in catalog))


class RenderTests(TestCase):
    def make_package(self, root):
        prs = Presentation()
        prs.slides.add_slide(prs.slide_layouts[6])
        source = root / "input.pptx"
        prs.save(source)
        package = root / "package"
        extract_template(source, package)
        return package

    @staticmethod
    def fake_engine(source, destination, width, height, timeout):
        Image.new("RGB", (width, height), "white").save(destination / "s1.png")
        return {"name": "test", "version": "1", "slide_count": 1}

    def test_publish_verified_batch_and_refuse_overwrite(self):
        with test_directory() as root:
            package = self.make_package(root)
            original = (package / "source.pptx").read_bytes()
            with patch("template_parser.render._powerpoint", side_effect=self.fake_engine):
                report = render_package(package, width=800, renderer='powerpoint')
                self.assertEqual(report["slides"][0]["height_px"], 600)
                self.assertTrue((package / "previews/s1.png").exists())
                self.assertEqual(json.loads((package / "render_manifest.json").read_text())["engine"]["name"], "test")
                with self.assertRaises(ValueError):
                    render_package(package, renderer='powerpoint')
            self.assertEqual((package / "source.pptx").read_bytes(), original)

    def test_wrong_source_fails_before_engine(self):
        with test_directory() as root:
            package = self.make_package(root)
            (package / "source.pptx").write_bytes(b"changed")
            with patch("template_parser.render._powerpoint") as engine:
                with self.assertRaises(ValueError):
                    render_package(package, renderer='powerpoint')
                engine.assert_not_called()

    def test_partial_render_does_not_publish(self):
        with test_directory() as root:
            package = self.make_package(root)
            with patch("template_parser.render._powerpoint", return_value={"slide_count": 1}):
                with self.assertRaises(ValueError):
                    render_package(package, renderer='powerpoint')
            self.assertFalse((package / "render_manifest.json").exists())
            self.assertFalse((package / "previews").exists())
            self.assertFalse(list(package.glob(".render-*")))

    def test_reject_wrong_dimensions(self):
        with test_directory() as root:
            Image.new("RGB", (12, 12)).save(root / "s1.png")
            with self.assertRaises(ValueError):
                validate_previews(root, [{"id": "s1", "index": 1}], 800, 600)
