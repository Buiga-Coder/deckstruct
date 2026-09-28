import json
import shutil
import uuid
from contextlib import contextmanager
import unittest
from pathlib import Path
from unittest.mock import patch
from io import BytesIO

from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from template_parser.extract import extract_template
from template_parser.schemas import SlideSemantics, validate_semantics
from template_parser.vlm import analyze_slide
from wire_fixture import wire_fixture


@contextmanager
def test_directory():
    workspace = Path(__file__).resolve().parents[1]
    root = workspace / "output" / ("test-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        if root.resolve().parent != (workspace / "output").resolve():
            raise ValueError("Unexpected cleanup path")
        shutil.rmtree(root)


class PipelineTests(unittest.TestCase):
    def test_extract_group_image_and_table(self):
        with test_directory() as directory:
            root = Path(directory)
            source = root / "fixture.pptx"
            prs = Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            group = slide.shapes.add_group_shape()
            group.shapes.add_textbox(0, 0, Inches(2), Inches(1)).text = "Example"
            image = BytesIO()
            Image.new("RGB", (2, 2), "white").save(image, format="PNG")
            image.seek(0)
            slide.shapes.add_picture(image, 0, 0, Inches(1), Inches(1))
            table = slide.shapes.add_table(2, 2, 0, 0, Inches(3), Inches(2)).table
            table.cell(0, 0).text = "Cell"
            prs.save(source)
            result = extract_template(source, root / "package")
            objects = result["slides"][0]["objects"]
            self.assertEqual([obj["kind"] for obj in objects], ["group", "shape", "image", "table"])
            self.assertEqual(objects[1]["parent_id"], objects[0]["id"])
            self.assertEqual(objects[3]["table"][0][0]["text"], "Cell")
            self.assertTrue((root / "package" / objects[2]["assets"][0]["path"]).exists())
            self.assertEqual(source.read_bytes(), (root / "package/source.pptx").read_bytes())
            with self.assertRaises(ValueError):
                extract_template(source, root / "package")

    def setUp(self):
        self.slide = {"id": "s1", "objects": [{"id": "s1_o2", "kind": "shape", "text": "Title",
                      "parent_id": None, "geometry": {}, "paragraphs": []}]}
        self.answer = {"slide_id": "s1", "slide_role": "cover", "usage": "generation_template",
                       "components": [{"id": "title", "type": "title", "members": ["s1_o2"],
                                       "slots": [{"object_id": "s1_o2", "role": "title", "content_type": "text"}],
                                       "preserve": []}], "unassigned": [], "needs_review": False}

    def test_reject_missing_coverage_and_invented_references(self):
        validate_semantics(SlideSemantics.model_validate(self.answer), self.slide)
        self.answer["components"] = []
        with self.assertRaises(ValueError):
            validate_semantics(SlideSemantics.model_validate(self.answer), self.slide)
        self.answer["unassigned"] = ["s1_o999"]
        self.answer["needs_review"] = True
        with self.assertRaises(ValueError):
            validate_semantics(SlideSemantics.model_validate(self.answer), self.slide)

    def test_reject_conflicting_edit_roles(self):
        self.answer["components"][0]["preserve"] = ["s1_o2"]
        with self.assertRaises(ValueError):
            validate_semantics(SlideSemantics.model_validate(self.answer), self.slide)

    def test_api_contract_with_mock_response(self):
        with test_directory() as directory:
            image = Path(directory) / "s1.png"
            Image.new("RGB", (2, 2)).save(image)
            response = BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(wire_fixture(self.answer))}}]}).encode())
            with patch("template_parser.vlm.urlopen", return_value=response) as call:
                result = analyze_slide(self.slide, image, {"base_url": "http://localhost:8000/v1", "model": "test"})
                self.assertEqual(result.slide_role, "cover")
                request = call.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(request.full_url, "http://localhost:8000/v1/chat/completions")
                self.assertTrue(body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
