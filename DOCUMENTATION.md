# Functionality Documentation

Polygon Migration Tool (Hatsu). This document traces each user action from the browser, through the Django backend, to PostgreSQL, Redis, the Polygon API and cloud object storage. It is written for an engineer taking over the tool.

## Table of contents

1. [Architecture at a glance](#1-architecture-at-a-glance)
2. [User flows](#2-user-flows)
   * [Flow 1: User login](#flow-1-user-login)
   * [Flow 2: Fetching a problem from Polygon](#flow-2-fetching-a-problem-from-polygon)
   * [Flow 3: Migrating a problem to the database](#flow-3-migrating-a-problem-to-the-database)
   * [Flow 4: Migrating test cases to the database](#flow-4-migrating-test-cases-to-the-database)
   * [Flow 5: Uploading test cases to cloud storage](#flow-5-uploading-test-cases-to-cloud-storage)
3. [External integrations](#3-external-integrations)
   * [Polygon API](#polygon-api)
   * [Cloud storage](#cloud-storage)
   * [Redis](#redis)
4. [Error handling and rollback](#4-error-handling-and-rollback)
5. [Configuration reference](#5-configuration-reference)

---

## 1. Architecture at a glance

A single Django project, `PolygonMigration`, with two apps.

| Component | Location | Responsibility |
|---|---|---|
| Project settings | `PolygonMigration/settings.py` | Reads all configuration from environment variables via `python-dotenv` |
| URL routing | `PolygonMigration/urls.py` | Mounts `problems/` at `/` and `users/` at `/users/` |
| Main view | `problems/views.py` (`index`) | Handles every migration action from one page |
| Problem templates | `problems/templates/problems/index.html` | Single page containing all forms |
| Polygon client | `problems/polygon_api.py` (`PolygonAPI`) | Signing, retries, rate limiting, Redis cache, uploads |
| Storage abstraction | `problems/storage.py` (`BlobStorage`) | One interface, four implementations |
| HTML sanitiser | `problems/html_sanitize.py` (`sanitize_html`) | Allow-list filter for remote problem content |
| Models | `problems/models.py` | `Problem`, `SampleTestCase`, `ProblemTestCase`, `ProblemTag` |
| Authentication | `users/views.py` | Email and password login restricted to staff |

There is one page and one view. Every action is a POST to `/` with a different hidden field telling `index()` which branch to take:

| POST field | Branch | Section |
|---|---|---|
| none | Fetch and display a problem | [Flow 2](#flow-2-fetching-a-problem-from-polygon) |
| `migrate_to_db=1` | Save problem metadata | [Flow 3](#flow-3-migrating-a-problem-to-the-database) |
| `migrate_test_cases_to_db=1` | Save test case rows | [Flow 4](#flow-4-migrating-test-cases-to-the-database) |
| `migrate_to_azure=1` | Upload files to object storage | [Flow 5](#flow-5-uploading-test-cases-to-cloud-storage) |

Access control: `index` is wrapped in `@user_passes_test(lambda u: u.is_authenticated and u.is_staff, login_url='/users/login/')`, so anonymous visitors are redirected and non staff authenticated users are refused.

---

## 2. User flows

### Flow 1: User login

**What the user does.** Opens `/`, is redirected to `/users/login/`, enters an email address and password, and submits.

**What the frontend sends.** A POST to `/users/login/` with two fields, `email` and `password`, plus Django's `csrfmiddlewaretoken`.

**What the backend does, in order.**

1. `users/views.py:login_view` logs out any already authenticated session first, so the login page can never be reached mid session.
2. `authenticate(request, username=email, email=email, password=password)` runs against Django's default `ModelBackend`. The custom user model stores the email as `username`, which is why the same value is passed for both.
3. On success it checks `user.is_staff`. Staff users get `login(request, user)`, which writes the session to `django_session` and sends the session cookie. The user is redirected to `problems:index` (`/`).
4. A valid user who is not staff gets the message "You do not have staff access." and stays on the login page. An invalid pair gets "Invalid email or password." The two cases are deliberately indistinguishable in wording.
5. On GET the view renders `users/templates/users/login.html`.

**External APIs called.** None.

**Database reads and writes.**

* Read: `auth_user` (the `User` row, its password hash and `is_staff` flag).
* Write: `django_session` (one row per login). `auth_user` is not modified.

**What the user sees.** The navbar on `/` reads "Welcome, {first_name} {last_name}" with a Logout button. On failure, a red `alert-danger` above the form.

**Note.** `createsuperuser --noinput` fails on this project. `users.User.REQUIRED_FIELDS` includes `contact_number`, `college`, `graduation_year` and `gender`, so those must be supplied even in scripted setup.

---

### Flow 2: Fetching a problem from Polygon

**What the user does.** Types a Polygon problem ID into the single text input at the top of the page and clicks "Fetch Problem".

**What the frontend sends.** POST to `/` with `problem_id=<id>` and the CSRF token. `problems/templates/problems/index.html` intercepts the submit in JavaScript, shows a full screen loading overlay, then submits the form 50 ms later so the spinner is painted before the request starts.

**What the backend does, in order.**

1. `index()` (`problems/views.py:107`) reads `problem_id` from `request.POST`. It is not coerced to an integer, so a non numeric value is passed through to Polygon and rejected there.
2. It creates `PolygonAPI()` (`problems/polygon_api.py:51`), which loads `POLYGON_API_KEY` and `POLYGON_API_SECRET` from settings.
3. **Redis is consulted first.** `get_test_cases_from_redis(polygon_id)` looks for the key `polygon_migration_test_cases_{id}`. On a cache hit the per test API calls are skipped entirely.
4. On a miss, `api.get_problem_info(problem_id)` calls Polygon `problem.info` for title, time limit, memory limit and checker name. `_normalize_checker_type()` (`views.py:20`) then strips the `std::` prefix and `.cpp` suffix, and falls back to `custom` for any name outside the model's own choices, so the two cannot drift apart.
5. `api.get_statements()` calls `problem.statements` for the language and statement id, and `download_and_extract_package()` fetches `problem.package`, which returns a ZIP. The archive is read in memory for `problem.html` and the checker source.
6. `parse_problem_html()` (`views.py:37`) parses that HTML with `lxml.html` and pulls the `legend`, `input-specification`, `output-specification` and `note` sections.
7. `api.get_all_test_cases()` (`polygon_api.py:413`) then calls `problem.tests` for the index, and for each test `problem.testInput` and `problem.testAnswer` as plain text methods. It sets `last_fetch_incomplete` if any test fails to answer.
8. `cache_test_cases()` stores the complete set in Redis under the same key, and refuses to cache a partial fetch.
9. The reference solution is fetched only when the problem already exists in the database. On a first time fetch it is not requested.
10. The result is placed in `context['fetched_problem']`. The six HTML content fields are passed through `sanitize_html()` (`problems/html_sanitize.py`) before display. The database write path uses the unsanitised originals.

**External APIs called.** `problem.info`, `problem.statements`, `problem.package`, `problem.html` (from the archive), `problem.tests`, `problem.testInput`, `problem.testAnswer`, and `problem.checker` when a custom checker is present.

**Database reads and writes.**

* Read: `problems_problem`, looked up by `polygon_id` only, to decide whether to show the database ID, the Algopath link and the reference solution.
* Write: none in this flow.

**What the user sees.** The page re renders with the metadata table (slug, difficulty, tags, time limit, memory limit, checker type, test case count), the statement, input format, output format and notes, and a table of test case previews. Each preview is truncated server side; a "View Full" button opens the complete text in a Bootstrap modal. A red alert appears on failure.

---

### Flow 3: Migrating a problem to the database

**What the user does.** Selects a difficulty, types or picks at least two tags, and clicks "Create/Update problem in Database".

**What the frontend sends.** POST to `/` with `problem_id`, `migrate_to_db=1`, `difficulty` and one `tags` value per selected tag. The difficulty `<select>` is mirrored into a hidden input and the tag chips are serialised into hidden inputs by `updateHiddenInputs()`, so the browser submits exactly what the user sees.

**What the backend does, in order.**

1. `index()` takes the `migrate_to_db` branch (`views.py:344`). The problem is fetched again through the same path as Flow 2, so the data saved is current rather than whatever was on screen.
2. Difficulty is validated first. A missing value raises immediately with "Please select a difficulty level before migrating to database."
3. Tags are validated next. Fewer than two is refused.
4. `slugify(title)` produces the slug.
5. The row is written inside `transaction.atomic()`. `Problem.objects.update_or_create(polygon_id=..., defaults=...)` is used, so re-running updates in place rather than creating a duplicate. `ProblemTag` rows are created on demand and attached through `extra_tags`.
6. After the transaction commits, the test cases are written to the database (this is [Flow 4](#flow-4-migrating-test-cases-to-the-database), and it runs as part of the same click).
7. `context['db_success']` and `context['success']` are set, and `fetched_problem` is re attached so the page can show the new database ID and the Algopath link.

**External APIs called.** The same set as Flow 2.

**Database reads and writes.**

* Read: `problems_problem` by `polygon_id`; `problems_problemtag` by name.
* Write: one `problems_problem` row; zero or more `problems_problemtag` rows; the `problems_problem.extra_tags` join rows.

**What the user sees.** A blue `alert-info` reading "Problem '{title}' updated in database successfully." The metadata table now shows the numeric database ID and a clickable `https://www.algopath.ai/problems/{slug}` link. The two storage buttons become enabled.

**Note on ordering.** The storage upload button is disabled until a database row exists, because the object key is built from the database `Problem.id`, not the Polygon ID.

---

### Flow 4: Migrating test cases to the database

**What the user does.** Clicks "Migrate Test Description to Database" below the test case table.

**What the frontend sends.** POST to `/` with `problem_id` and `migrate_test_cases_to_db=1`.

**What the backend does, in order.**

1. `index()` takes the `migrate_test_cases_to_db` branch (`views.py:509`).
2. It refuses unless a `Problem` row already exists for that Polygon ID, telling the user to migrate the problem first.
3. The test cases come from Redis when warm, otherwise from Polygon.
4. Sample tests are separated from regular tests by `useInStatements`, taken from `problem.tests`.
5. Inside `transaction.atomic()`, existing `SampleTestCase` and `ProblemTestCase` rows for the problem are deleted, then rewritten. `SampleTestCase` rows are created for samples only; every test, samples included, also gets a `ProblemTestCase` row.
6. Content is deliberately reduced before storage: each field is truncated to 260 characters and right stripped. This is the loss described in Edge Case Q4 of `ISSUES.md`.

**External APIs called.** `problem.tests`, and per test `problem.testInput` and `problem.testAnswer` only if Redis is cold.

**Database reads and writes.**

* Read: `problems_problem`; `problems_sampletestcase` and `problems_problemtestcase` for deletion.
* Write: rewritten `problems_sampletestcase` and `problems_problemtestcase` rows.

**What the user sees.** A green `alert-success` reading "Test cases description migrated to database Successfully."

---

### Flow 5: Uploading test cases to cloud storage

**What the user does.** Clicks "Migrate Test Cases to Cloud Storage".

**What the frontend sends.** POST to `/` with `problem_id` and `migrate_to_azure=1`. The field name is historical: it predates the provider abstraction and is kept so the flow does not change.

**What the backend does, in order.**

1. `index()` takes the `migrate_to_azure` branch (`views.py:209`). It refuses unless a `Problem` row exists, because the object key needs the database ID.
2. `get_storage()` (`storage.py:411`) builds the backend named by `STORAGE_PROVIDER`. It returns `LocalBlobStorage`, `AzureBlobStorage` or `S3BlobStorage`, and raises `BlobStorageError` naming the missing environment variable if configuration is incomplete.
3. `storage.ensure_container()` creates the bucket or container if it is absent.
4. `api.migrate_to_storage(polygon_id, db_problem_id, testset)` (`polygon_api.py:703`) then, for each test case, calls `storage.upload_test_case(problem_id, test_number, input_data, output_data)`. That method writes two objects through `build_test_case_key()`.
5. If the problem uses a custom checker, `upload_custom_checker_to_storage()` additionally uploads the checker source and, when `g++` is available, the compiled binary.
6. A failure part way through deletes everything uploaded so far via `storage.delete_prefix(build_problem_prefix(problem_id))`, so a partial upload is never left behind.
7. The method returns `(uploaded_count, skipped, checker_key)`. Any test whose input or output came back empty is counted as skipped and named in the success message rather than being silently dropped.

**External APIs called.** `problem.testInput` and `problem.testAnswer` per test if Redis is cold. `problem.checker` when a custom checker is present.

**Storage writes.** For a problem with 12 tests and no custom checker, 24 objects:

```
test_cases/{Problem.id}/1     test_cases/{Problem.id}/1.a
test_cases/{Problem.id}/2     test_cases/{Problem.id}/2.a
...
test_cases/{Problem.id}/12    test_cases/{Problem.id}/12.a
```

`{Problem.id}` is the **database** ID, not the Polygon ID. Numbers are not zero padded, so a 10th test is `10`, never `010`. Objects are written as bytes with no transformation, so trailing newlines and CRLF line endings survive byte for byte.

**Database writes.** None in this flow. The upload does not update the problem row.

**What the user sees.** A green `alert-success` reading "{n} test case(s) migrated to cloud storage successfully.", listing any skipped test numbers. On failure a red alert explains what went wrong and confirms the transaction was rolled back.

---

## 3. External integrations

### Polygon API

Base URL `https://polygon.codeforces.com/api/`. The client is `PolygonAPI` in `problems/polygon_api.py`.

**Endpoints used**

| Endpoint | Called by | Returns |
|---|---|---|
| `problem.info` | `get_problem_info()` | JSON with title, limits, checker name |
| `problem.statements` | `get_statements()` | JSON with language and statement id |
| `problem.package` | `download_and_extract_package()` | ZIP binary of the problem directory |
| `problem.tests` | `get_test_cases()` | JSON array of test objects, one `useInStatements` flag each |
| `problem.testInput` | `get_all_test_cases()` | Plain text input |
| `problem.testAnswer` | `get_all_test_cases()` | Plain text expected output |
| `problem.checker` | `get_custom_checker_info()` | JSON describing the custom checker |
| `problem.script` | `get_test_script()` | JSON, used for solution and generator scripts |

Methods returning text use a separate code path, `_make_plain_request()`, which does not attempt JSON decoding. A plain text `FAILED` envelope is raised as an error rather than returned as file content.

**Authentication.** Every request is signed by `_generate_api_sig()` (`polygon_api.py:62`):

1. `apiKey` and `time` are added to the parameters.
2. Parameters are sorted lexicographically and URL encoded.
3. Six random lowercase alphanumeric characters are generated as a nonce.
4. The string `{nonce}/{method}?{param_string}#{api_secret}` is hashed with SHA-512.
5. `apiSig` is the nonce concatenated with the hex digest.

The secret is never sent and never logged. Only non secret parameters appear in log lines.

**Reliability.** `_make_request()` (`polygon_api.py:97`) applies four safeguards, each added after being observed failing against the live service:

* `REQUEST_TIMEOUT = (10, 120)`, a 10 second connect and 120 second read timeout, so a stalled read cannot hang a request indefinitely.
* `MIN_REQUEST_INTERVAL = 0.25`, a floor on the gap between consecutive calls, to stay inside Polygon's rate limit.
* Up to `REQUEST_ATTEMPTS = 4` retries for HTTP 429 and 5xx, with linear backoff from `RETRY_BACKOFF_SECONDS = 1.0`.
* A bad or absent signature returns HTTP 400, which is surfaced as a request error rather than as a `FAILED` body.

### Cloud storage

Configuration is entirely environment driven, read through `get_storage()`.

| `STORAGE_PROVIDER` | Class | Required variables |
|---|---|---|
| `azure` | `AzureBlobStorage` | `AZURE_STORAGE_ACCOUNT_URL`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_USERNAME`, `AZURE_PASSWORD` |
| `s3` | `S3BlobStorage` | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION_NAME`, and `S3_ENDPOINT_URL` for R2, B2 or MinIO |
| `local` | `LocalBlobStorage` | `STORAGE_LOCAL_DIR` |

`STORAGE_CONTAINER_NAME` names the container or bucket in every case. The Azure path is the original implementation and is preserved unchanged behind the interface.

**The interface.** `BlobStorage` (`storage.py:64`) defines `ensure_container`, `upload_bytes`, `upload_text`, `delete_prefix`, `list_keys`, `read_bytes`, `read_text` and the convenience method `upload_test_case`. Every provider implements the same eight methods, so switching provider is a one line `.env` change. `views.py` never receives a storage object or passes provider arguments; it calls `get_storage()` itself. No migration flow contains provider specific code.

**File naming.** Two module level functions own all naming:

* `build_test_case_key(problem_id, test_number, is_answer)` returns `test_cases/{id}/{n}` or `test_cases/{id}/{n}.a`.
* `build_problem_prefix(problem_id)` returns `test_cases/{id}/`, used for listing and for rollback deletion.

Because both are the single source of truth, the required layout is identical across all four backends, and a test pins the exact strings.

**Provider specific notes.**

* `S3BlobStorage.ensure_container()` omits `LocationConstraint` when the region is `auto` (Cloudflare R2) or `us-east-1`, and retries bucket creation without it, because Backblaze B2 rejects the parameter.
* `S3BlobStorage.ensure_container()` treats a 403 from `HeadBucket` as "the bucket probably exists" and continues, because a key scoped to a single bucket is often not permitted to list bucket names. Genuine credential errors still raise.
* `LocalBlobStorage._path()` refuses any key containing `..`, so a crafted key cannot escape the storage root.

### Redis

Redis caches fetched test cases so repeated views and migrations do not re hit Polygon. Keys are `polygon_migration_test_cases_{polygon_id}`, with a default expiry of 0.5 hours.

The read path is `get_test_cases_from_redis()` and the write path is `store_test_cases_in_redis()`. `cache_test_cases()` refuses to cache a partial fetch, so a rate limited or failed fetch cannot poison the cache with incomplete data. `clear_test_cases_from_redis()` is called during rollback compensation.

A dropped Redis connection is logged and treated as a cache miss, and the request falls through to Polygon.

---

## 4. Error handling and rollback

`index()` wraps every action in `transaction.atomic()`. When an exception escapes:

1. The full exception and traceback are written to the log via `logger.error(..., exc_info=True)`.
2. `context['error']` is set. The message deliberately includes the underlying reason, because for a duplicate title the `IntegrityError` text naming the unique slug constraint is the only signal the user gets.
3. Any success message from earlier in the same request is removed, since the transaction rolled back and any success it claimed no longer holds.
4. Objects uploaded during the failed request are deleted through `storage.delete_prefix()`.
5. The Redis cache is cleared.
6. A failure in the compensation step is logged but never replaces the original error.

The page renders with HTTP 200 and a red `alert-danger`, so a failed migration never leaves the user on a blank error page.

---

## 5. Configuration reference

All configuration comes from environment variables, loaded by `python-dotenv` in `PolygonMigration/settings.py`. `.env.example` documents every variable with placeholder values.

| Variable | Purpose |
|---|---|
| `POLYGON_API_KEY`, `POLYGON_API_SECRET` | Polygon request signing |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | PostgreSQL connection |
| `REDIS_HOST`, `REDIS_PORT` | Redis connection |
| `STORAGE_PROVIDER` | `azure`, `s3` or `local` |
| `STORAGE_CONTAINER_NAME` | Container or bucket name |
| `S3_*`, `AZURE_*`, `STORAGE_LOCAL_DIR` | Provider specific credentials |

Port notes from this setup: host ports 5432 and 6379 were already occupied, so PostgreSQL was published on 55432 and Redis on 56379, and the development server ran on 8765. These are local choices only and do not affect the application.
