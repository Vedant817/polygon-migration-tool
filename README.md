# Polygon Migration Tool (PolygonMigration)

## Overview

PolygonMigration is a Django web application that copies problems from [Polygon](https://polygon.codeforces.com/) into a PostgreSQL database and uploads their test-case files to cloud storage. Staff users fetch a problem, review it, tag it, and migrate it.

## Features
- **Polygon Integration:** Fetch problems and test cases directly from Polygon using API keys.
- **Database Migration:** Store problem statements, metadata, and test cases in a PostgreSQL database.
- **Pluggable Cloud Storage:** Upload test cases to Azure Blob Storage, any S3-compatible service (Amazon S3, Cloudflare R2, MinIO), or a local filesystem. Provider-specific code is isolated behind one interface.
- **Tagging & Metadata:** Add, search, and manage tags and difficulty levels for each problem.
- **Admin Interface:** Manage users, problems, tags, and test cases via Django admin.
- **Custom User Model:** Email-based authentication with extended user profile fields.
- **Staff-Only Access:** Only staff users can access migration features.

## Storage layout

Migrated test cases are always written as:

```
test_cases/{problem_id}/{test_number}      # input file
test_cases/{problem_id}/{test_number}.a    # output file
```

`{problem_id}` is the **database** `Problem.id`, not the Polygon ID.

## Storage providers

The migration flow (`problems.views.index` -> `PolygonAPI.migrate_to_storage`) depends only on the
`problems.storage.BlobStorage` interface. Swapping provider is a settings change, not a code change.

Set `STORAGE_PROVIDER` in `.env`:

| `STORAGE_PROVIDER` | Backend | Extra configuration |
|---|---|---|
| `azure` | `AzureBlobStorage` (azure-storage-blob) | `AZURE_STORAGE_ACCOUNT_URL`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_USERNAME`, `AZURE_PASSWORD` |
| `s3` | `S3BlobStorage` (boto3) | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION_NAME`, optional `S3_ENDPOINT_URL` (Cloudflare R2 / MinIO) |
| `local` | `LocalBlobStorage` (filesystem) | `STORAGE_LOCAL_DIR` - for running the flow without cloud credentials |

`STORAGE_CONTAINER_NAME` names the container / bucket / folder for all of them.

To add a provider: subclass `BlobStorage` (`problems/storage.py`), implement `ensure_container`,
`upload_bytes`, `delete_prefix`, `list_keys` and `read_bytes`, then register it in `get_storage()`.

## Workflow
1. **Login:** Staff users log in via `/users/login/` using their email and password.
2. **Fetch Problem:** Enter a Polygon Problem ID to fetch problem details and test cases.
3. **Review & Tag:** Review the fetched problem, select difficulty, and add tags.
4. **Migrate to Database:** Save the problem and metadata to the local database.
5. **Migrate Test Cases:** Optionally, migrate test cases to the database and/or cloud storage.
6. **View & Manage:** Use the admin interface for advanced management of problems, tags, and users.

## Setup Instructions
### 1. Prerequisites
- Python 3.10-3.13
- PostgreSQL database
- Redis
- Cloud storage credentials (Azure, an S3-compatible account, or none when using `STORAGE_PROVIDER=local`)
- Polygon API credentials

### 2. Clone the Repository
```bash
git clone <repo-url>
cd Polygon-migration-assignment
```

### 3. Create and Activate a Virtual Environment
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Unix/Mac:
source .venv/bin/activate
```

### 4. Install Dependencies
```bash
pip install -r requirement.txt
```

### 5. Start PostgreSQL and Redis
```bash
docker run --name polygon-postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=polygon_migration -p 5432:5432 -d postgres:15
docker run --name polygon-redis -p 6379:6379 -d redis:7
```

### 6. Configure Environment Variables
Copy `PolygonMigration/.env.example` to `PolygonMigration/.env`, beside `manage.py`, and fill in your values:
```env
SECRET_KEY=your-django-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# Database
DB_NAME=polygon_migration
DB_USER=postgres
DB_PASSWORD=postgres
DB_HOST=localhost
DB_PORT=5432

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_SSL=False

# Polygon API
POLYGON_API_KEY=your_polygon_api_key
POLYGON_API_SECRET=your_polygon_api_secret

# Cloud storage
STORAGE_PROVIDER=s3
STORAGE_CONTAINER_NAME=testcases
S3_ACCESS_KEY_ID=your-access-key-id
S3_SECRET_ACCESS_KEY=your-secret-access-key
S3_REGION_NAME=us-east-1
S3_ENDPOINT_URL=
```

### 7. Database Setup
- Ensure PostgreSQL is running and the database/user exist:
  ```sql
  CREATE DATABASE polygon_migration;
  CREATE USER your_db_user WITH PASSWORD 'your_db_password';
  GRANT ALL PRIVILEGES ON DATABASE polygon_migration TO your_db_user;
  ```
- Update your `.env` with the database name, user and password, then run migrations and create a
  superuser (the migration view is staff-only):
```bash
cd PolygonMigration
python manage.py migrate
python manage.py createsuperuser
```

### 8. Collect Static Files (for production)
```bash
python manage.py collectstatic
```

### 9. Run the Development Server
```bash
python manage.py runserver
```

Access the app at [http://localhost:8000/](http://localhost:8000/)

### 10. Run the Tests
```bash
python manage.py test
```
Tests run against a throwaway PostgreSQL database. Tests that need cloud storage or a live Polygon
account skip themselves automatically when those are unavailable.

## Usage
- **Login:** Go to `/users/login/` and log in as a staff user.
- **Main Interface:** Use the home page to fetch and migrate problems by Polygon ID.
- **Admin Panel:** Access `/admin/` for advanced management.
- **Migration:**
  - Fetch a problem by Polygon ID.
  - Select difficulty and add at least two tags.
  - Migrate to the database.
  - Migrate test cases to the database and/or cloud storage.

## Environment Variables Reference
| Variable                  | Description                                 |
|--------------------------|---------------------------------------------|
| `SECRET_KEY`              | Django secret key                           |
| `DEBUG`                   | Django debug mode (True/False)              |
| `ALLOWED_HOSTS`           | Comma-separated allowed hosts               |
| `DB_NAME`                 | PostgreSQL database name                    |
| `DB_USER`                 | PostgreSQL user                             |
| `DB_PASSWORD`             | PostgreSQL password                         |
| `DB_HOST`                 | PostgreSQL host                             |
| `DB_PORT`                 | PostgreSQL port                             |
| `POLYGON_API_KEY`         | Polygon API key                             |
| `POLYGON_API_SECRET`      | Polygon API secret                          |
| `REDIS_HOST`              | Redis host                                  |
| `REDIS_PORT`              | Redis port                                  |
| `REDIS_PASSWORD`          | Redis password (optional)                   |
| `REDIS_SSL`               | Redis SSL (optional, True/False)            |
| `STORAGE_PROVIDER`        | `azure`, `s3` or `local`                    |
| `STORAGE_CONTAINER_NAME`  | Container / bucket / folder for test cases   |
| `STORAGE_LOCAL_DIR`       | Root directory for `STORAGE_PROVIDER=local`  |
| `AZURE_STORAGE_ACCOUNT_URL` | Azure Blob Storage account URL            |
| `AZURE_TENANT_ID`         | Azure AD tenant ID                          |
| `AZURE_CLIENT_ID`         | Azure AD application client ID              |
| `AZURE_USERNAME`          | Azure username                              |
| `AZURE_PASSWORD`          | Azure password                              |
| `S3_ENDPOINT_URL`         | S3 endpoint (empty for AWS; set for R2/MinIO)|
| `S3_ACCESS_KEY_ID`        | S3 access key                               |
| `S3_SECRET_ACCESS_KEY`    | S3 secret key                               |
| `S3_REGION_NAME`          | S3 region                                   |
| `S3_ADDRESSING_STYLE`     | `path` for MinIO/R2, `auto` for AWS         |
| `CUSTOM_CHECKER_DIR`      | Where to compile custom checkers (optional)  |

## Project Structure
```
Polygon-migration-assignment/
├── requirement.txt         # Python dependencies
└── PolygonMigration/
    ├── .env.example        # Environment template
    ├── manage.py
    ├── problems/           # Polygon client, models, migration view, storage
    │   ├── polygon_api.py  # Polygon API client + Redis test-case cache
    │   ├── storage.py      # Cloud storage abstraction (azure / s3 / local)
    │   ├── views.py        # Single migration view handling all POST actions
    │   └── tests/          # Automated tests
    ├── users/              # Custom user model and authentication
    ├── contents/           # Topic and content management
    ├── static/             # Static files
    └── PolygonMigration/   # Project settings and URLs
```