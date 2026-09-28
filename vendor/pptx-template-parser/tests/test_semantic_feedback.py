import unittest

from template_parser.schemas import SlideSemantics, validate_semantics


class SemanticFeedbackTests(unittest.TestCase):
    def test_reports_all_conflicts_in_one_pass(self):
        slide = {"id": "s5", "objects": [
            {"id": "text", "kind": "shape", "text": "Title", "paragraphs": [{}]},
            {"id": "badge", "kind": "shape", "text": None},
            {"id": "missing", "kind": "image"},
            {"id": "group", "kind": "group"},
        ]}
        result = SlideSemantics.model_validate({
            "slide_id": "s5", "slide_role": "content", "usage": "generation_template",
            "components": [{"id": "card", "type": "card",
                "members": ["text", "badge", "group"],
                "slots": [{"object_id": "text", "role": "title", "content_type": "text"}]}],
            "unassigned": ["text"], "needs_review": False,
        })
        with self.assertRaises(ValueError) as caught:
            validate_semantics(result, slide)
        message = str(caught.exception)
        for fragment in ["group containers", "badge", "already assigned", "missing leaf objects", "missing", "needs_review must be true"]:
            self.assertIn(fragment, message)

    def test_unknown_slot_does_not_crash_validation(self):
        result = SlideSemantics.model_validate({
            "slide_id": "s1", "slide_role": "content", "usage": "generation_template",
            "components": [{"id": "card", "type": "card", "members": ["ghost"],
                "slots": [{"object_id": "ghost", "role": "title", "content_type": "text"}]}],
            "unassigned": [], "needs_review": False,
        })
        with self.assertRaisesRegex(ValueError, "unknown slot object.*ghost"):
            validate_semantics(result, {"id": "s1", "objects": []})
