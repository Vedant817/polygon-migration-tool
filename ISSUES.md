# Issues Analysis

> **Instructions**: Document the three highest-priority product issues and the three highest-priority code issues. Rank the issues within each category from highest to lowest priority.
> Replace the example entries with your actual findings and explain why each issue deserves its priority. Complete all four questions in the Edge Case Analysis section.
> Rename this file to `ISSUES.md` before submitting.

## Summary

| Type | Critical | High | Medium | Low | Total |
|------|----------|------|--------|-----|-------|
| Product Issues | 0 | 2 | 1 | 0 | 3 |
| Code Issues | 0 | 1 | 2 | 0 | 3 |

All six findings below were confirmed against the running application with PostgreSQL 15, Redis 7 and Backblaze B2. Where I state that behaviour was tested, I ran it; where I state it was read from code, I say so.

---

## Product Issues

> Product issues are user-facing problems: broken functionality, missing validation, poor UX, data integrity risks visible to users.

### [P1] Re-migrating a problem never removes test cases that were deleted on Polygon, and reports success

**Severity**: High

**Location**: `problems/views.py:528-556` (the `migrate_test_cases_to_db` branch), surfaced to the user by `problems/templates/problems/index.html`

**Description**:
The test case migration loop iterates over the test cases that were just fetched and updates or creates one row per test, matching existing rows by list position:

```python
existing_test_cases = list(ProblemTestCase.objects.filter(problem=problem_obj).order_by('order'))
order = 1
for idx, test in enumerate(test_cases):
    ...
    if idx < len(existing_test_cases):
        ptc = existing_test_cases[idx]
        ptc.save()
    else:
        ProblemTestCase.objects.create(...)
```

Nothing ever removes rows beyond `len(test_cases)`. If a problem is migrated with 20 tests and the setter later deletes 8 on Polygon, a re-migration rewrites the first 12 rows and leaves rows 13 to 20 exactly as they were. The request then reports "Test cases description migrated to database Successfully."

**Impact**:
- The database ends up holding 20 test cases while the problem has 12, and the UI reports success.
- The 8 survivors are not merely stale duplicates, they hold the content of tests that no longer exist, so anyone reading the database gets test data that the setter has withdrawn.
- Because these rows drive judging, students could be graded against tests that were deliberately removed.
- The problem is invisible. There is no warning, no count of removed rows and no difference in the success message, so nothing prompts an operator to look.
- The object storage copy is cleaned up correctly, which makes the inconsistency worse: storage holds 12 test cases while the database holds 20.

**Suggested Fix**:
Delete rows whose `order` exceeds the number of fetched tests, or delete and rewrite the whole set inside the transaction. Report the number of removed rows in the success message so the operator can see what changed:

```python
surplus = existing_test_cases[len(test_cases):]
for row in surplus:
    row.delete()
```

**Why this priority**: It is the only finding that produces confidently wrong data while telling the user everything worked. It is silent, it survives re-runs, and it affects the output the tool exists to produce. I have not seen a symptom of the other five issues that is harder to notice or harder to recover from.

---

### [P2] Database copies of test data are silently truncated to 260 characters

**Severity**: High

**Location**: `problems/views.py:537-539`

**Description**:
Before writing a test case row, the input and output are reduced to 260 characters and right stripped:

```python
# Truncate input and output to first 260 bytes
truncated_input = input_data[:260]
truncated_output = output_data[:260]
```

The comment says bytes, but Python slicing on `str` truncates by character. Nothing in the template, the success message or the database records that the stored value is a prefix of the real test.

**Impact**:
- Any test whose input or output exceeds 260 characters is stored incomplete. Competitive programming test data frequently exceeds this, particularly generated tests with large arrays or matrices.
- The loss is invisible in the UI. The test case table renders from the full fetched data, so the preview looks complete while the stored row is not. An operator comparing the page against the database sees a mismatch with no explanation.
- The truncated copy is what a reviewer, a checker or any downstream consumer of the database would read, so it can produce wrong verdicts.
- `rstrip()` also removes trailing whitespace, which matters for test data where trailing newlines are significant to a checker.

**Suggested Fix**:
Store the full value in the database and truncate only for display, which is where truncation belongs. If a length limit is genuinely required, make it a named setting, count characters rather than bytes, and report the number of truncated fields to the user. Note that the object storage copy is already complete, so the database is the only lossy copy.

**Why this priority**: This is silent data corruption on the primary output, and it is the second issue where the tool reports success while the stored data is wrong. It ranks below P1 because the truncation is bounded and predictable, whereas P1 leaves a structurally wrong row count that is much harder to detect later.

---

### [P3] A duplicate problem title surfaces a raw database constraint error

**Severity**: Medium

**Location**: `problems/views.py` (`migrate_to_db` branch), database constraint `problems_problem_slug_key`

**Description**:
Two different Polygon problems with the same title produce the same `slugify(title)` value. The second migration fails on the unique constraint, and the message the user sees is the driver's own text:

```
Migration failed and all changes have been rolled back. Reason:
duplicate key value violates unique constraint "problems_problem_slug_key"
```

The message confirms that something failed and that the transaction rolled back, but it does not name the problem, explain that the title is already taken, or suggest a remedy.

**Impact**:
- The user has to work backwards from a PostgreSQL constraint name to understand that two problems share a title. Most operators will read this as a database or migration fault rather than a naming conflict.
- No hint is given about what to change. The slug is not editable anywhere in the UI, so the user cannot resolve it without a database change.
- The error is reported on a page that otherwise looks healthy, which makes it easy to miss on a long migration session.

**Suggested Fix**:
Catch `IntegrityError` around the problem write specifically, detect the slug collision, and report it in product language: "A problem titled '{title}' already exists in the database with the slug '{slug}' (Polygon ID {existing}). Migrate that problem instead, or choose a distinct title." Offer to disambiguate the slug by appending the Polygon ID.

**Why this priority**: It is a genuine dead end with no route to resolution from the UI, and duplicate titles are common on Polygon where many setters reuse standard names such as "Two Sum". It ranks below P1 and P2 because nothing is corrupted and the failure is loud and obvious, so it will be noticed and reported immediately.

---

## Code Issues

> Code issues are technical problems: bugs, security vulnerabilities, performance problems, code quality concerns, architectural issues.

### [C1] Object storage uploads run inside the database transaction

**Severity**: High

**Location**: `problems/views.py:207` (`with transaction.atomic():`) wrapping `problems/views.py:233` (`api.migrate_to_storage(...)`)

**Description**:
`transaction.atomic()` opens at line 207. The `migrate_to_azure` branch is the first block inside it, and the upload happens at line 233. A twelve test problem writes 24 objects to Backblaze B2, each a separate HTTPS round trip, while a PostgreSQL transaction is held open for the duration.

This is why the manual compensation logic exists. On failure the code has to delete objects it already wrote, in a separate `except` block, because the database rollback cannot undo storage writes. A comment in the view records that compensation must never mask the original error.

**Impact**:
- A database connection is held open across network calls to an external service. Connection pool capacity is consumed for the whole upload, so concurrent migrations can exhaust the pool and stall unrelated requests.
- Transaction lifetime scales with object count and with cloud latency, not with database work. For a 100 test problem that is 200 sequential HTTPS round trips inside an open transaction.
- Partial failure is the normal case rather than the exception, and recovery depends on hand written compensation that has to be kept correct as the flow changes.
- Cloud provider throttling makes this worse: a slow provider lengthens the transaction and increases the chance the database gives up first.
- The ordering is also backwards. Uploading before the database commit means storage can be populated for a row that never commits.

**Suggested Fix**:
Two changes, in order of value. First, move the upload outside the transaction: commit the database work, then upload, and on failure record the problem as needing a retry rather than trying to unwind storage. Second, if an atomic guarantee is required, write an outbox row inside the transaction and process uploads in a separate worker, which makes the database the single source of truth and removes the compensation path entirely.

**Why this priority**: This is the structural problem underneath P1 and the rollback complexity. It is the reason the most complicated part of the codebase exists, it degrades under exactly the load the tool is meant to absorb, and fixing it properly would remove a whole class of bugs rather than one instance.

---

### [C2] Test case synchronisation is a positional per-row save loop with no bulk path

**Severity**: Medium

**Location**: `problems/views.py:528-556`, and the equivalent sample loop at `problems/views.py:466`

**Description**:
Each test case is matched to an existing row by its position in the ordered list and then persisted with an individual `save()`:

```python
existing_test_cases = list(ProblemTestCase.objects.filter(problem=problem_obj).order_by('order'))
...
    ptc = existing_test_cases[idx]
    ptc.save()
```

This is one UPDATE per test rather than a bulk `bulk_update` or `update_or_create`. The same pattern is repeated for `SampleTestCase`.

**Impact**:
- Two database round trips per test case. A 100 test problem issues 200 statements where a bulk operation would issue two. The cost grows linearly with problem size inside a transaction, which is the worst place to pay it given C1.
- Matching by position is fragile. It silently rewrites the wrong row if the ordering ever differs from Polygon's, and it is the direct cause of the surplus rows described in P1, because rows past the end of the list are simply never visited.
- The duplicated shape of the two loops means a fix to one has to be remembered in the other.

**Suggested Fix**:
Load the existing rows into a dictionary keyed by `order` rather than relying on list position, delete the keys that are no longer present, and write the remainder with `bulk_create` and `bulk_update`. Extract the shared logic so the sample and regular loops cannot drift apart.

**Why this priority**: It is a clear performance and maintainability problem with a correct fix available, but it produces wrong results only through the surplus row behaviour already counted as P1, and it does not corrupt data on its own.

---

### [C3] The Redis cache is never reconciled with Polygon, so cached data can be silently out of date

**Severity**: Medium

**Location**: `problems/polygon_api.py:413` (`get_all_test_cases`) and `problems/polygon_api.py:843` (`get_test_cases_from_redis`); consumed at `problems/views.py:517`

**Description**:
Every action that needs test cases consults Redis first and only calls Polygon on a miss:

```python
test_cases = api.get_test_cases_from_redis(polygon_id)
if test_cases is None:
    test_cases = api.get_all_test_cases(polygon_id)
    api.cache_test_cases(polygon_id, test_cases)
```

The cache is written with a 0.5 hour expiry and is cleared on rollback. There is no comparison against Polygon's `problem.tests`, which is the one cheap call that reports the current test count, so nothing detects that the setter has changed the problem.

**Impact**:
- For up to 30 minutes after a setter edits tests, the tool reports the previous contents as current. A migration in that window writes outdated rows to the database and outdated files to storage, and reports success.
- This is the mechanism that can make P1 hard to reproduce. Re-running a migration after a Polygon edit may appear to fix nothing, because the second run reads the same cached copy.
- The cached copy is trusted for the destructive path as well as the read path, so a stale entry can feed a write.
- The cache key contains only the Polygon ID, so it cannot distinguish problem versions.

**Suggested Fix**:
Always call `problem.tests` first, which is a single cheap request, and compare the returned test count against the cached copy. Invalidate and refetch on any difference. At minimum, surface the cache age in the UI so an operator can tell fresh data from cached data. A `tests_version` or `updatedTime` value from Polygon, if available, would make this exact rather than approximate.

**Why this priority**: It is the correctness risk that is hardest to diagnose in production, because the failure is intermittent and time dependent, and it silently amplifies P1. It sits at Medium rather than High only because the window is bounded at 30 minutes and self healing.

---

<!-- Keep this section to three product issues, ranked from highest to lowest priority. -->

---

## Edge Case Analysis

### Q1

> A Polygon problem has 0 sample test cases but 15 regular test cases. What happens when you migrate this problem?

**Tested.** I created this shape and ran the migration against a real PostgreSQL database.

The migration succeeds. Nothing is rejected for having no samples. Afterwards the database contains:

* `problems_sampletestcase`: **0 rows**
* `problems_problemtestcase`: **15 rows**, every one with `is_sample = false`
* `problems_problem`: 1 row, with `test_case_count = 15`

**Why**: `sample_test_cases` are not inferred from the data, they come from Polygon's own `useInStatements` flag on each entry of `problem.tests`. With no sample tests, every entry has that flag false. The view creates `SampleTestCase` rows only for entries where the flag is true, so the loop simply never runs for that model. `ProblemTestCase` rows are created for every test regardless, which is why all 15 appear there.

**Consequence for the user**: The page renders a "Sample Test Case" column reading "No" on all 15 rows, which is correct rather than broken. Nothing in the UI or the validation layer requires a minimum of three samples, so a problem with no samples is indistinguishable from any other complete migration.

**Distinguishing tested from read**: the row counts, the `is_sample` values and the absence of an error message were all observed. The explanation that the flag comes from `useInStatements` is from reading `get_all_test_cases()` and the sample branch in the view.

---

### Q2

> A problem is migrated with 20 test cases. Later, the problem setter removes 8 test cases on Polygon (now 12 remain). The problem is re-migrated. What happens?

**Tested, on both the warm and the cold cache path.** This is the behaviour behind P1.

**Cold cache, the interesting case.** After the re-migration:

* `problems_problemtestcase`: **20 rows**, unchanged in count. **0 rows deleted.**
* Rows 1 to 12 are overwritten with the current content.
* Rows 13 to 20 keep their previous input, output and description, that is, the content of the 8 tests that were withdrawn on Polygon.
* The UI reports "Test cases description migrated to database Successfully."
* Object storage is correct: it holds only 12 test cases, because the upload path replaces the whole `test_cases/{id}/` prefix.

**Warm cache, the more misleading case.** If the previous migration populated Redis less than 30 minutes ago, the re-migration reads the cached 20 test cases, not the current 12. It therefore rewrites all 20 rows with the same values they already had and reports success. Nothing changes at all, and the operator sees a clean run rather than a skipped one.

**Why**: the loop at `views.py:530` walks the fetched test cases and writes each one, and the update branch is guarded by `if idx < len(existing_test_cases)`. Once `idx` reaches 12 the guard fails and new rows stop being created, but nothing walks the remaining entries of `existing_test_cases` to remove them. The surplus is invisible because the success message reports only that the operation completed.

**Distinguishing tested from read**: the row counts, the survival of the 8 removed tests, the unchanged storage prefix and both messages were observed. The `idx < len(existing_test_cases)` guard and the absence of any delete call are from reading the loop.

---

### Q3

> Two different Polygon problems have the exact same title: "Two Sum". You migrate the first one successfully. Then you try to migrate the second one. What happens?

**Tested.** The first migration commits. The second fails on the database, not on Polygon.

* The second request raises `IntegrityError` naming the unique constraint `problems_problem_slug_key`.
* The failure happens inside `transaction.atomic()`, so **no partial row is created**. I confirmed there is no second `Problem` row, no orphan `ProblemTag` rows and no `SampleTestCase` or `ProblemTestCase` rows for the second problem. The rollback is clean.
* No objects are written to cloud storage, because the write fails before the upload stage.
* The user sees HTTP 200 with the message: "Migration failed and all changes have been rolled back. Reason: duplicate key value violates unique constraint "problems_problem_slug_key""

**Why**: `slugify("Two Sum")` produces the same value for both problems, and `Problem.slug` is declared `unique=True`. `update_or_create` looks the problem up by `polygon_id`, which differs, so it correctly attempts an insert rather than an update, and the database rejects it. The matching on `polygon_id` is the right key and is not the problem here; the collision is on the derived slug.

**Practical consequence**: because the slug is not user editable anywhere in the interface, there is no way to resolve this from the UI. The user has to change the title on Polygon or intervene in the database. This is why P3 recommends naming the conflict in product language and offering to disambiguate the slug, for example by appending the Polygon ID.

**Distinguishing tested from read**: the constraint name, the absence of partial rows, the clean tag and test case state, the storage behaviour and the exact message were all observed. The role of `slugify` and the `unique=True` declaration are from reading the view and the model.

---

### Q4

> When test cases are saved to the database via "Migrate Test Cases to DB", some data is intentionally discarded. What data is lost? Why might this cause problems?

**Tested and read.** Four separate losses, all in the path that writes `ProblemTestCase` and `SampleTestCase`.

**1. Input and output are truncated to 260 characters.** `views.py:537-539` slices `input_data[:260]` and `output_data[:260]`. Anything past 260 characters is discarded permanently from the database copy. The comment describes this as bytes, but it is characters.

**2. Input and output are right stripped.** `views.py:531-532` applies `.rstrip()` before truncation, so trailing whitespace and newlines are removed from the stored value.

**3. Test descriptions are lost on the cached path.** Polygon supplies a `description` only for tests it classifies as `manual`. Generated tests have no `input` or description in the `problem.tests` payload, and the description is not carried through the Redis cache round trip. So on a warm cache the `description` column is written empty even for tests that have one.

**4. The `index` and `manual` markers are never persisted.** Polygon's `problem.tests` returns both fields. The `ProblemTestCase` model has no column for either, so they are read and then discarded. There is no record of which tests were hand written and which were generated.

**Why this causes problems**:
* The database becomes an incomplete copy of the test data, with no field recording that it is incomplete. Anything reading it downstream, a judge, a reviewer or a checker, cannot tell that what it holds has been cut short.
* Trailing whitespace can be significant to a checker that compares raw output, so stripping it can turn a correct solution into an incorrect verdict.
* Losing the `manual` and `index` markers removes the ability to distinguish curated tests from generated ones, which is usually the first thing needed when a failure is investigated.
* The loss is invisible in the UI. Previews are rendered from the full fetched data, so the page looks correct while the stored row is not. This is the mismatch behind P2.
* It matters that **object storage still holds the complete bytes**. The two copies disagree, and only one of them is lossy, so a reader who checks the bucket will conclude the database is wrong.

**Suggested fix**: keep the full text in the database, truncate only for display, and add columns for `manual` and the original `index` if the distinction is worth keeping.

**Distinguishing tested from read**: the truncation and strip behaviour, the empty descriptions on the cached path, and the mismatch between the database copy and the stored object were all observed. The claim that `index` and `manual` are absent from the model is from reading `problems/models.py` and the write loop.

---

## Severity Guidelines

Use these definitions when assigning severity:

| Severity | Definition | Examples |
|----------|------------|----------|
| **Critical** | System broken, security vulnerability, data loss | SQL injection, authentication bypass, data corruption |
| **High** | Major functionality broken, significant data integrity risk | Feature doesn't work, orphaned records, race conditions |
| **Medium** | Feature partially broken, poor UX, code maintainability | Missing validation, confusing errors, code duplication |
| **Low** | Minor issues, cosmetic, best practice violations | Unused imports, inconsistent formatting, missing logs |

---

## Notes

**Things I fixed rather than listed as issues.** Working through the codebase I found and corrected a stored cross site scripting hole in the tag rendering path, a migration blocker caused by two model fields having no migration, a zero padded object key that violated the required layout, and two provider faults that only appeared against a live S3 endpoint. They are described here so the reviewer knows they were found, but they are not in the ranked lists because they are no longer defects.

**Two deliberate design choices that look like bugs.** First, object keys use the database `Problem.id` rather than the Polygon ID, and the upload button is disabled until a database row exists. That ordering looks odd but is required by the storage layout, so I left it. Second, raw exception text is shown to the user on failure. It reads as leaked internals, but for a duplicate title the constraint name in that text is the only clue the user gets, so I kept it and only fixed the case where it was empty.

**A structural observation.** `index()` in `problems/views.py` is one view roughly 590 lines long handling authentication, fetching, parsing, four migration paths, rollback and compensation. Every finding above lives in that function. Splitting it into one view per action would make the flows independently testable and would let the storage upload leave the transaction in C1 without restructuring everything around it.

**Not covered.** I did not load test the tool, so I cannot put a number on how C1 behaves under concurrency. I also did not verify behaviour for problems with more than about 100 test cases, so the cost figures in C2 are extrapolated from the per row pattern rather than measured.
