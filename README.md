# PolygonMigration

## Overview
PolygonMigration is a Django-based web application designed to facilitate the migration of programming problems and their test cases from the [Polygon](https://polygon.codeforces.com/) platform to a local database and Azure Blob Storage. It provides a user-friendly interface for staff users to fetch, review, tag, and migrate problems, as well as manage test cases and metadata.

## Features
- **Polygon Integration:** Fetch problems and test cases directly from Polygon using API keys.
- **Database Migration:** Store problem statements, metadata, and test cases in a PostgreSQL database.
- **Azure Blob Storage:** Upload test cases to Azure Blob Storage for scalable storage and retrieval.
- **Tagging & Metadata:** Add, search, and manage tags and difficulty levels for each problem.
- **Admin Interface:** Manage users, problems, tags, and test cases via Django admin.
- **Custom User Model:** Email-based authentication with extended user profile fields.
- **Staff-Only Access:** Only staff users can access migration features.

## Workflow
1. **Login:** Staff users log in via `/users/login/` using their email and password.
2. **Fetch Problem:** Enter a Polygon Problem ID to fetch problem details and test cases.
3. **Review & Tag:** Review the fetched problem, select difficulty, and add tags.
4. **Migrate to Database:** Save the problem and metadata to the local database.
5. **Migrate Test Cases:** Optionally, migrate test cases to the database and/or Azure Blob Storage.
6. **View & Manage:** Use the admin interface for advanced management of problems, tags, and users.

## Setup Instructions
### 1. Prerequisites
- Python 3.8+
- PostgreSQL database
- [Azure account](https://portal.azure.com/) with Blob Storage and an AAD application
- Polygon API credentials

### 2. Clone the Repository
```bash
git clone <repo-url>
cd Polygon-migration/PolygonMigration
```

### 3. Create and Activate a Virtual Environment
```bash
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Unix/Mac:
source venv/bin/activate
```

### 4. Install Dependencies
```bash
pip install -r ../requirement.txt
```

### 5. Configure Environment Variables
Copy `PolygonMigration/.env.example` to `PolygonMigration/.env`, beside `manage.py`, and configure the following variables:
```env
SECRET_KEY=your-django-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# Database
DB_NAME=your_db_name
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_HOST=localhost
DB_PORT=5432

# Polygon API
POLYGON_API_KEY=your_polygon_api_key
POLYGON_API_SECRET=your_polygon_api_secret

# Azure Blob Storage
AZURE_STORAGE_ACCOUNT_URL=https://<your-storage-account>.blob.core.windows.net/
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_USERNAME=your-azure-username
AZURE_PASSWORD=your-azure-password
AZURE_CONTAINER_NAME=your-container-name

# Redis (optional, for caching)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_SSL=False
```

### 6. Database Setup
- Ensure PostgreSQL is running.
- **If you have not set up the database and user yet, follow these steps:**

  1. Open a terminal and access the PostgreSQL prompt (you may need to enter your system password):
     ```bash
     psql -U postgres
     ```
  2. Create a new database (replace `your_db_name` with your desired database name):
     ```sql
     CREATE DATABASE your_db_name;
     ```
  3. Create a new user (replace `your_db_user` and `your_db_password` with your desired username and password):
     ```sql
     CREATE USER your_db_user WITH PASSWORD 'your_db_password';
     ```
  4. Grant all privileges on the new database to the new user:
     ```sql
     GRANT ALL PRIVILEGES ON DATABASE your_db_name TO your_db_user;
     ```
  5. Exit the PostgreSQL prompt:
     ```sql
     \q
     ```

- Update your `.env` file with the database name, user, and password you just created.
- Run migrations:
```bash
python manage.py migrate
```

### 7. Create a Superuser
```bash
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

## Usage
- **Login:** Go to `/users/login/` and log in as a staff user.
- **Main Interface:** Use the home page to fetch and migrate problems by Polygon ID.
- **Admin Panel:** Access `/admin/` for advanced management.
- **Migration:**
  - Fetch a problem by Polygon ID.
  - Select difficulty and tags.
  - Migrate to the database.
  - Migrate test cases to the database and/or Azure.

## Environment Variables Reference
| Variable                  | Description                                 |
|--------------------------|---------------------------------------------|
| SECRET_KEY                | Django secret key                           |
| DEBUG                     | Django debug mode (True/False)              |
| ALLOWED_HOSTS             | Comma-separated allowed hosts               |
| DB_NAME                   | PostgreSQL database name                    |
| DB_USER                   | PostgreSQL user                             |
| DB_PASSWORD               | PostgreSQL password                         |
| DB_HOST                   | PostgreSQL host                             |
| DB_PORT                   | PostgreSQL port                             |
| POLYGON_API_KEY           | Polygon API key                             |
| POLYGON_API_SECRET        | Polygon API secret                          |
| AZURE_STORAGE_ACCOUNT_URL | Azure Blob Storage account URL              |
| AZURE_TENANT_ID           | Azure AD tenant ID                          |
| AZURE_CLIENT_ID           | Azure AD application client ID              |
| AZURE_USERNAME            | Azure username                              |
| AZURE_PASSWORD            | Azure password                              |
| AZURE_CONTAINER_NAME      | Azure Blob container name                   |
| REDIS_HOST                | Redis host (optional)                       |
| REDIS_PORT                | Redis port (optional)                       |
| REDIS_PASSWORD            | Redis password (optional)                   |
| REDIS_SSL                 | Redis SSL (optional, True/False)            |

## Project Structure
```
PolygonMigration/
├── contents/         # Topic and content management
├── problems/         # Polygon migration logic, models, views
├── users/            # Custom user model and authentication
├── static/           # Static files (CSS, JS, etc.)
├── staticfiles/      # Collected static files (for production)
├── logs/             # Log files
├── manage.py         # Django management script
├── PolygonMigration/ # Project settings and URLs
```