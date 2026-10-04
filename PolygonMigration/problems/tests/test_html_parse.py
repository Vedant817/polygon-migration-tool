"""problem.html parsing used to build the reviewable problem fields."""

from django.test import SimpleTestCase

from problems.views import parse_problem_html
from problems.tests.polygon_stub import PROBLEM_HTML


class ParseProblemHtmlTests(SimpleTestCase):
    def setUp(self):
        self.data = parse_problem_html(PROBLEM_HTML)

    def test_title_is_extracted(self):
        self.assertEqual(self.data["title"], "Probe Sum")

    def test_legend_is_extracted_as_html(self):
        self.assertIn("<b>a</b>", self.data["legend"])
        self.assertIn("print their sum", self.data["legend"])

    def test_input_and_output_specifications_drop_their_section_titles(self):
        self.assertIn("two integers", self.data["input_format"])
        self.assertIn("a + b", self.data["output_format"])
        self.assertNotIn("section-title", self.data["input_format"])
        self.assertNotIn("section-title", self.data["output_format"])

    def test_notes_are_extracted(self):
        self.assertIn("32-bit", self.data["notes"])
        self.assertNotIn("section-title", self.data["notes"])

    def test_legend_keeps_its_section_title(self):
        self.assertIn("section-title", self.data["legend"])

    def test_missing_sections_yield_empty_strings_not_errors(self):
        data = parse_problem_html("<html><body><div class='title'>Bare</div></body></html>")
        self.assertEqual(data["title"], "Bare")
        self.assertEqual(data["legend"], "")
        self.assertEqual(data["input_format"], "")
        self.assertEqual(data["output_format"], "")
        self.assertEqual(data["notes"], "")