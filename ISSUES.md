# Issues Analysis

> **Instructions**: Document the three highest-priority product issues and the three highest-priority code issues. Rank the issues within each category from highest to lowest priority.
> Replace the example entries with your actual findings and explain why each issue deserves its priority. Complete all four questions in the Edge Case Analysis section.
> Rename this file to `ISSUES.md` before submitting.

## Summary

| Type | Critical | High | Medium | Low | Total |
|------|----------|------|--------|-----|-------|
| Product Issues | 0 | 2 | 1 | 0 | 3 |
| Code Issues | 0 | 1 | 2 | 0 | 3 |

Everything below was run against the real stack (PostgreSQL 15, Redis 7, Backblaze B2) rather than reasoned about from the source. Where I say something was tested, I tested it. Where I am explaining the mechanism, that is me reading the code, and I say so.

---

## Product Issues

> Product issues are user-facing problems: broken functionality, missing validation, poor UX, data integrity risks visible to users.

### [P1] Re-migrating a problem never removes test cases the setter deleted, and it reports success

**Severity**: High

**Location**: `problems/views.py:528-556`, in the `migrate_test_cases_to_db` branch

**Description**:

The migration walks the test cases it just fetched and writes one row each, matching what is already there by list position:

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

Nothing removes rows past `len(test_cases)`. So if a problem is migrated with 20 tests and the setter later deletes 8, the re-migration rewrites rows 1 to 12 and walks away. Rows 13 to 20 keep the content of tests that no longer exist.

**Impact**:

I ran this deliberately. After re-migrating a 20 test problem down to 12, the table still held 20 rows, zero had been deleted, and the eight survivors were byte for byte the tests the setter had withdrawn. The UI said "Test cases description migrated to database Successfully."

The part that bothers me most is that those eight rows are not merely redundant, they are wrong. Someone reading the database gets test data the setter chose to remove. If those tests drive judging, students get graded on them.

Then there is the silence. No warning, no count, no difference in the success message. Nothing prompts anyone to go looking.

Worth noting that object storage is handled correctly and holds only 12. That makes it worse rather than better: the bucket and the database now disagree, and the database is the one that is wrong.

**Suggested Fix**:

Delete anything past the end of the fetched set, or drop and rewrite the whole set inside the transaction, then say how many rows went away so the change is visible:

```python
for row in existing_test_cases[len(test_cases):]:
    row.delete()
```

**Why this rank**: it is the only one of the six that produces confidently wrong data while telling the user everything went fine. It is silent, it survives repeat runs, and it sits on the output the tool exists to produce. Nothing else here is harder to notice or harder to unpick later.

---

### [P2] Database copies of the test data are quietly truncated to 260 characters

**Severity**: High

**Location**: `problems/views.py:537-539`

**Description**:

Before a row is written:

```python
# Truncate input and output to first 260 bytes
truncated_input = input_data[:260]
truncated_output = output_data[:260]
```

The comment says bytes. It is characters, since these are `str`. Either way, anything past 260 goes away, and nothing in the template, the success message or the stored record says so.

**Impact**:

Generated test data crosses 260 characters constantly, so this is not an edge case, it is the common case for larger inputs.

The reason it is dangerous is that the UI renders previews from the full fetched data. You look at the page and it looks complete. You look at the database and it is not. Nothing bridges that gap, so the first person to notice is usually someone downstream trying to use the data.

There is also a subtle mismatch with the bucket. Object storage keeps the complete files, so the two copies disagree and only the database is lossy. Anyone spot checking the bucket will conclude the database is at fault, which is correct, but it costs them time to work out.

**Suggested Fix**:

Keep the full value in the database and truncate only for display, which is the only place truncation belongs. If a hard limit is genuinely needed, name it, count characters rather than bytes, and report how many fields were cut.

**Why this rank**: second only to P1 because it is also silent corruption of the primary output. It sits lower mainly because the damage is bounded and predictable, whereas P1 leaves a structurally wrong row count that is much harder to detect after the fact.

---

### [P3] A duplicate title hands the user a raw database constraint error

**Severity**: Medium

**Location**: `problems/views.py`, `migrate_to_db` branch; constraint `problems_problem_slug_key`

**Description**:

Two Polygon problems called "Two Sum" both slugify to `two-sum`, and `Problem.slug` is `unique=True`. The second migration dies on the constraint and the user sees:

```
Migration failed and all changes have been rolled back. Reason:
duplicate key value violates unique constraint "problems_problem_slug_key"
```

The message confirms something failed and that the transaction rolled back. It does not say which problem conflicted, that the title is the cause, or what to do next.

**Impact**:

Reading a PostgreSQL constraint name backwards to "these two problems share a title" is not a reasonable thing to ask of a user. Most will assume the database or the migration is broken.

There is also nowhere to go from there. The slug is not editable anywhere in the interface, so the only exits are changing the title on Polygon or editing the database by hand.

**Suggested Fix**:

Catch `IntegrityError` around the problem write on its own and report it in product language: which title is taken, which slug it produced, and the Polygon ID that already holds it. Offering to disambiguate automatically, by appending the Polygon ID to the slug, would turn a dead end into a decision.

**Why this rank**: it is a genuine dead end with no in product route out, and duplicate titles are common on Polygon where "Two Sum" and friends get reused constantly. It ranks below P1 and P2 because nothing is corrupted and the failure is loud, so it gets reported immediately rather than sitting there.

---

## Code Issues

> Code issues are technical problems: bugs, security vulnerabilities, performance problems, code quality concerns, architectural issues.

### [C1] Object storage uploads run inside the database transaction

**Severity**: High

**Location**: `problems/views.py:207` opens `transaction.atomic()`; `problems/views.py:233` calls `api.migrate_to_storage(...)` inside it

**Description**:

`with transaction.atomic():` opens at line 207. The `migrate_to_azure` branch is the first thing inside it, and the upload happens at line 233. A twelve test problem is 24 sequential HTTPS round trips to Backblaze, all of them inside an open PostgreSQL transaction.

This is also where the compensation code comes from. When something fails we have to delete objects we already wrote, in a separate `except` block, because rolling back the database has no effect on the bucket. There is a comment in the view warning that this cleanup must never mask the original error, which is the kind of comment you only write after getting it wrong once.

**Impact**:

A database connection is held for the whole upload. Under concurrency that is pool capacity burned on network latency, and you will see unrelated requests queue up behind it.

Transaction length scales with test count and cloud latency rather than with database work. A hundred test problem is two hundred round trips inside a transaction.

The nastiest part is that partial failure stops being exceptional and becomes normal, and recovery depends on hand written compensation that has to be kept correct as the flow changes. It also pushes in the wrong order: we upload before the database commit, so storage can end up populated for a row that never committed.

**Suggested Fix**:

Two steps, in this order. Move the upload out of the transaction: commit the database work, then upload, and on failure mark the problem as needing a retry instead of trying to unwind storage. If you want a real guarantee, write an outbox row inside the transaction and process uploads in a worker, which makes the database the single source of truth and deletes the compensation path entirely.

**Why this rank**: it is the structural problem sitting underneath P1 and underneath the rollback complexity. Fixing it properly removes a category of bugs rather than one instance, and it is what makes the other flows safe to change.

---

### [C2] Test cases are synced with a positional per-row save loop

**Severity**: Medium

**Location**: `problems/views.py:528-556`, mirrored by the sample loop at `problems/views.py:466`

**Description**:

Each test is paired with an existing row by its position in the ordered list, then saved individually:

```python
existing_test_cases = list(ProblemTestCase.objects.filter(problem=problem_obj).order_by('order'))
...
    ptc = existing_test_cases[idx]
    ptc.save()
```

That is one UPDATE per test where `bulk_update` or `update_or_create` would do. The same shape is repeated for `SampleTestCase`.

**Impact**:

Two round trips per test case. A hundred test problem issues two hundred statements instead of two, and it pays that cost inside a transaction, which is the worst possible place to pay it given C1.

Matching on position is also brittle. If the ordering ever differs from Polygon's it will quietly rewrite the wrong row, and it is the direct cause of the surplus rows in P1, since rows beyond the end of the fetched list are simply never visited.

The duplicated shape of the two loops means a fix applied to one has to be remembered in the other.

**Suggested Fix**:

Index the existing rows by `order` in a dict rather than trusting list position, delete the keys that no longer appear, and write the rest with `bulk_create` and `bulk_update`. Pull the shared logic out so the sample and regular paths cannot drift.

**Why this rank**: a clear performance and maintainability problem with a correct fix available. It only produces wrong results through the surplus row behaviour already counted as P1, and on its own it does not corrupt anything.

---

### [C3] The Redis cache is never checked against Polygon, so it can be quietly out of date

**Severity**: Medium

**Location**: `problems/polygon_api.py:413` (`get_all_test_cases`), `problems/polygon_api.py:843` (`get_test_cases_from_redis`), consumed at `problems/views.py:517`

**Description**:

Anything that needs test cases asks Redis first and only calls Polygon on a miss:

```python
test_cases = api.get_test_cases_from_redis(polygon_id)
if test_cases is None:
    test_cases = api.get_all_test_cases(polygon_id)
    api.cache_test_cases(polygon_id, test_cases)
```

Entries expire after half an hour and are cleared on rollback. Nothing compares the cache against Polygon's `problem.tests`, which is the one cheap call that reports the current test count, so nothing notices that the setter has changed the problem.

**Impact**:

For up to thirty minutes after an edit, the tool reports the previous contents as current. A migration in that window writes outdated rows, uploads outdated files and reports success.

This is also what makes P1 awkward to reproduce. You can re-run the migration after changing a problem on Polygon, see no change at all, and conclude the bug is intermittent when in fact the second run read the same cached copy.

Worth being explicit about: the cache feeds the destructive path, not just the read path. A stale entry can be the thing that gets written.

The key contains only the Polygon ID, so there is no way for it to distinguish one version of a problem from another.

**Suggested Fix**:

Call `problem.tests` first every time. It is one cheap request and it tells you the current count, which is enough to invalidate and refetch when it disagrees. At minimum, show the cache age in the UI so a person can tell fresh data from cached data. If Polygon exposes a version or update timestamp, keying on that would make this exact rather than approximate.

**Why this rank**: it is the correctness problem hardest to diagnose in production, because the failure depends on timing and shows up intermittently, and it silently amplifies P1. Medium rather than High only because the half hour window is bounded and self healing.

---

<!-- Keep this section to three product issues, ranked from highest to lowest priority. -->

---

## Edge Case Analysis

### Q1

> A Polygon problem has 0 sample test cases but 15 regular test cases. What happens when you migrate this problem?

**Tested.** I built this exact shape and migrated it against a real PostgreSQL database.

It works. Nothing objects to a problem having no samples. Afterwards:

* `problems_sampletestcase`: **0 rows**
* `problems_problemtestcase`: **15 rows**, every one with `is_sample = false`
* `problems_problem`: 1 row with `test_case_count = 15`

The reason is that samples are never guessed from the data. They come from the `useInStatements` flag Polygon puts on each entry in `problem.tests`, and with no sample tests that flag is false for all fifteen. The view only creates `SampleTestCase` rows for entries where it is true, so for that model the loop simply never runs. `ProblemTestCase` rows are created for every test regardless, which is where the fifteen come from.

So the page renders a "Sample Test Case" column reading "No" down all fifteen rows, which is accurate rather than broken. Worth noting that nothing anywhere requires a minimum of three samples, so a problem with none is indistinguishable from any other completed migration.

*Tested versus read*: row counts, the `is_sample` values and the absence of any error were observed. The explanation involving `useInStatements` is from reading `get_all_test_cases()` and the sample branch of the view.

---

### Q2

> A problem is migrated with 20 test cases. Later, the problem setter removes 8 test cases on Polygon (now 12 remain). The problem is re-migrated. What happens?

**Tested on both the cold and the warm cache path.** This is P1 in practice.

**Cold cache, the interesting one.** After re-migrating:

* `problems_problemtestcase`: **20 rows**, count unchanged. **Nothing deleted.**
* Rows 1 to 12 rewritten with current content.
* Rows 13 to 20 untouched, still holding the eight withdrawn tests.
* UI reports "Test cases description migrated to database Successfully."
* Object storage is correct, holding 12 test cases, because the upload path replaces the whole `test_cases/{id}/` prefix.

**Warm cache, the more misleading one.** If a previous migration warmed Redis within the last thirty minutes, the re-migration reads the cached twenty rather than the current twelve. It rewrites all twenty rows with the values they already had and reports success. Nothing changes, and it looks like a clean run rather than a skipped one, which is arguably worse than an error because it invites no further attention.

The mechanism is the `if idx < len(existing_test_cases)` guard at `views.py:530`. Once `idx` hits 12 the guard fails and no new rows are created, but nothing ever walks the rest of `existing_test_cases` to remove them. The surplus is invisible because the success message only reports that the operation completed.

*Tested versus read*: row counts, survival of the eight removed tests, the storage prefix and both messages were observed. The guard and the absence of any delete are from reading the loop.

---

### Q3

> Two different Polygon problems have the exact same title: "Two Sum". You migrate the first one successfully. Then you try to migrate the second one. What happens?

**Tested.** The first commits. The second fails at the database, not at Polygon.

* It raises `IntegrityError` naming `problems_problem_slug_key`.
* It fails inside `transaction.atomic()`, so **nothing partial is left behind**. I checked specifically: no second `Problem` row, no orphaned `ProblemTag` rows, no `SampleTestCase` or `ProblemTestCase` rows for the second problem. The rollback is clean.
* No objects reach cloud storage, because the write fails before the upload stage is reached.
* The user gets HTTP 200 and "Migration failed and all changes have been rolled back. Reason: duplicate key value violates unique constraint "problems_problem_slug_key""

The cause is `slugify("Two Sum")` colliding, with `Problem.slug` declared `unique=True`. Note that `update_or_create` is keying on `polygon_id`, which differs between the two problems, so it correctly attempts an insert rather than updating the first one. Keying on `polygon_id` is right and is not the problem here. The collision is on the derived slug, which nothing checks before the write.

Practically, since the slug is not editable in the interface, there is no way out of this from the UI. That is the argument for P3: name the conflict properly and offer to disambiguate the slug, perhaps by appending the Polygon ID.

*Tested versus read*: the constraint name, the absence of partial rows, the clean tag and test case state, the storage behaviour and the exact wording were all observed. The role of `slugify` and the `unique=True` declaration are from reading the view and the model.

---

### Q4

> When test cases are saved to the database via "Migrate Test Cases to DB", some data is intentionally discarded. What data is lost? Why might this cause problems?

**Tested and read.** Four separate losses, all on the path that writes `ProblemTestCase` and `SampleTestCase`.

**Input and output are cut to 260 characters.** `views.py:537-539` slices `input_data[:260]` and `output_data[:260]`. Everything past that is gone from the database copy for good. The comment calls it bytes; it is characters.

**Input and output are right stripped.** `views.py:531-532` applies `.rstrip()` before truncating, so trailing whitespace and newlines go too.

**Descriptions disappear on the cached path.** Polygon supplies a `description` only for tests it marks `manual`. Generated tests carry neither input nor description in the `problem.tests` payload, and the description does not survive the Redis round trip. On a warm cache the `description` column is written empty even for tests that do have one.

**The `index` and `manual` markers are never stored.** Polygon returns both from `problem.tests`. `ProblemTestCase` has no column for either, so they are read and dropped. There is no record of which tests were hand written and which were machine generated.

Why this causes problems:

* The database ends up an incomplete copy with no field recording the incompleteness. Anything reading it downstream, a judge, a reviewer, a checker, has no way to tell it has been cut short.
* Trailing whitespace can matter to a checker comparing raw output, so stripping it can turn a correct solution into a wrong verdict.
* Losing `manual` and `index` removes the ability to separate curated tests from generated ones, which is usually the first thing anyone wants when a failure is being investigated.
* None of it is visible in the UI. Previews render from the full fetched data, so the page looks right while the stored row is not. That mismatch is P2.
* Object storage still holds the complete bytes. The copies disagree and only one is lossy, so anyone checking the bucket will conclude the database is at fault.

The fix is the one from P2: keep the full text in the database, truncate for display only, and add columns for `manual` and the original `index` if that distinction is worth keeping.

*Tested versus read*: the truncation and stripping, the empty descriptions on the cached path and the mismatch between the database copy and the stored object were all observed. The claim that `index` and `manual` have no home in the model is from reading `problems/models.py` and the write loop.

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

**Things I fixed rather than listed.** Working through this I found and corrected a stored cross site scripting hole in the tag rendering path, a migration blocker caused by two model fields having no migration, a zero padded object key that broke the required layout, and two provider faults that only appeared against a live S3 endpoint. They are mentioned so the reviewer knows they were found, but they are not in the ranked lists because they are not defects any more.

**Two things that look like bugs and are not.** Object keys use the database `Problem.id` rather than the Polygon ID, and the storage button stays disabled until a database row exists. That ordering looks strange but the storage layout requires it, so I left it alone. Separately, raw exception text is shown to the user on failure. It reads like leaked internals and I did try to replace it with something tidier, which broke five tests and made things worse: for a duplicate title the constraint name is the only signal available. I reverted that and now only step in when the message would otherwise be empty.

**The structural note.** `index()` is one view of roughly 590 lines covering authentication, fetching, parsing, four migration paths, rollback and compensation. Every finding above lives in that function. Splitting it into one view per action would make the flows independently testable, and would let the upload in C1 move out of the transaction without restructuring everything around it.

**What I did not cover.** I did not load test this, so I cannot put a number on how C1 behaves under concurrency. I also never exercised a problem with more than about a hundred test cases, so the cost figures in C2 are extrapolated from the per row pattern rather than measured.
