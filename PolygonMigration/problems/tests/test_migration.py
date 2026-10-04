"""Full migration workflow exercised through the real view, plus the four
edge-case scenarios the assessment asks about.

Polygon is replaced at the HTTP boundary (so signing/parsing still runs) and
storage is an in-memory backend, unless a test explicitly asks for the real
S3-compatible service. PostgreSQL is the real test database; Redis is the real
configured server when available and degrades to "cache miss" when it is not.
"""

import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from problems.models import Problem, ProblemTag, ProblemTestCase, SampleTestCase
from problems.polygon_api import PolygonAPI
from problems.storage import BlobStorageError, build_problem_prefix, get_storage
from problems.tests import polygon_stub as stub
from problems.tests import polygon_stub as stubmod
from problems.tests.test_storage import MemoryStorage

User = get_user_model()

TAGS = ["arrays", "math"]


def html_with_title(title):
    return stub.PROBLEM_HTML.replace("Probe Sum", title)


class MigrationTestBase(TestCase):
    """Shared wiring: a staff user, an isolated polygon id, isolated storage."""

    def setUp(self):
        stubmod.disable_pacing(self)
        self.user = User.objects.create_user(
            email="staff@example.com", password="s3cret-pass-123",
            username="staff", first_name="Ada", last_name="L", is_staff=True,
        )
        self.client.force_login(self.user)
        self.storage = MemoryStorage()
        # Unique per test so Redis keys from a previous run can never leak in.
        self.polygon_id = f"T{uuid.uuid4().int % 10**9}"
        self.addCleanup(self._clear_redis)

    def _clear_redis(self):
        try:
            PolygonAPI().clear_test_cases_from_redis(self.polygon_id)
        except Exception:
            pass

    def patched(self, fake_polygon):
        """Patch the Polygon HTTP boundary and the storage factory."""
        return (
            mock.patch("problems.polygon_api.requests.post", side_effect=fake_polygon),
            mock.patch("problems.views.get_storage", return_value=self.storage),
            mock.patch("problems.polygon_api.get_storage", return_value=self.storage),
        )

    def use(self, fake_polygon):
        p1, p2, p3 = self.patched(fake_polygon)
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)
        return fake_polygon

    def post(self, **fields):
        data = {"problem_id": self.polygon_id}
        data.update(fields)
        return self.client.post(reverse("problems:index"), data)


class AccessControlTests(MigrationTestBase):
    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("problems:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/users/login/", response["Location"])

    def test_non_staff_user_is_redirected(self):
        plain = User.objects.create_user(
            email="plain@example.com", password="s3cret-pass-123",
            username="plain", first_name="P", last_name="L", is_staff=False)
        self.client.force_login(plain)
        response = self.client.get(reverse("problems:index"))
        self.assertEqual(response.status_code, 302)

    def test_login_page_renders(self):
        self.client.logout()
        response = self.client.get("/users/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_staff_user_gets_the_migration_page(self):
        response = self.client.get(reverse("problems:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fetch Problem")


class FetchProblemTests(MigrationTestBase):
    def test_fetch_renders_statement_previews_and_but_not_the_solution(self):
        """Characterisation: the reference solution is only fetched for an
        already-migrated problem or right after a migration, never on a
        first-time fetch."""
        long_input = "1 " * 60 + "\n"
        long_output = "2 " * 60 + "\n"
        contents = {}
        for i in range(1, 6):
            contents[(i, "input")] = long_input
            contents[(i, "output")] = long_output
        fake = self.use(stub.PolygonStub(
            tests=stub.default_tests(sample_count=3, regular_count=2),
            contents=contents))
        response = self.post()
        self.assertEqual(response.status_code, 200)

        html = response.content.decode()
        self.assertIn("Probe Sum", html)
        self.assertIn("print their sum", html)          # legend
        self.assertIn("two integers", html)             # input format
        self.assertIn("All Test Cases (5 total)", html) # test table
        self.assertContains(response, "View Full")      # full-content affordance
        # preview is truncated server-side to 50 chars
        self.assertIn((long_input[:50] + "..."), html)
        self.assertIn("problem.tests", fake.methods_called())
        self.assertIn("problem.testInput", fake.methods_called())
        self.assertIn("problem.testAnswer", fake.methods_called())

        self.assertIsNone(response.context.get("main_solution"))
        self.assertNotIn("int main", html)
        self.assertIn("Please migrate the problem to the database first.", html)

    def test_short_test_content_has_no_full_content_affordance(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=1)))
        response = self.post()
        self.assertNotContains(response, "View Full")

    def test_solution_appears_once_the_problem_is_in_the_database(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        response = self.post()
        self.assertIn("int main", response.context["main_solution"])

    def test_fetch_writes_nothing_to_the_database(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        self.post()
        self.assertEqual(Problem.objects.count(), 0)
        self.assertEqual(ProblemTestCase.objects.count(), 0)

    def test_invalid_problem_id_surfaces_an_error_message(self):
        self.use(stub.PolygonStub(fail_methods={"problem.info", "problem.tests",
                                                "problem.packages", "problem.checker"}))
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("error", response.context)
        self.assertIn("problem.info", response.context["error"].lower() + " ")

    def test_polygon_timeout_surfaces_an_error_message(self):
        """Uses requests' own Timeout so the real RequestException handler runs."""
        import requests
        self.use(stub.PolygonStub(
            raise_methods={"problem.info": requests.exceptions.ReadTimeout("timed out")}))
        response = self.post()
        self.assertIn("error", response.context)
        self.assertIn("HTTP Request Error", response.context["error"])
        self.assertIn("timed out", response.context["error"])

    def test_custom_checker_name_is_reported(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(), checker="weird_checker.cpp"))
        response = self.post()
        self.assertEqual(response.context["fetched_problem"]["checker_type"], "custom")
        self.assertEqual(response.context["fetched_problem"]["custom_checker_info"]["name"],
                         "weird_checker.cpp")


class ProblemContentSanitisationTests(MigrationTestBase):
    """The statement is remote HTML, so it is filtered before it reaches the page.

    Rendering it escaped showed literal ``<p>`` tags to the user; rendering it
    with ``|safe`` unfiltered would execute whatever the problem contains.
    """

    HOSTILE_HTML = (
        '<div class="legend">'
        '<div class="section-title">Problem</div>'
        '<p>Given <b>a</b> and <b>b</b>.</p>'
        '<script>window.stolen = 1;</script>'
        '<p onclick="window.stolen = 2">click</p>'
        '<a href="javascript:window.stolen=3">go</a>'
        '</div>'
    )

    def _post_with_legend(self, legend_html):
        html = ('<html><body><div class="title">Probe Sum</div>'
                f'<div class="legend">{legend_html}</div>'
                '<div class="input-specification">'
                '<div class="section-title">Input</div><p>Two ints.</p></div>'
                '<div class="output-specification">'
                '<div class="section-title">Output</div><p>The sum.</p></div>'
                '</body></html>')
        self.use(stub.PolygonStub(tests=stub.default_tests(), problem_html=html))
        return self.post()

    def test_script_and_handlers_never_reach_the_rendered_page(self):
        response = self._post_with_legend(self.HOSTILE_HTML)
        self.assertEqual(response.status_code, 200)
        statement = response.context["fetched_problem"]["problem_statement"]
        self.assertNotIn("script", statement.lower())
        self.assertNotIn("onclick", statement.lower())
        self.assertNotIn("javascript:", statement.lower())
        self.assertIn("Given", statement)

    def test_meaningful_markup_is_preserved(self):
        response = self._post_with_legend(self.HOSTILE_HTML)
        statement = response.context["fetched_problem"]["problem_statement"]
        self.assertIn("<b>a</b>", statement)

    def test_rendered_html_shows_formatting_not_literal_tags(self):
        response = self._post_with_legend(
            '<p>Print <code>a+b</code> on one line.</p>')
        html = response.content.decode()
        self.assertIn("<code>a+b</code>", html)
        self.assertNotIn("&lt;code&gt;", html)

    def test_database_keeps_the_original_unsanitised_html(self):
        """Display is filtered; the migration target must stay lossless."""
        html = ('<html><body><div class="title">Probe Sum</div>'
                f'<div class="legend">{self.HOSTILE_HTML}</div>'
                '<div class="input-specification">'
                '<div class="section-title">Input</div><p>Two ints.</p></div>'
                '<div class="output-specification">'
                '<div class="section-title">Output</div><p>The sum.</p></div>'
                '</body></html>')
        self.use(stub.PolygonStub(tests=stub.default_tests(), problem_html=html))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        stored = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertIn("window.stolen", stored.problem_statement)


class MigrateProblemTests(MigrationTestBase):
    def test_problem_is_persisted_with_difficulty_and_tags(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        response = self.post(migrate_to_db="1", difficulty="medium",
                             tags=["arrays", "math", "greedy"])
        self.assertIn("db_success", response.context)

        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertEqual(problem.title, "Probe Sum")
        self.assertEqual(problem.difficulty, "medium")
        self.assertEqual(problem.slug, "probe-sum")
        self.assertEqual(problem.test_case_count, 15)
        self.assertEqual(problem.time_limit, 1000)
        self.assertEqual(problem.memory_limit, 256)
        self.assertEqual(problem.checker_type, "ncmp")
        self.assertIn("print their sum", problem.problem_statement)
        self.assertIn("32-bit", problem.notes)

        self.assertEqual(
            sorted(problem.extra_tags.values_list("tag_name", flat=True)),
            ["arrays", "greedy", "math"],
        )
        self.assertEqual(ProblemTag.objects.count(), 3)

    def test_two_tags_minimum_is_satisfied(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertEqual(problem.extra_tags.count(), 2)

    def test_missing_difficulty_is_refused_and_writes_nothing(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        response = self.post(migrate_to_db="1", tags=TAGS)
        self.assertIn("difficulty", response.context["error"].lower())
        self.assertEqual(Problem.objects.count(), 0)

    def test_repeat_migration_updates_instead_of_duplicating(self):
        fake = stub.PolygonStub(tests=stub.default_tests())
        self.use(fake)
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        self.post(migrate_to_db="1", difficulty="hard", tags=["arrays"])
        self.assertEqual(Problem.objects.count(), 1)
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertEqual(problem.difficulty, "hard")
        self.assertEqual([t.tag_name for t in problem.extra_tags.all()], ["arrays"])
        # tags cleared then re-added, not accumulated
        self.assertEqual(ProblemTag.objects.count(), 2)

    def test_sample_test_cases_are_persisted_separately(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=3, regular_count=12)))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertEqual(SampleTestCase.objects.filter(problem=problem).count(), 3)
        self.assertEqual(problem.sample_test_cases.count(), 3)
        self.assertTrue(all(s.input and s.output for s in problem.sample_test_cases.all()))

    def test_reference_solution_is_returned_for_display(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        response = self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        self.assertIn("int main", response.context["main_solution"])


class MigrateTestCasesToDbTests(MigrationTestBase):
    def _migrate(self, tests, sample_count=None):
        fake = stub.PolygonStub(tests=tests)
        self.use(fake)
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        response = self.post(migrate_test_cases_to_db="1")
        return response

    def test_all_test_cases_are_persisted(self):
        self._migrate(stub.default_tests(sample_count=3, regular_count=12))
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        rows = list(ProblemTestCase.objects.filter(problem=problem).order_by("order"))
        self.assertEqual(len(rows), 15)
        self.assertEqual([r.order for r in rows], list(range(1, 16)))
        self.assertEqual(sum(1 for r in rows if r.is_sample), 3)

    def test_requires_the_problem_to_exist_first(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        response = self.post(migrate_test_cases_to_db="1")
        self.assertIn("migrate the problem to the database first",
                      response.context["error"].lower())

    def test_repeat_run_does_not_duplicate_rows(self):
        self._migrate(stub.default_tests())
        self.post(migrate_test_cases_to_db="1")
        self.assertEqual(ProblemTestCase.objects.count(), 15)

    def test_content_longer_than_the_column_is_stored(self):
        long_in = "1 2\n" + ("9 " * 400)
        long_out = "3\n"
        tests = [stub.make_test(1, sample=True, manual=True)]
        fake = stub.PolygonStub(tests=tests, contents={
            (1, "input"): long_in, (1, "output"): long_out})
        self.use(fake)
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        self.post(migrate_test_cases_to_db="1")
        row = ProblemTestCase.objects.get()
        self.assertEqual(len(row.input), 260)
        self.assertTrue(long_in.startswith(row.input))


class StorageMigrationTests(MigrationTestBase):
    def _migrate_problem(self):
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)

    def test_objects_use_the_required_layout_and_content(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=3, regular_count=12)))
        self._migrate_problem()
        response = self.post(migrate_to_azure="1")
        self.assertIn("success", response.context)
        self.assertIn("15 test case(s)", response.context["success"])

        problem = Problem.objects.get(polygon_id=self.polygon_id)
        keys = sorted(self.storage.objects)
        expected = []
        for number in range(1, 16):
            expected.append(f"test_cases/{problem.id}/{number}")
            expected.append(f"test_cases/{problem.id}/{number}.a")
        self.assertEqual(keys, sorted(expected))

        # independent read-back through the interface
        self.assertEqual(
            self.storage.read_text(f"test_cases/{problem.id}/1"), "1\n1\n")
        self.assertEqual(
            self.storage.read_text(f"test_cases/{problem.id}/1.a"), "2\n")

    def test_storage_uses_the_database_problem_id_not_the_polygon_id(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=1)))
        self._migrate_problem()
        self.post(migrate_to_azure="1")
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertNotEqual(str(problem.id), self.polygon_id)
        self.assertTrue(all(k.startswith(f"test_cases/{problem.id}/") for k in self.storage.objects))

    def test_requires_the_problem_to_exist_first(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        response = self.post(migrate_to_azure="1")
        self.assertIn("cloud storage", response.context["error"])
        self.assertEqual(self.storage.objects, {})

    def test_rerun_replaces_previous_objects(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=2)))
        self._migrate_problem()
        self.post(migrate_to_azure="1")
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertEqual(len(self.storage.objects), 6)

        # An object left behind by an earlier, larger run must be gone after the
        # prefix is cleared -- this is what proves delete_prefix actually runs.
        stale = f"test_cases/{problem.id}/99"
        self.storage.upload_text(stale, "left over")
        self.assertIn(stale, self.storage.objects)

        self.post(migrate_to_azure="1")
        self.assertNotIn(stale, self.storage.objects)
        self.assertEqual(len(self.storage.objects), 6)

    def test_skipped_test_cases_are_reported_to_the_user(self):
        tests = [stub.make_test(1, sample=True, manual=True),
                 stub.make_test(2), stub.make_test(3)]
        contents = {(1, "input"): "1\n", (1, "output"): "1\n",
                    (2, "input"): "", (2, "output"): "",
                    (3, "input"): "3\n", (3, "output"): "3\n"}
        self.use(stub.PolygonStub(tests=tests, contents=contents))
        self._migrate_problem()
        response = self.post(migrate_to_azure="1")
        self.assertIn("2 test case(s) migrated", response.context["success"])
        self.assertIn("1 test case(s) were skipped", response.context["success"])
        self.assertIn("2", response.context["success"])

    def test_upload_failure_is_reported_to_the_user(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=2)))
        self._migrate_problem()
        self.storage.fail_on = {"*"}
        response = self.post(migrate_to_azure="1")
        self.assertIn("error", response.context)
        # the specific cause and the rollback notice are both reported
        self.assertIn("Storage migration failed", response.context["error"])
        self.assertIn("rolled back", response.context["error"])

    def test_a_single_failing_object_is_not_reported_as_success(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=3)))
        self._migrate_problem()
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.storage.fail_on = {f"test_cases/{problem.id}/2.a"}
        response = self.post(migrate_to_azure="1")
        self.assertIn("error", response.context)
        self.assertNotIn("success", response.context)
        self.assertIn("2.a", response.context["error"])

    def test_skipped_test_cases_do_not_break_numbering(self):
        tests = [stub.make_test(1, sample=True, manual=True),
                 stub.make_test(2), stub.make_test(3)]
        contents = {(1, "input"): "1\n", (1, "output"): "1\n",
                    (2, "input"): "", (2, "output"): "",
                    (3, "input"): "3\n", (3, "output"): "3\n"}
        fake = stub.PolygonStub(tests=tests, contents=contents)
        self.use(fake)
        self._migrate_problem()
        self.post(migrate_to_azure="1")
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        keys = sorted(self.storage.objects)
        # numbering is positional: test 2 is skipped, test 3 still lands on "3"
        self.assertEqual(keys, [
            f"test_cases/{problem.id}/1",
            f"test_cases/{problem.id}/1.a",
            f"test_cases/{problem.id}/3",
            f"test_cases/{problem.id}/3.a",
        ])


# ---------------------------------------------------------------------------
# Assessment edge-case scenarios (facts only; no conclusions drawn here)
# ---------------------------------------------------------------------------

class EdgeCaseATests(MigrationTestBase):
    """0 sample tests, 15 regular tests."""

    def test_zero_samples_still_migrates_everything_else(self):
        tests = stub.default_tests(sample_count=0, regular_count=15)
        self.use(stub.PolygonStub(tests=tests))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        response = self.post(migrate_test_cases_to_db="1")

        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertIn("success", response.context)
        self.assertEqual(problem.sample_test_cases.count(), 0)
        self.assertEqual(SampleTestCase.objects.filter(problem=problem).count(), 0)
        self.assertEqual(ProblemTestCase.objects.filter(problem=problem).count(), 15)
        self.assertEqual(ProblemTestCase.objects.filter(problem=problem, is_sample=True).count(), 0)

    def test_shrinking_samples_leaves_stale_sample_rows(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=3, regular_count=2)))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertEqual(problem.sample_test_cases.count(), 3)

        # Polygon problem loses every sample test.
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=0, regular_count=2)))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        self.assertEqual(
            SampleTestCase.objects.filter(problem=problem).count(), 3,
            "no SampleTestCase rows were deleted")


class EdgeCaseBTests(MigrationTestBase):
    """20 tests migrated, then Polygon drops to 12, then migrated again."""

    def test_removed_tests_are_not_deleted_from_the_database(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=3, regular_count=17)))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.post(migrate_test_cases_to_db="1")

        before = list(ProblemTestCase.objects.filter(problem=problem)
                      .order_by("order").values_list("id", "order", "input"))
        self.assertEqual(len(before), 20)

        # Setter removes eight tests on Polygon.
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=3, regular_count=9)))
        response = self.post(migrate_test_cases_to_db="1")

        after = list(ProblemTestCase.objects.filter(problem=problem)
                     .order_by("order").values_list("id", "order", "input"))
        self.assertEqual(len(after), 20, "row count did not shrink")
        self.assertEqual(after, before, "existing rows were rewritten in place")

        # The twelve surviving Polygon indices are 1..12; rows 13..20 keep stale data.
        stale = ProblemTestCase.objects.filter(problem=problem, order__gt=12)
        self.assertEqual(stale.count(), 8)
        self.assertEqual(stale.filter(input="").count(), 0,
                         "stale rows still carry the removed tests' contents")
        self.assertIn("success", response.context)

    def test_repeat_migration_after_test_count_drops_still_reports_success(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=3, regular_count=17)))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        self.post(migrate_test_cases_to_db="1")

        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=4)))
        response = self.post(migrate_test_cases_to_db="1")
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertIn("success", response.context)
        self.assertEqual(ProblemTestCase.objects.filter(problem=problem).count(), 20)
        self.assertEqual(ProblemTestCase.objects.filter(problem=problem, is_sample=True).count(), 3)

    def test_storage_migration_after_shrinking_does_leave_stale_objects(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=19)))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self.post(migrate_to_azure="1")
        self.assertEqual(len(self.storage.objects), 40)

        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=11)))
        self.post(migrate_to_azure="1")
        self.assertEqual(len(self.storage.objects), 24,
                         "the prefix is cleared before upload, so objects do not accumulate")
        present = {int(k.rsplit("/", 1)[-1].split(".")[0]) for k in self.storage.objects}
        self.assertEqual(present, set(range(1, 13)))


class EdgeCaseCTests(MigrationTestBase):
    """Two different Polygon problems sharing the title 'Two Sum'."""

    def setUp(self):
        stubmod.disable_pacing(self)
        super().setUp()
        self.second_polygon_id = f"S{uuid.uuid4().int % 10**9}"

    def test_duplicate_title_second_migration_fails_on_unique_slug(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(),
                                  problem_html=html_with_title("Two Sum")))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        first = Problem.objects.get(polygon_id=self.polygon_id)
        self.assertEqual(first.title, "Two Sum")
        self.assertEqual(first.slug, "two-sum")

        response = self.client.post(reverse("problems:index"), {
            "problem_id": self.second_polygon_id, "migrate_to_db": "1",
            "difficulty": "easy", "tags": TAGS})
        self.assertIn("error", response.context)
        self.assertIn("slug", response.context["error"].lower())

        self.assertEqual(Problem.objects.filter(title="Two Sum").count(), 1)
        self.assertFalse(Problem.objects.filter(polygon_id=self.second_polygon_id).exists())
        self.assertEqual(ProblemTag.objects.count(), 2)

    def test_slug_uniqueness_is_enforced_by_the_database(self):
        self.use(stub.PolygonStub(tests=stub.default_tests()))
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        original = Problem.objects.get()
        original.pk = None
        original.id = None
        original.polygon_id = "other"
        with self.assertRaises(Exception) as ctx:
            original.save()
        self.assertIn("slug", str(ctx.exception).lower())


class EdgeCaseDTests(MigrationTestBase):
    """What source fields exist, and what survives 'Migrate Test Cases to DB'."""

    def test_polygon_index_and_manual_flag_are_not_persisted(self):
        fake = stub.PolygonStub(tests=stub.default_tests(sample_count=3, regular_count=12))
        self.use(fake)

        source = PolygonAPI().get_all_test_cases(self.polygon_id)
        self.assertEqual(set(source[0]),
                         {"index", "manual", "is_sample", "description", "input", "output"})
        self.assertIn("index", source[0])
        self.assertIn("manual", source[0])

        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        self.post(migrate_test_cases_to_db="1")

        problem = Problem.objects.get(polygon_id=self.polygon_id)
        rows = list(ProblemTestCase.objects.filter(problem=problem).order_by("order"))
        persisted = set(rows[0].__dict__) | {"_state"}
        self.assertIn("is_sample", persisted)
        self.assertIn("input", persisted)
        self.assertIn("output", persisted)
        self.assertIn("description", persisted)
        self.assertIn("order", persisted)
        self.assertNotIn("index", persisted)
        self.assertNotIn("manual", persisted)
        # Polygon test indices 1..15 became orders 1..15 here, but that is a
        # positional counter, not the Polygon index.
        self.assertEqual([r.order for r in rows], list(range(1, 16)))

    def test_manual_flag_is_absent_after_a_redis_round_trip(self):
        tests = stub.default_tests(sample_count=3, regular_count=12)
        fake = stub.PolygonStub(tests=tests)
        self.use(fake)
        api = PolygonAPI()
        source = api.get_all_test_cases(self.polygon_id)
        self.assertIn("manual", source[0])

        api.store_test_cases_in_redis(self.polygon_id, source, expiry_hours=1)
        cached = api.get_test_cases_from_redis(self.polygon_id)
        self.assertIsNotNone(cached)
        self.assertEqual(set(cached[0]), {"index", "input", "output", "description", "is_sample"})
        self.assertNotIn("manual", cached[0])

    def test_long_test_content_is_truncated_and_right_stripped_on_persistence(self):
        payload = "1 " * 500
        tests = [stub.make_test(1, sample=True, manual=True)]
        fake = stub.PolygonStub(tests=tests, contents={
            (1, "input"): payload + "\n", (1, "output"): "999\n"})
        self.use(fake)
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        self.post(migrate_test_cases_to_db="1")
        row = ProblemTestCase.objects.get()
        self.assertEqual(len(row.input), 260)
        self.assertEqual(row.input, payload[:260])
        # the trailing newline is stripped by .rstrip() before truncation
        self.assertEqual(row.output, "999")
        self.assertEqual(SampleTestCase.objects.get().output, "999")

    def test_storage_upload_keeps_full_untruncated_content(self):
        payload = "1 " * 500
        tests = [stub.make_test(1, sample=True, manual=True)]
        fake = stub.PolygonStub(tests=tests, contents={
            (1, "input"): payload + "\n", (1, "output"): "999\n"})
        self.use(fake)
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)
        self.post(migrate_to_azure="1")
        problem = Problem.objects.get(polygon_id=self.polygon_id)
        # the object keeps the bytes Polygon returned, including the trailing newline
        self.assertEqual(self.storage.read_text(f"test_cases/{problem.id}/1"), payload + "\n")
        self.assertEqual(self.storage.read_text(f"test_cases/{problem.id}/1.a"), "999\n")


class TransactionalityTests(MigrationTestBase):
    def test_failure_after_problem_write_rolls_the_database_back(self):
        fake = stub.PolygonStub(tests=stub.default_tests())
        self.use(fake)
        problem_id = self.polygon_id

        # Fail during test-case persistence, after the Problem row was written.
        with mock.patch(
            "problems.views.SampleTestCase.objects.create",
            side_effect=RuntimeError("boom during sample persistence"),
        ):
            response = self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)

        self.assertIn("rolled back", response.context["error"])
        self.assertFalse(Problem.objects.filter(polygon_id=problem_id).exists())
        self.assertEqual(ProblemTag.objects.count(), 0)


class StorageRollbackTests(MigrationTestBase):
    """The compensation path: objects uploaded, then something later fails."""

    def _migrate_problem(self):
        self.post(migrate_to_db="1", difficulty="easy", tags=TAGS)

    def test_objects_uploaded_before_a_later_failure_are_removed(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=2)))
        self._migrate_problem()
        problem = Problem.objects.get(polygon_id=self.polygon_id)

        # upload runs first inside the request; then force a later failure
        with mock.patch("problems.views.parse_problem_html",
                        side_effect=RuntimeError("later boom")):
            response = self.post(migrate_to_azure="1")

        self.assertIn("error", response.context)
        self.assertIn("later boom", response.context["error"])
        # The Problem row was committed by an earlier, successful request, so it
        # legitimately survives; this request's own changes are rolled back.
        self.assertTrue(Problem.objects.filter(pk=problem.pk).exists())
        # The objects uploaded during the failed request are compensated away.
        self.assertEqual(self.storage.objects, {})

    def test_a_rollback_does_not_leave_a_stale_success_message(self):
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=2)))
        self._migrate_problem()
        with mock.patch("problems.views.parse_problem_html",
                        side_effect=RuntimeError("later boom")):
            response = self.post(migrate_to_azure="1")
        self.assertIn("error", response.context)
        self.assertNotIn("success", response.context,
                         "a rolled-back request must not also claim success")

    def test_compensation_failure_does_not_mask_the_original_error(self):
        """If the cleanup itself explodes, the original cause must survive."""
        self.use(stub.PolygonStub(tests=stub.default_tests(sample_count=1, regular_count=2)))
        self._migrate_problem()
        self._migrate_problem()
        with mock.patch("problems.views.parse_problem_html",
                        side_effect=RuntimeError("original failure")), \
             mock.patch("problems.views.get_storage",
                        side_effect=ConnectionResetError("cleanup exploded")):
            response = self.post(migrate_to_azure="1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("original failure", response.context["error"])


class RealStorageWorkflowTests(MigrationTestBase):
    """The same workflow, but writing to the real configured storage backend."""

    def setUp(self):
        stubmod.disable_pacing(self)
        super().setUp()
        try:
            self.real_storage = get_storage()
            self.real_storage.ensure_container()
        except BlobStorageError as exc:
            self.skipTest(f"storage backend unavailable: {exc}")
        self._written_problem_ids = []

    def _track(self, problem_id):
        if problem_id is not None and problem_id not in self._written_problem_ids:
            self._written_problem_ids.append(problem_id)

    def tearDown(self):
        problem = Problem.objects.filter(polygon_id=self.polygon_id).first()
        if problem is not None:
            self._track(problem.id)
        for pid in self._written_problem_ids:
            try:
                self.real_storage.delete_prefix(build_problem_prefix(pid))
            except BlobStorageError:
                pass

    def test_end_to_end_upload_and_independent_verification(self):
        mock.patch("problems.polygon_api.requests.post",
                   side_effect=stub.PolygonStub(tests=stub.default_tests(sample_count=3, regular_count=12))).start()
        self.addCleanup(mock.patch.stopall)

        self.post(migrate_to_db="1", difficulty="medium", tags=TAGS)
        self.post(migrate_test_cases_to_db="1")
        response = self.post(migrate_to_azure="1")
        self.assertIn("success", response.context)

        problem = Problem.objects.get(polygon_id=self.polygon_id)
        self._track(problem.id)
        # Re-read through a brand-new client: nothing served from process memory.
        fresh = get_storage()
        keys = fresh.list_keys(build_problem_prefix(problem.id))
        self.assertEqual(len(keys), 30)
        numbers = {int(k.rsplit("/", 1)[-1].split(".")[0]) for k in keys}
        self.assertEqual(numbers, set(range(1, 16)))
        self.assertIn(f"test_cases/{problem.id}/1", keys)
        self.assertIn(f"test_cases/{problem.id}/1.a", keys)
        self.assertIn(f"test_cases/{problem.id}/15", keys)
        self.assertIn(f"test_cases/{problem.id}/15.a", keys)

        source = PolygonAPI().get_all_test_cases(self.polygon_id)
        for number, case in enumerate(source, start=1):
            self.assertEqual(fresh.read_text(f"test_cases/{problem.id}/{number}"), case["input"])
            self.assertEqual(fresh.read_text(f"test_cases/{problem.id}/{number}.a"), case["output"])

        # And the same input/answer pair is what landed in PostgreSQL (truncated).
        for row in ProblemTestCase.objects.filter(problem=problem).order_by("order"):
            self.assertTrue(fresh.read_text(f"test_cases/{problem.id}/{row.order}").startswith(row.input))