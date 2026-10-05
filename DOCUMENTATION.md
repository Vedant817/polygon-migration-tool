# Functionality Documentation

This is a walkthrough of Hatsu, the Polygon migration tool, written for whoever picks it up next. It follows a click all the way through: what the browser sends, what Django does with it, which external service gets called, what ends up in PostgreSQL or in the bucket, and what finally appears on screen.

If you are looking for one specific action, jump to the flows. If you are trying to understand why the code is shaped the way it is, start with [Why it is built this way](#why-it-is-built-this-way) at the end.

## Contents

- [The short version](#the-short-version)
- [User flows](#user-flows)
  - [Login](#1-login)
  - [Fetching a problem](#2-fetching-a-problem-from-polygon)
  - [Migrating the problem row](#3-migrating-the-problem-to-the-database)
  - [Migrating the test case rows](#4-migrating-test-cases-to-the-database)
  - [Uploading the files](#5-uploading-test-cases-to-cloud-storage)
- [How it talks to Polygon](#how-it-talks-to-polygon)
- [How storage is organised](#how-storage-is-organised)
- [Redis](#redis)
- [When things go wrong](#when-things-go-wrong)
- [Configuration](#configuration)
- [Why it is built this way](#why-it-is-built-this-way)

---

## The short version

Everything happens on one page, and every action on that page is a POST to `/`. A hidden field tells the view which of four things to do:

| Hidden POST field | What runs |
|---|---|
| *(none)* | fetch the problem and display it |
| `migrate_to_db=1` | save the problem row and its tags |
| `migrate_test_cases_to_db=1` | save the test case rows |
| `migrate_to_azure=1` | upload the test case files |

That is the whole request surface. `problems/views.py` has a single view, `index`, which branches on those four values. The field name `migrate_to_azure` is a leftover from when Azure was the only option and it never got renamed, which is worth knowing before you grep for it.

Behind the view, three collaborators do the real work:

- `PolygonAPI` in `problems/polygon_api.py` handles signing, retries, pacing and the Redis cache.
- `BlobStorage` in `problems/storage.py` is an interface with four implementations behind it. `get_storage()` picks one from an environment variable.
- `sanitize_html` in `problems/html_sanitize.py` filters problem content before it reaches the template.

Access is restricted at the view with `@user_passes_test(lambda u: u.is_authenticated and u.is_staff, login_url='/users/login/')`, so anonymous visitors get bounced to the login page and authenticated non staff users are turned away.

---

## User flows

### 1. Login

The user opens `/`, gets redirected to `/users/login/`, types an email and a password, and submits. The form posts `email`, `password` and a CSRF token to `/users/login/`.

`users/views.py:login_view` does something slightly unusual first: if the visitor already has a session, it logs them out before rendering the page. That way you cannot end up sitting on the login form while already signed in.

Authentication goes through `authenticate(request, username=email, email=email, password=password)`. The same value is passed for both `username` and `email` because the custom user model keeps the email address in the `username` column, and Django's default `ModelBackend` only knows how to match on `username`.

A successful login is checked against `user.is_staff`. Staff users get `login(request, user)`, which writes a row to `django_session` and sets the session cookie, then get redirected to the home page. A valid account that is not staff gets the message "You do not have staff access." and stays put. A wrong password gets "Invalid email or password." The wording is deliberately identical in the last two cases so the form does not confirm which addresses exist.

Once in, the navbar shows "Welcome, {first_name} {last_name}" next to a Logout button.

On the database side this flow reads one row from `auth_user` and writes one row to `django_session`. It calls no external API.

One setup gotcha worth repeating: `createsuperuser --noinput` fails on this project. `users.User.REQUIRED_FIELDS` lists `contact_number`, `college`, `graduation_year` and `gender`, so a scripted superuser needs all four even though the interactive prompt does not ask for them in that order.

### 2. Fetching a problem from Polygon

The user types a Polygon problem ID into the one text field at the top of the page and presses "Fetch Problem". The POST carries `problem_id` and the CSRF token.

Before the request goes anywhere, a submit handler in `index.html` paints a full screen overlay and then submits the form 50 milliseconds later. The delay exists purely so the spinner is actually visible, since a navigation started in the same tick would replace the page before anything renders.

`index` reads `problem_id` straight out of `request.POST`. Note that it never casts it to an integer, so whatever the user typed is what gets sent to Polygon. That is why a non numeric ID produces a Polygon error rather than a friendly local validation message.

With that in place, the view creates a `PolygonAPI()` and asks for the test cases, but it checks Redis first. `get_test_cases_from_redis()` looks for `polygon_migration_test_cases_{id}` and, on a hit, skips every per test API call. On a miss it walks the full sequence:

1. `get_problem_info()` calls `problem.info` for the title, time limit, memory limit and checker name.
2. `_normalize_checker_type()` cleans that checker name up. It strips the `std::` prefix and the `.cpp` suffix and falls back to `custom` for anything outside the model's own choices, so the value written to the database can never drift away from the choices declared on the model.
3. `get_statements()` calls `problem.statements`, and `download_and_extract_package()` pulls `problem.package`, which is a ZIP. The archive is opened in memory to get at `problem.html` and, when there is one, the checker source.
4. `parse_problem_html()` parses that HTML with `lxml.html` and lifts out the `legend`, `input-specification`, `output-specification` and `note` sections.
5. `get_all_test_cases()` calls `problem.tests` for the index list, then `problem.testInput` and `problem.testAnswer` for each test as plain text methods. If any test fails to answer, the method sets `last_fetch_incomplete`.
6. `cache_test_cases()` writes the result into Redis, and refuses to write anything if the fetch was incomplete.

The reference solution is not fetched on this path. It only comes back once the problem exists in the database, which is why it is missing the first time you look at a problem and appears later.

The result goes into `context['fetched_problem']`. The six free text HTML fields pass through `sanitize_html()` on the way to the template, because this content came from someone else's server and the template has to mark it `|safe` for the formatting to render. The write path uses the original, unfiltered strings, so the database keeps exactly what Polygon returned.

This flow reads `problems_problem` by `polygon_id`, purely to decide whether to show the database ID, the Algopath link and the solution. It writes nothing.

The user gets the metadata table (slug, difficulty, tags, limits, checker type, test case count), the statement and formats, and a table of test case previews. Previews are cut short on the server, and anything over 50 characters gets a "View Full" button that opens the rest in a modal.

### 3. Migrating the problem to the database

The user picks a difficulty, adds at least two tags, and presses "Create/Update problem in Database". The POST carries `problem_id`, `migrate_to_db=1`, `difficulty`, and one `tags` value per tag.

The difficulty `<select>` is mirrored into a hidden input and the tag chips are serialised into hidden inputs by `updateHiddenInputs()`. So the browser posts back exactly the choices on screen, which matters because the tag chips are built in JavaScript and are not form controls themselves.

The `migrate_to_db` branch refetches the problem through the same path as flow 2 rather than trusting what was on screen. It is slower, but it means you always save current data.

Validation happens before any write. A missing difficulty raises immediately with "Please select a difficulty level before migrating to database." Fewer than two tags is refused in the same way. Only after both pass does it compute `slugify(title)` and open the transaction.

Inside `transaction.atomic()`, the row is written with `Problem.objects.update_or_create(polygon_id=..., defaults=...)`. Keying on `polygon_id` rather than title or slug is what makes a repeat run update in place instead of creating a duplicate. `ProblemTag` rows are created on demand and attached through the `extra_tags` many to many.

Once that commits, the test case rows are written too. That is flow 4, and clicking this button runs both.

On success the page sets `db_success` and `success`, and re-attaches `fetched_problem` so the metadata table can show the new numeric ID and the Algopath link built from the slug. This flow touches `problems_problem`, `problems_problemtag` and the `extra_tags` join table.

Worth noting the ordering constraint: the storage buttons stay disabled until a database row exists, because the object key is built from the database `Problem.id` rather than the Polygon ID. That is not an oversight, it is required by the storage layout.

### 4. Migrating test cases to the database

The user presses "Migrate Test Description to Database" under the test case table. The POST carries `problem_id` and `migrate_test_cases_to_db=1`.

This branch refuses to run unless a `Problem` row already exists, and says so plainly: "Please migrate the problem to the database first." Test cases come from Redis if they are warm, and from Polygon if they are not.

Samples are separated from regular tests using the `useInStatements` flag that Polygon returns per test in `problem.tests`. Inside the transaction, the existing `SampleTestCase` and `ProblemTestCase` rows for the problem are deleted and rewritten. Samples get a `SampleTestCase` row; every test, samples included, also gets a `ProblemTestCase` row, which is why a problem with 3 samples and 9 regular tests ends up with 12 rows in `ProblemTestCase` and 3 in `SampleTestCase`.

Before writing, each field is right stripped and cut to 260 characters. That loss is real and it is the subject of edge case Q4 in `ISSUES.md`, along with what it means for anyone reading the database afterwards.

The page reports "Test cases description migrated to database Successfully."

### 5. Uploading test cases to cloud storage

The user presses "Migrate Test Cases to Cloud Storage". The POST carries `problem_id` and `migrate_to_azure=1`, and as noted above the field name is historical.

This branch also refuses without a database row, since the object key needs the database ID.

`get_storage()` builds the backend named by `STORAGE_PROVIDER`: `LocalBlobStorage`, `AzureBlobStorage` or `S3BlobStorage`. If configuration is incomplete it raises `BlobStorageError` naming the missing variable, rather than failing later with something less obvious. `storage.ensure_container()` then creates the bucket or container if it is not there.

`api.migrate_to_storage(polygon_id, db_problem_id, testset)` loops the test cases and calls `storage.upload_test_case()` for each, which writes two objects using keys from `build_test_case_key()`. A problem with a custom checker additionally gets the checker source uploaded, and the compiled binary if `g++` happens to be available.

If something fails partway through, everything already written is removed with `storage.delete_prefix(build_problem_prefix(problem_id))`, so a partial upload is not left lying around. The method returns `(uploaded_count, skipped, checker_key)`, and any test that came back with an empty input or output is counted as skipped and named in the success message. Silently dropping tests and then reporting a cheerful count was a real bug, so the skip list is surfaced rather than swallowed.

For a 12 test problem with no custom checker you get 24 objects:

```
test_cases/7/1     test_cases/7/1.a
test_cases/7/2     test_cases/7/2.a
...
test_cases/7/12    test_cases/7/12.a
```

`7` there is the database `Problem.id`. Numbers are not zero padded, so the tenth test is `10` and never `010`. Objects are written as raw bytes with no transformation, so trailing newlines and CRLF line endings survive exactly as Polygon sent them.

This flow writes nothing to the database. The upload does not update the problem row.

---

## How it talks to Polygon

Base URL is `https://polygon.codeforces.com/api/`, and the client is `PolygonAPI`. Eight endpoints are used:

| Endpoint | Called by | Gives back |
|---|---|---|
| `problem.info` | `get_problem_info()` | JSON: title, limits, checker name |
| `problem.statements` | `get_statements()` | JSON: language and statement id |
| `problem.package` | `download_and_extract_package()` | ZIP of the problem directory |
| `problem.tests` | `get_test_cases()` | JSON array, one `useInStatements` flag per test |
| `problem.testInput` | `get_all_test_cases()` | plain text input |
| `problem.testAnswer` | `get_all_test_cases()` | plain text expected output |
| `problem.checker` | `get_custom_checker_info()` | JSON describing a custom checker |
| `problem.script` | `get_test_script()` | JSON, used for solution and generator scripts |

The two methods that return bare text go through `_make_plain_request()` rather than `_make_request()`, so they never try to JSON decode a test input that happens to start with a brace. A plain text `FAILED` envelope is raised as an error instead of being handed back as if it were file content, which was another real bug.

Every request is signed by `_generate_api_sig()`. It adds `apiKey` and `time` to the parameters, sorts them lexicographically, URL encodes them, generates six random lowercase alphanumeric characters as a nonce, and hashes this string with SHA-512:

```
{rand}/{method}?{sorted_params}#{api_secret}
```

`apiSig` is the nonce followed by the hex digest. The secret is never transmitted and never logged; only non secret parameters reach the log lines.

Four reliability measures in `_make_request()` exist because each one was added after watching it fail against the live service:

- `REQUEST_TIMEOUT = (10, 120)`, so ten seconds to connect and 120 to read. Without the read timeout a stalled connection hangs the request indefinitely.
- `MIN_REQUEST_INTERVAL = 0.25`, a floor on the gap between calls. Polygon rate limits aggressively and a fast loop earns HTTP 429 quickly.
- Up to `REQUEST_ATTEMPTS = 4` retries for 429 and 5xx, backing off linearly from `RETRY_BACKOFF_SECONDS = 1.0`.
- A bad or missing signature comes back as HTTP 400 rather than a 200 with a `FAILED` body, so it surfaces as a request error.

---

## How storage is organised

Configuration comes entirely from the environment, read through `get_storage()`:

| `STORAGE_PROVIDER` | Class | Needs |
|---|---|---|
| `azure` | `AzureBlobStorage` | `AZURE_STORAGE_ACCOUNT_URL`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_USERNAME`, `AZURE_PASSWORD` |
| `s3` | `S3BlobStorage` | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION_NAME`, plus `S3_ENDPOINT_URL` for R2, B2 or MinIO |
| `local` | `LocalBlobStorage` | `STORAGE_LOCAL_DIR` |

`STORAGE_CONTAINER_NAME` names the container or bucket in all three cases. Azure is the original implementation and is preserved intact.

`BlobStorage` defines `ensure_container`, `upload_bytes`, `upload_text`, `delete_prefix`, `list_keys`, `read_bytes`, `read_text` and the convenience wrapper `upload_test_case`. Every backend implements the same eight methods, so switching provider is a one line change in `.env`. The view never receives a storage object and never passes provider arguments, it calls `get_storage()` itself, and no migration flow contains provider specific code. That was the point of the refactor.

All naming lives in two functions, `build_test_case_key()` and `build_problem_prefix()`, so the required layout is identical no matter which backend is active and there is a single place to change it.

Three provider specific wrinkles are worth knowing:

- `S3BlobStorage.ensure_container()` leaves out `LocationConstraint` when the region is `auto` (Cloudflare R2) or `us-east-1`, and retries creation without it, because Backblaze B2 rejects the parameter outright.
- The same method treats a 403 from `HeadBucket` as "the bucket is probably fine" and carries on. A key scoped to one bucket is often not allowed to list bucket names, so `HeadBucket` answers 403 for a bucket that works perfectly. Genuine credential errors still raise.
- `LocalBlobStorage._path()` rejects any key containing `..`, so a crafted key cannot write outside the storage root.

---

## Redis

Redis exists to keep us off Polygon's rate limit. Keys are `polygon_migration_test_cases_{polygon_id}` with a half hour expiry.

`get_test_cases_from_redis()` is the read path, `store_test_cases_in_redis()` the write path, and `cache_test_cases()` refuses to cache a partial fetch so a throttled or failed fetch cannot poison the cache with incomplete data. `clear_test_cases_from_redis()` runs during rollback compensation.

If Redis drops a connection the error is logged and treated as a cache miss, and the request falls through to Polygon. That is deliberate: the cache is an optimisation, so it should never be the thing that fails a migration.

---

## When things go wrong

`index` wraps every action in `transaction.atomic()`. When an exception escapes:

1. The exception and traceback go to the log with `logger.error(..., exc_info=True)`.
2. `context['error']` is set. The underlying reason is included on purpose. For a duplicate title, the `IntegrityError` text naming the unique slug constraint is the only clue the user gets, so a generic "something went wrong" would be worse than ugly.
3. Any success message set earlier in the same request is dropped. The transaction rolled back and the compensation may have deleted objects it claimed to upload, so the success is no longer true.
4. Objects uploaded during the failed request are deleted with `delete_prefix()`.
5. The Redis cache is cleared.
6. A failure inside the compensation step is logged but never allowed to replace the original error.

The page renders with HTTP 200 and a red `alert-danger`, so a failed migration never leaves someone staring at a blank error page.

---

## Configuration

Everything comes from environment variables via `python-dotenv` in `PolygonMigration/settings.py`. `.env.example` documents each one with placeholder values only.

| Variable | Purpose |
|---|---|
| `POLYGON_API_KEY`, `POLYGON_API_SECRET` | request signing |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | PostgreSQL |
| `REDIS_HOST`, `REDIS_PORT` | Redis |
| `STORAGE_PROVIDER` | `azure`, `s3` or `local` |
| `STORAGE_CONTAINER_NAME` | container or bucket name |
| `S3_*`, `AZURE_*`, `STORAGE_LOCAL_DIR` | provider credentials |

On the machine this was built on, host ports 5432 and 6379 were already occupied by unrelated containers, so PostgreSQL runs on 55432 and Redis on 56379, with the dev server on 8765. Those are local choices and nothing in the application depends on them.

---

## Why it is built this way

A few decisions look odd until you know the reason, so here they are in one place.

**One view, four branches.** `index` is around 590 lines and handles authentication, fetching, parsing, four migration paths, rollback and compensation. Splitting it into one view per action is the obvious next refactor. It would make each flow independently testable and would let the storage upload move out of the transaction, which is where the most awkward code in the project lives.

**The upload runs inside the transaction.** This is a known wart, written up as C1 in `ISSUES.md`. Holding a database connection open across 24 HTTPS round trips is not great, and it is the reason the compensation logic has to exist at all: a database rollback cannot undo object writes.

**Raw exception text reaches the user.** It looks like leaked internals, and I spent time trying to hide it. That was wrong. For a duplicate title the constraint name in that message is the only thing telling the user what happened, and there is no other channel for it. The compromise I settled on is to keep the text and only intervene when it would be empty.

**Object keys use the database ID.** Required by the storage layout, and the reason the storage buttons are disabled until a database row exists.

**Problem content is sanitised, then marked safe.** The statement and formats come from a remote server and the template needs `|safe` for the formatting to render at all. Rather than choose between broken formatting and an XSS hole, the content goes through an allow-list filter first. The filter lives in `problems/html_sanitize.py` and uses `beautifulsoup4`, which was already a dependency, so nothing new was added to `requirement.txt`.

**MathJax is loaded from a CDN.** Codeforces writes maths as `$$$x$$$`, which is meaningless as plain text. `pre` and `code` are in `skipHtmlTags` so the reference solution is never mistaken for maths. If the machine is offline the text still reads fine, only the typesetting is lost.
