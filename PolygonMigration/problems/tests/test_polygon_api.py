"""PolygonAPI client behaviour: signing, envelope parsing, failure handling."""

from unittest import mock

from django.test import SimpleTestCase

from problems.polygon_api import REQUEST_TIMEOUT, PolygonAPI
from problems.tests import polygon_stub as stub


class ApiSigTests(SimpleTestCase):
    """apiSig proves the caller knows the secret; verify it against the spec.

    Polygon: ``<rand>/<method>?sorted-params#<secret>``, SHA-512 hex, where the
    params include apiKey and time and exclude apiSig.
    """

    def setUp(self):
        self.api = PolygonAPI()

    def test_signature_is_six_random_chars_plus_sha512_hex(self):
        sig, _ = self.api._generate_api_sig("problem.info", {"problemId": "123"})
        rand, digest = sig[:6], sig[6:]
        self.assertEqual(len(sig), 6 + 128, "6 random chars + 128 hex chars of SHA-512")
        self.assertTrue(all(c.islower() or c.isdigit() for c in rand))
        int(digest, 16)  # raises if not hexadecimal

    def test_two_signatures_differ_because_rand_is_random(self):
        first, _ = self.api._generate_api_sig("problem.info", {"problemId": "1"})
        second, _ = self.api._generate_api_sig("problem.info", {"problemId": "1"})
        self.assertNotEqual(first, second)

    def test_digest_matches_the_documented_algorithm(self):
        import hashlib
        from urllib.parse import urlencode

        sent = {}
        fake = mock.Mock(return_value=stub.FakeResponse(
            json_data={"status": "OK", "result": {}}))
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            PolygonAPI()._make_request("problem.info",
                                       {"zeta": "1", "alpha": "2", "problemId": "9"})
        sent = fake.call_args.kwargs["data"]

        rand = sent["apiSig"][:6]
        canonical = urlencode(sorted({
            "alpha": "2", "zeta": "1", "problemId": "9",
            "apiKey": self.api.api_key, "time": sent["time"],
        }.items()))
        expected = hashlib.sha512(
            f"{rand}/problem.info?{canonical}#{self.api.api_secret}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(sent["apiSig"][6:], expected)

    def test_sent_payload_carries_apikey_and_but_not_the_secret(self):
        fake = mock.Mock(return_value=stub.FakeResponse(
            json_data={"status": "OK", "result": {}}))
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            PolygonAPI()._make_request("problem.info", {"problemId": "7"})
        sent = fake.call_args.kwargs["data"]
        self.assertEqual(sent["apiKey"], self.api.api_key)
        self.assertIn("apiSig", sent)
        self.assertIn("time", sent)
        self.assertNotIn(self.api.api_secret, "".join(str(v) for v in sent.values()))

    def test_caller_params_are_not_mutated(self):
        fake = mock.Mock(return_value=stub.FakeResponse(
            json_data={"status": "OK", "result": {}}))
        params = {"problemId": "7"}
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            PolygonAPI()._make_request("problem.info", params)
        self.assertEqual(params, {"problemId": "7"})


class RequestTests(SimpleTestCase):
    def setUp(self):
        self.patcher = mock.patch("problems.polygon_api.requests.post")
        self.post = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_ok_envelope_returns_result(self):
        self.post.return_value = stub.FakeResponse(json_data={"status": "OK", "result": {"name": "x"}})
        self.assertEqual(PolygonAPI()._make_request("problem.info", {}), {"name": "x"})

    def test_failed_envelope_raises_with_comment(self):
        self.post.return_value = stub.FakeResponse(
            json_data={"status": "FAILED", "comment": "problem not found"})
        with self.assertRaises(Exception) as ctx:
            PolygonAPI()._make_request("problem.info", {"problemId": "1"})
        self.assertIn("problem not found", str(ctx.exception))

    def test_plain_request_returns_text_not_json(self):
        self.post.return_value = stub.FakeResponse(text="1 2\n3\n")
        self.assertEqual(PolygonAPI()._make_plain_request("problem.testAnswer", {}), "1 2\n3\n")

    def test_plain_request_does_not_return_the_failure_envelope_as_content(self):
        """A FAILED answer must raise, not become the test's expected output."""
        self.post.return_value = stub.FakeResponse(
            json_data={"status": "FAILED", "comment": "no access to test 4"})
        with self.assertRaises(Exception) as ctx:
            PolygonAPI()._make_plain_request("problem.testAnswer", {"testIndex": "4"})
        self.assertIn("no access to test 4", str(ctx.exception))

    def test_plain_request_that_legitimately_looks_like_json_is_returned_as_is(self):
        self.post.return_value = stub.FakeResponse(text='{"a": 1}')
        self.assertEqual(PolygonAPI()._make_plain_request("problem.script", {}), '{"a": 1}')

    def test_plain_request_json_with_status_ok_is_not_treated_as_a_failure(self):
        """A test file whose contents happen to be JSON must survive untouched."""
        payload = '{"status": "OK", "result": 7}'
        self.post.return_value = stub.FakeResponse(text=payload)
        self.assertEqual(PolygonAPI()._make_plain_request("problem.testAnswer", {}), payload)

    def test_plain_request_with_empty_body_returns_empty_string(self):
        self.post.return_value = stub.FakeResponse(text="")
        self.assertEqual(PolygonAPI()._make_plain_request("problem.testInput", {}), "")

    def test_http_error_is_wrapped(self):
        self.post.return_value = stub.FakeResponse(
            json_data={"status": "FAILED"}, status_code=403)
        with self.assertRaises(Exception) as ctx:
            PolygonAPI()._make_request("problem.info", {})
        self.assertIn("HTTP Request Error", str(ctx.exception))

    def test_requests_always_carry_a_timeout(self):
        self.post.return_value = stub.FakeResponse(json_data={"status": "OK", "result": {}})
        PolygonAPI()._make_request("problem.info", {})
        self.assertEqual(self.post.call_args.kwargs.get("timeout"), REQUEST_TIMEOUT)

    def test_get_test_cases_swallows_errors_and_returns_empty_list(self):
        self.post.return_value = stub.FakeResponse(
            json_data={"status": "FAILED", "comment": "denied"})
        self.assertEqual(PolygonAPI().get_test_cases("1"), [])

    def test_get_problem_info_propagates_failure(self):
        self.post.return_value = stub.FakeResponse(
            json_data={"status": "FAILED", "comment": "denied"})
        with self.assertRaises(Exception):
            PolygonAPI().get_problem_info("1")


class TestContentRetrievalTests(SimpleTestCase):
    """The listing endpoint carries no test contents; they must be fetched."""

    def test_all_test_cases_fetches_contents_per_index(self):
        tests = stub.default_tests(sample_count=3, regular_count=12)
        fake = stub.PolygonStub(tests=tests)
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            cases = PolygonAPI().get_all_test_cases("555")

        self.assertEqual(len(cases), 15)
        self.assertEqual(len(fake.calls_to("problem.testInput")), 15)
        self.assertEqual(len(fake.calls_to("problem.testAnswer")), 15)

        first = cases[0]
        self.assertEqual(set(first), {"index", "manual", "is_sample", "description", "input", "output"})
        self.assertEqual(first["index"], 1)
        self.assertTrue(first["is_sample"])
        self.assertEqual(first["input"], "1\n1\n")
        self.assertEqual(first["output"], "2\n")

        generated = cases[-1]
        self.assertFalse(generated["is_sample"])
        self.assertFalse(generated["manual"])

    def test_sample_flag_maps_from_useinstatedements(self):
        tests = [stub.make_test(1, sample=True), stub.make_test(2, sample=False)]
        fake = stub.PolygonStub(tests=tests)
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            cases = PolygonAPI().get_all_test_cases("556")
        self.assertEqual([c["is_sample"] for c in cases], [True, False])

    def test_unavailable_test_content_becomes_empty_not_the_error_text(self):
        tests = [stub.make_test(1), stub.make_test(2)]
        fake = stub.PolygonStub(tests=tests, fail_methods={"problem.testInput"})
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            cases = PolygonAPI().get_all_test_cases("557")
        self.assertEqual(cases[0]["input"], "")
        self.assertEqual(cases[0]["output"], "")
        for case in cases:
            self.assertNotIn("status", case["input"])
            self.assertNotIn("FAILED", case["output"])

    def test_a_failed_answer_does_not_leak_the_error_envelope_into_output(self):
        fake = stub.PolygonStub(tests=[stub.make_test(1)],
                               fail_methods={"problem.testAnswer"})
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            cases = PolygonAPI().get_all_test_cases("558")
        self.assertEqual(cases[0]["output"], "")


class PackageTests(SimpleTestCase):
    def test_download_and_extract_returns_problem_html(self):
        fake = stub.PolygonStub()
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            html = PolygonAPI().download_and_extract_package("558")
        self.assertIn("Probe Sum", html)
        self.assertIn('class="input-specification"', html)

    def test_non_zip_response_is_rejected(self):
        fake = stub.PolygonStub(package_bytes=b"not a zip")
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            with self.assertRaises(Exception) as ctx:
                PolygonAPI().download_and_extract_package("559")
        self.assertIn("not a valid ZIP", str(ctx.exception))

    def test_package_missing_problem_html_is_rejected(self):
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("42/problem.xml", "<problem/>")
        fake = stub.PolygonStub(package_bytes=buffer.getvalue())
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            with self.assertRaises(Exception) as ctx:
                PolygonAPI().download_and_extract_package("560")
        self.assertIn("problem.html not found", str(ctx.exception))

    def test_package_download_also_carries_a_timeout(self):
        fake = mock.Mock(return_value=stub.FakeResponse(
            content=stub.build_package()))
        fake.side_effect = stub.PolygonStub()
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            PolygonAPI().download_and_extract_package("561")
        # download_and_extract_package builds its own signed POST
        self.assertEqual(fake.call_args.kwargs.get("timeout"), REQUEST_TIMEOUT)


class CheckerTests(SimpleTestCase):
    """Characterisation of the shipped custom-checker heuristic.

    ``get_custom_checker_info`` treats any checker name without a ``std::``
    prefix as custom, so Polygon's standard bare names (``ncmp``) are reported
    as custom. ``views.index`` separately re-classifies those as standard, so
    the two disagree for the most common case. Pinned here as observed.
    """

    def test_bare_standard_checker_name_is_reported_as_custom(self):
        fake = stub.PolygonStub(checker="ncmp")
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            self.assertEqual(PolygonAPI().get_custom_checker_info("561"),
                             {"name": "ncmp", "type": "custom"})

    def test_std_prefixed_checker_is_standard(self):
        fake = stub.PolygonStub(checker="std::ncmp")
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            self.assertIsNone(PolygonAPI().get_custom_checker_info("562"))

    def test_custom_checker_source_name_is_reported(self):
        fake = stub.PolygonStub(checker="my_checker.cpp")
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            self.assertEqual(PolygonAPI().get_custom_checker_info("563"),
                             {"name": "my_checker.cpp", "type": "custom"})

    def test_view_reclassifies_the_same_name_as_standard(self):
        from problems.views import _normalize_checker_type
        self.assertEqual(_normalize_checker_type("ncmp"), "ncmp")
        self.assertEqual(_normalize_checker_type("std::ncmp"), "ncmp")
        self.assertEqual(_normalize_checker_type("my_checker.cpp"), "custom")
        self.assertEqual(_normalize_checker_type("testlib"), "custom")