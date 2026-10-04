"""Cloud storage abstraction for migrated Polygon test cases.

The migration flow (``problems.views.index`` -> ``PolygonAPI.migrate_to_storage``)
depends only on the operations defined by :class:`BlobStorage`:

* construct an object key      -> :func:`build_test_case_key`
* upload an object             -> :meth:`BlobStorage.upload_bytes`
* replace a problem's objects  -> :meth:`BlobStorage.delete_prefix`
* verify what was written      -> :meth:`BlobStorage.list_keys` / :meth:`BlobStorage.read_bytes`

Provider SDK calls live only inside the subclasses, so switching provider is a
settings change (``STORAGE_PROVIDER``) and not a migration-flow rewrite.

Object layout is fixed by the assessment and must not change:

    test_cases/{problem_id}/{test_number}      input
    test_cases/{problem_id}/{test_number}.a    answer

Configuration comes exclusively from environment variables (see
``PolygonMigration/.env.example``):

``STORAGE_PROVIDER``          ``azure``, ``s3`` or ``local``
``STORAGE_CONTAINER_NAME``    container (Azure) / bucket (S3) name
``AZURE_*`` / ``S3_*``        provider credentials and endpoint
"""

import logging
import os

logger = logging.getLogger(__name__)

#: All migrated test objects live under this prefix.
TEST_CASE_PREFIX = "test_cases"


class BlobStorageError(Exception):
    """A storage operation failed.

    Raised (never swallowed) so a partial or failed upload is visible to the
    caller and can be reported to the user.
    """


def build_test_case_key(problem_id, test_number, is_answer=False):
    """Return the object key for one test case.

    Args:
        problem_id: the problem's storage identifier (the database ``Problem.id``).
        test_number: 1-based test number.
        is_answer: ``True`` for the ``.a`` answer object, ``False`` for input.

    Returns:
        ``test_cases/{problem_id}/{test_number}`` or ``.../{test_number}.a``.
    """
    key = f"{TEST_CASE_PREFIX}/{problem_id}/{test_number}"
    return f"{key}.a" if is_answer else key


def build_problem_prefix(problem_id):
    """Return the key prefix covering every object for one problem."""
    return f"{TEST_CASE_PREFIX}/{problem_id}/"


class BlobStorage:
    """Interface the migration layer depends on.

    Subclasses must raise :class:`BlobStorageError` on failure and must not
    swallow exceptions, so that partial uploads surface to the user.
    """

    #: Human readable provider name, used in log/error messages.
    provider = "abstract"

    def __init__(self, container):
        self.container = container

    # -- lifecycle ---------------------------------------------------------
    def ensure_container(self):
        """Create the container/bucket if it does not exist. Idempotent."""

    # -- write operations --------------------------------------------------
    def upload_bytes(self, key, data):
        """Write ``data`` (bytes) to ``key``, overwriting any existing object.

        Raises:
            BlobStorageError: if the object was not stored.
        """
        raise NotImplementedError

    def upload_text(self, key, text):
        """Write ``text`` as UTF-8 to ``key``. See :meth:`upload_bytes`."""
        return self.upload_bytes(key, text.encode("utf-8"))

    def delete_prefix(self, prefix):
        """Delete every object whose key starts with ``prefix``.

        Returns:
            The number of objects deleted.

        Raises:
            BlobStorageError: if the prefix could not be fully cleared.
        """
        raise NotImplementedError

    # -- read/verify operations -------------------------------------------
    def list_keys(self, prefix=""):
        """Return the sorted keys of every object starting with ``prefix``."""
        raise NotImplementedError

    def read_bytes(self, key):
        """Return the exact stored bytes for ``key``.

        Raises:
            BlobStorageError: if the object is missing or unreadable.
        """
        raise NotImplementedError

    def read_text(self, key):
        """Return the stored object decoded as UTF-8."""
        return self.read_bytes(key).decode("utf-8")

    # -- convenience -------------------------------------------------------
    def upload_test_case(self, problem_id, test_number, input_data, output_data):
        """Upload one test case's input and its ``.a`` answer.

        Both objects are written. If either fails, the error propagates so a
        partial upload is never reported as success.
        """
        self.upload_text(build_test_case_key(problem_id, test_number), input_data)
        self.upload_text(build_test_case_key(problem_id, test_number, is_answer=True), output_data)


class AzureBlobStorage(BlobStorage):
    """Azure Blob Storage backend (the provider the starter code shipped with)."""

    provider = "azure"

    def __init__(self, container, account_url, tenant_id, client_id, username, password):
        super().__init__(container)
        self.account_url = account_url
        try:
            from azure.identity import UsernamePasswordCredential
            from azure.storage.blob import BlobServiceClient

            credential = UsernamePasswordCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                username=username,
                password=password,
            )
            self.blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
        except Exception as exc:
            raise BlobStorageError(f"Azure authentication failed for account {account_url}: {exc}") from exc

    def ensure_container(self):
        from azure.core.exceptions import ResourceExistsError

        try:
            self.blob_service_client.create_container(self.container)
        except ResourceExistsError:
            pass
        except Exception as exc:
            raise BlobStorageError(f"Azure container {self.container!r} unavailable: {exc}") from exc

    def upload_bytes(self, key, data):
        try:
            client = self.blob_service_client.get_blob_client(container=self.container, blob=key)
            client.upload_blob(data, overwrite=True)
        except Exception as exc:
            raise BlobStorageError(f"Azure upload failed for {key!r}: {exc}") from exc

    def delete_prefix(self, prefix):
        try:
            container_client = self.blob_service_client.get_container_client(self.container)
            names = [b.name for b in container_client.list_blobs(name_starts_with=prefix)]
            for name in names:
                container_client.delete_blob(name)
        except Exception as exc:
            raise BlobStorageError(f"Azure delete failed for prefix {prefix!r}: {exc}") from exc
        return len(names)

    def list_keys(self, prefix=""):
        try:
            container_client = self.blob_service_client.get_container_client(self.container)
            return sorted(b.name for b in container_client.list_blobs(name_starts_with=prefix))
        except Exception as exc:
            raise BlobStorageError(f"Azure list failed for prefix {prefix!r}: {exc}") from exc

    def read_bytes(self, key):
        try:
            client = self.blob_service_client.get_blob_client(container=self.container, blob=key)
            return client.download_blob().readall()
        except Exception as exc:
            raise BlobStorageError(f"Azure read failed for {key!r}: {exc}") from exc


class S3BlobStorage(BlobStorage):
    """S3-compatible backend.

    Works unchanged against Amazon S3, Cloudflare R2 and MinIO: only
    ``S3_ENDPOINT_URL`` differs (leave it empty for Amazon S3).
    """

    provider = "s3"

    def __init__(self, container, access_key_id, secret_access_key, region_name,
                 endpoint_url=None, addressing_style="path"):
        super().__init__(container)
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise BlobStorageError(
                "boto3 is required for STORAGE_PROVIDER=s3; pip install -r requirement.txt"
            ) from exc
        try:
            self.client = boto3.client(
                "s3",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region_name or None,
                endpoint_url=endpoint_url or None,
                config=Config(s3={"addressing_style": addressing_style or "path"}),
            )
        except Exception as exc:
            raise BlobStorageError(f"S3 client construction failed: {exc}") from exc

    def ensure_container(self):
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.container)
            return
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status == 404:
                pass                       # bucket absent -> create it
            elif status in (301, 400):
                # Bucket exists in another region / is owned elsewhere.
                return
            elif status in (401, 403) and code not in ("InvalidAccessKeyId",
                                                  "SignatureDoesNotMatch",
                                                  "ExpiredToken",
                                                  "InvalidToken"):
                # A key restricted to one bucket may not be allowed to list bucket
                # names (Backblaze calls this listAllBucketNames), so HeadBucket
                # answers 403 even though the bucket is perfectly usable. Carry on:
                # the first upload is the real test and fails loudly if the bucket
                # name is wrong. Credential errors are NOT swallowed - they raise.
                logger.info("HeadBucket on %r returned %s (%s); continuing without "
                            "creating it", self.container, status, code or "no code")
                return
            else:
                raise BlobStorageError(f"S3 bucket {self.container!r} unavailable: {exc}") from exc
        except Exception as exc:
            raise BlobStorageError(f"S3 bucket {self.container!r} unavailable: {exc}") from exc

        kwargs = {"Bucket": self.container}
        region = getattr(self.client, "meta", None) and self.client.meta.region_name
        if region and region not in ("us-east-1", "auto"):
            # us-east-1 must not carry a LocationConstraint, other real regions must.
            # "auto" is the convention Cloudflare R2 requires, and R2 rejects any
            # LocationConstraint outright, so nothing is sent for it.
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            self.client.create_bucket(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                return
            if "CreateBucketConfiguration" in kwargs:
                # Backblaze B2 and some other S3-compatible endpoints derive the
                # region from the endpoint URL and reject the constraint. Retry
                # once without it rather than failing the whole migration.
                logger.info("Retrying create_bucket without a LocationConstraint for %r",
                            self.container)
                try:
                    self.client.create_bucket(Bucket=self.container)
                    return
                except ClientError as retry_exc:
                    retry_code = retry_exc.response.get("Error", {}).get("Code")
                    if retry_code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        return
                    raise BlobStorageError(
                        f"S3 bucket {self.container!r} could not be created: {retry_exc}"
                    ) from retry_exc
            raise BlobStorageError(
                f"S3 bucket {self.container!r} could not be created: {exc}") from exc

    def upload_bytes(self, key, data):
        try:
            self.client.put_object(Bucket=self.container, Key=key, Body=data)
        except Exception as exc:
            raise BlobStorageError(f"S3 upload failed for {key!r}: {exc}") from exc

    def delete_prefix(self, prefix):
        keys = self.list_keys(prefix)
        for start in range(0, len(keys), 1000):
            batch = keys[start:start + 1000]
            try:
                self.client.delete_objects(
                    Bucket=self.container,
                    Delete={"Objects": [{"Key": k} for k in batch]},
                )
            except Exception as exc:
                raise BlobStorageError(f"S3 delete failed for prefix {prefix!r}: {exc}") from exc
        return len(keys)

    def list_keys(self, prefix=""):
        keys, token = [], None
        try:
            while True:
                kwargs = {"Bucket": self.container, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                page = self.client.list_objects_v2(**kwargs)
                keys.extend(o["Key"] for o in page.get("Contents", []))
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        except Exception as exc:
            raise BlobStorageError(f"S3 list failed for prefix {prefix!r}: {exc}") from exc
        return sorted(keys)

    def read_bytes(self, key):
        try:
            return self.client.get_object(Bucket=self.container, Key=key)["Body"].read()
        except Exception as exc:
            raise BlobStorageError(f"S3 read failed for {key!r}: {exc}") from exc


class LocalBlobStorage(BlobStorage):
    """Filesystem-backed backend under ``STORAGE_LOCAL_DIR``.

    Not a cloud provider. It exists so the whole migration flow - including the
    exact object layout - can be exercised and verified on a machine that has no
    cloud credentials. It speaks the same interface as the cloud backends.
    """

    provider = "local"

    def __init__(self, container, root_dir):
        super().__init__(container)
        self.root = os.path.abspath(os.path.join(root_dir, container))

    def ensure_container(self):
        try:
            os.makedirs(self.root, exist_ok=True)
        except OSError as exc:
            raise BlobStorageError(f"Local storage root unavailable: {exc}") from exc

    def _path(self, key):
        # Keys are generated by build_test_case_key; refuse anything that escapes root.
        full = os.path.abspath(os.path.join(self.root, key.replace("/", os.sep)))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise BlobStorageError(f"Refusing object key outside the storage root: {key!r}")
        return full

    def upload_bytes(self, key, data):
        path = self._path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            raise BlobStorageError(f"Local upload failed for {key!r}: {exc}") from exc

    def delete_prefix(self, prefix):
        if not prefix or prefix.strip("/") == "":
            raise BlobStorageError("Refusing to delete the whole storage root; pass a problem prefix.")
        base = self._path(prefix).rstrip(os.sep) or self.root
        if not os.path.isdir(base):
            return 0
        removed = 0
        try:
            for dirpath, _dirs, files in os.walk(base, topdown=False):
                for name in files:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                for name in os.listdir(dirpath):
                    target = os.path.join(dirpath, name)
                    if os.path.isdir(target):
                        os.rmdir(target)
            os.rmdir(base)
        except OSError as exc:
            raise BlobStorageError(f"Local delete failed for prefix {prefix!r}: {exc}") from exc
        return removed

    def list_keys(self, prefix=""):
        if not os.path.isdir(self.root):
            return []
        keys = []
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                key = rel.replace(os.sep, "/")
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def read_bytes(self, key):
        path = self._path(key)
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise BlobStorageError(f"Local read failed for {key!r}: {exc}") from exc


def get_storage():
    """Build the configured :class:`BlobStorage` from Django settings.

    Raises:
        BlobStorageError: if ``STORAGE_PROVIDER`` is unknown or the required
            environment variables are missing.
    """
    from django.conf import settings

    provider = (settings.STORAGE_PROVIDER or "").strip().lower()
    container = settings.STORAGE_CONTAINER_NAME

    if not container:
        raise BlobStorageError("STORAGE_CONTAINER_NAME is not set; cannot choose a container/bucket.")

    if provider == "local":
        if not settings.STORAGE_LOCAL_DIR:
            raise BlobStorageError("Missing local configuration: STORAGE_LOCAL_DIR")
        return LocalBlobStorage(container=container, root_dir=settings.STORAGE_LOCAL_DIR)

    if provider == "azure":
        required = {
            "account_url": settings.AZURE_STORAGE_ACCOUNT_URL,
            "tenant_id": settings.AZURE_TENANT_ID,
            "client_id": settings.AZURE_CLIENT_ID,
            "username": settings.AZURE_USERNAME,
            "password": settings.AZURE_PASSWORD,
        }
        names = {
            "account_url": "AZURE_STORAGE_ACCOUNT_URL",
            "tenant_id": "AZURE_TENANT_ID",
            "client_id": "AZURE_CLIENT_ID",
            "username": "AZURE_USERNAME",
            "password": "AZURE_PASSWORD",
        }
        missing = sorted(names[key] for key, value in required.items() if not value)
        if missing:
            raise BlobStorageError(f"Missing Azure configuration: {', '.join(missing)}")
        return AzureBlobStorage(container=container, **required)

    if provider == "s3":
        required = {
            "access_key_id": settings.S3_ACCESS_KEY_ID,
            "secret_access_key": settings.S3_SECRET_ACCESS_KEY,
        }
        names = {
            "access_key_id": "S3_ACCESS_KEY_ID",
            "secret_access_key": "S3_SECRET_ACCESS_KEY",
        }
        missing = sorted(names[key] for key, value in required.items() if not value)
        if missing:
            raise BlobStorageError(f"Missing S3 configuration: {', '.join(missing)}")
        return S3BlobStorage(
            container=container,
            region_name=settings.S3_REGION_NAME,
            endpoint_url=settings.S3_ENDPOINT_URL,
            addressing_style=settings.S3_ADDRESSING_STYLE,
            **required,
        )

    raise BlobStorageError(
        f"Unknown STORAGE_PROVIDER {provider!r}; expected 'azure', 's3' or 'local'."
    )