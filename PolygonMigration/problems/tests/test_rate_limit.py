"""Polygon rate limiting: request pacing, retry, and cache poisoning.

Reproduced against the live API on 2026-10-04: a 12-test migration issues ~25
POSTs back to back, Polygon answers `429 Too Many Requests`, and the affected
tests end up with empty input/output.
"""

from unittest import mock

from django.test import SimpleTestCase

from problems.polygon_api import REQUEST_ATTEMPTS, PolygonAPI
from problems.tests import polygon_stub as stub


class RetryTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch("problems.polygon_api.requests.post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        # never actually sleep between requests during tests
        sleep_patcher = mock.patch("problems.polygon_api.time.sleep", mock.Mock())
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        self.api = PolygonAPI()

    @staticmethod
    def _ok():
        return stub.FakeResponse(json_data={"status": "OK", "result": {"ok": True}})

    @staticmethod
    def _status(code):
        return stub.FakeResponse(json_data={"status": "FAILED"}, status_code=code)

    def test_rate_limited_call_is_retried_then_succeeds(self):
        self.post.side_effect = [self._status(429), self._status(429), self._ok()]
        self.assertEqual(self.api._make_request("problem.info", {}), {"ok": True})
        self.assertEqual(self.post.call_count, 3)

    def test_rate_limit_gives_up_after_the_attempt_limit(self):
        self.post.return_value = self._status(429)
        with self.assertRaises(Exception) as ctx:
            self.api._make_request("problem.info", {})
        self.assertIn("HTTP Request Error", str(ctx.exception))
        self.assertEqual(self.post.call_count, REQUEST_ATTEMPTS)

    def test_gateway_error_is_retried(self):
        self.post.side_effect = [self._status(502), self._ok()]
        self.assertEqual(self.api._make_request("problem.info", {}), {"ok": True})
        self.assertEqual(self.post.call_count, 2)

    def test_client_error_is_not_retried(self):
        self.post.return_value = self._status(403)
        with self.assertRaises(Exception):
            self.api._make_request("problem.info", {})
        self.assertEqual(self.post.call_count, 1, "4xx must fail fast, not be retried")

    def test_consecutive_calls_are_spaced_out(self):
        self.post.return_value = self._ok()
        api = PolygonAPI()
        api._make_request("problem.info", {})
        first_at = api._last_request_at
        api._make_request("problem.info", {})
        self.assertGreater(api._last_request_at, first_at)

    def test_plain_text_call_is_retried_too(self):
        self.post.side_effect = [
            self._status(429),
            stub.FakeResponse(text="1 2\n"),
        ]
        self.assertEqual(
            self.api._make_plain_request("problem.testInput", {"testIndex": "1"}), "1 2\n")
        self.assertEqual(self.post.call_count, 2)


class CachePoisoningTests(SimpleTestCase):
    """A partial fetch must not be cached for the next 30 minutes."""

    def setUp(self):
        sleep_patcher = mock.patch("problems.polygon_api.time.sleep", mock.Mock())
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_incomplete_fetch_is_never_cached(self):
        api = PolygonAPI()
        fake = stub.PolygonStub(
            tests=[stub.make_test(1), stub.make_test(2)],
            fail_methods={"problem.testAnswer"})
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake), \
             mock.patch.object(PolygonAPI, "store_test_cases_in_redis") as store:
            cases = api.get_all_test_cases("4242")
            cached = api.cache_test_cases("4242", cases)
        self.assertEqual(api.last_fetch_incomplete, 2)
        self.assertFalse(cached)
        store.assert_not_called()

    def test_complete_fetch_is_cached(self):
        api = PolygonAPI()
        fake = stub.PolygonStub(tests=[stub.make_test(1)])
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake), \
             mock.patch.object(PolygonAPI, "store_test_cases_in_redis") as store:
            cases = api.get_all_test_cases("4243")
            cached = api.cache_test_cases("4243", cases)
        self.assertEqual(api.last_fetch_incomplete, 0)
        self.assertTrue(cached)
        store.assert_called_once_with("4243", cases, expiry_hours=0.5)

    def test_a_failed_answer_also_discards_the_fetched_input(self):
        """Both contents are fetched in one try block, so one failure blanks both.

        This is why a rate-limited fetch loses more than the failing test: the
        input that was retrieved successfully is thrown away too.
        """
        api = PolygonAPI()
        fake = stub.PolygonStub(
            tests=[stub.make_test(1), stub.make_test(2), stub.make_test(3)],
            fail_methods={"problem.testAnswer"})
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            cases = api.get_all_test_cases("4244")
        self.assertEqual([c["input"] for c in cases], ["", "", ""])
        self.assertEqual([c["output"] for c in cases], ["", "", ""])
        self.assertEqual(api.last_fetch_incomplete, 3)

    def test_metadata_survives_a_failed_content_fetch(self):
        """Index, is_sample and description are still present, so the row is not
        simply lost - it is stored with empty content."""
        api = PolygonAPI()
        fake = stub.PolygonStub(
            tests=[stub.make_test(1, sample=True, description="a sample"),
                   stub.make_test(2)],
            fail_methods={"problem.testAnswer"})
        with mock.patch("problems.polygon_api.requests.post", side_effect=fake):
            cases = api.get_all_test_cases("4245")
        self.assertEqual([c["index"] for c in cases], [1, 2])
        self.assertEqual([c["is_sample"] for c in cases], [True, False])
        self.assertEqual(cases[0]["description"], "a sample")
        self.assertEqual(cases[0]["input"], "")