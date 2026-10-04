"""Tests for the allow-list sanitiser applied to Polygon problem content."""

from django.test import SimpleTestCase

from problems.html_sanitize import sanitize_html


class SanitizeKeepsFormattingTests(SimpleTestCase):
    def test_plain_paragraph_is_preserved(self):
        self.assertEqual(
            sanitize_html("<p>You are given two integers.</p>"),
            "<p>You are given two integers.</p>")

    def test_formatting_elements_survive(self):
        for markup in ("<b>bold</b>", "<strong>strong</strong>",
                       "<em>em</em>", "<code>int a;</code>",
                       "<pre>code block</pre>", "<ul><li>one</li></ul>",
                       "<table><tr><td>1</td></tr></table>"):
            with self.subTest(markup=markup):
                self.assertEqual(sanitize_html(markup), markup)

    def test_maths_delimiters_are_left_as_text(self):
        raw = "<p>Print $$$a+b$$$ on one line.</p>"
        self.assertIn("$$$a+b$$$", sanitize_html(raw))

    def test_unknown_but_harmless_tag_is_unwrapped_keeping_text(self):
        # A drop-with-content element would silently delete this sentence.
        for markup in ("<marquee>scroll me</marquee>",
                       "<button>press me</button>",
                       "<canvas>fallback text</canvas>"):
            with self.subTest(markup=markup):
                out = sanitize_html(markup)
                self.assertNotIn("<", out)
                self.assertTrue(out.strip())

    def test_none_and_blank_become_empty_string(self):
        self.assertEqual(sanitize_html(None), "")
        self.assertEqual(sanitize_html(""), "")
        # html.parser collapses runs of whitespace; only "no markup left" is asserted
        self.assertEqual(sanitize_html("   ").strip(), "")


class SanitizeRemovesScriptTests(SimpleTestCase):
    def test_script_element_and_its_body_are_removed(self):
        out = sanitize_html("<p>before</p><script>alert(1)</script><p>after</p>")
        self.assertNotIn("script", out)
        self.assertNotIn("alert(1)", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_event_handler_attribute_is_stripped(self):
        out = sanitize_html('<p onclick="alert(1)">text</p>')
        self.assertNotIn("onclick", out)
        self.assertIn("text", out)

    def test_image_onerror_is_stripped(self):
        out = sanitize_html('<img src=x onerror="alert(1)">')
        self.assertNotIn("onerror", out)
        self.assertNotIn("<img", out)

    def test_style_element_is_removed_with_its_body(self):
        out = sanitize_html("<style>body{display:none}</style><p>kept</p>")
        self.assertNotIn("display:none", out)
        self.assertIn("kept", out)

    def test_inline_style_attribute_is_stripped(self):
        out = sanitize_html('<p style="position:fixed;top:0">x</p>')
        self.assertNotIn("style", out)

    def test_svg_is_removed_with_its_contents(self):
        out = sanitize_html("<svg><script>alert(1)</script></svg><p>kept</p>")
        self.assertNotIn("svg", out)
        self.assertNotIn("alert(1)", out)
        self.assertIn("kept", out)

    def test_iframe_is_removed(self):
        out = sanitize_html('<iframe src="http://evil.test"></iframe><p>kept</p>')
        self.assertNotIn("iframe", out)
        self.assertIn("kept", out)

    def test_form_controls_are_removed(self):
        out = sanitize_html('<form action="/x"><input name="a"><button>go</button></form>')
        self.assertNotIn("<form", out)
        self.assertNotIn("<input", out)
        self.assertNotIn("<button", out)


class SanitizeHrefTests(SimpleTestCase):
    def test_javascript_scheme_is_removed(self):
        out = sanitize_html('<a href="javascript:alert(1)">click</a>')
        self.assertNotIn("javascript:", out)
        self.assertIn("click", out)

    def test_entity_encoded_javascript_scheme_is_removed(self):
        out = sanitize_html('<a href="java&#115;cript:alert(1)">click</a>')
        self.assertNotIn("cript:alert", out)

    def test_data_scheme_is_removed(self):
        out = sanitize_html('<a href="data:text/html;base64,PHNjcmlwdD4=">click</a>')
        self.assertNotIn("data:", out)

    def test_whitespace_padded_javascript_scheme_is_removed(self):
        out = sanitize_html('<a href="  javascript:alert(1)  ">click</a>')
        self.assertNotIn("javascript:", out)

    def test_relative_and_http_links_are_kept(self):
        for href in ("/problems/1", "https://algopath.ai/x", "http://example.test",
                     "mailto:a@b.test"):
            with self.subTest(href=href):
                self.assertIn(href, sanitize_html('<a href="%s">go</a>' % href))

    def test_target_attribute_is_stripped(self):
        out = sanitize_html('<a href="/x" target="_blank">go</a>')
        self.assertNotIn("target", out)


class SanitizeTableAttributeTests(SimpleTestCase):
    def test_colspan_and_rowspan_are_kept(self):
        out = sanitize_html('<table><tr><td colspan="2" rowspan="3">x</td></tr></table>')
        self.assertIn('colspan="2"', out)
        self.assertIn('rowspan="3"', out)

    def test_style_on_a_table_cell_is_stripped(self):
        out = sanitize_html('<table><tr><td style="color:red" colspan="2">x</td></tr></table>')
        self.assertNotIn("color:red", out)
        self.assertIn('colspan="2"', out)


class RealPolygonContentTests(SimpleTestCase):
    """Content shaped like what parse_problem_html actually returns."""

    def test_codeforces_style_statement_is_readable_not_markup(self):
        raw = ("<p>You are given two integers $$$a$$$ and $$$b$$$. "
               "Print $$$a+b$$$.</p>")
        out = sanitize_html(raw)
        self.assertTrue(out.startswith("<p>"))
        self.assertIn("You are given two integers", out)

    def test_nested_markup_is_filtered_not_flattened(self):
        raw = ('<div class="problem-statement">'
               '<p>Read <a href="https://example.test/x">this</a>.</p>'
               '<script>steal()</script>'
               '<pre><code>int main(){}</code></pre>'
               '</div>')
        out = sanitize_html(raw)
        self.assertIn("<p>Read", out)
        self.assertIn('href="https://example.test/x"', out)
        self.assertIn("<pre><code>", out)
        self.assertNotIn("steal()", out)
        self.assertNotIn('class="problem-statement"', out)
