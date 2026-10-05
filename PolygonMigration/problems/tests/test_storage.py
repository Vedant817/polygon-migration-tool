"""Storage abstraction: key layout, provider boundary, failure propagation.

Most tests here are unit level against an in-memory backend or the local
filesystem. ``LocalStorageIntegrationTests`` performs a real filesystem
round-trip; ``StorageIntegrationTests`` performs a real round-trip against
whatever backend ``STORAGE_PROVIDER`` selects and skips itself when that
backend is unavailable. S3-specific request shapes are asserted with
``botocore.stub.Stubber`` in ``test_storage_backends.py``.
"""

import os
import shutil
import unittest
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from problems import storage as storage_mod
from problems.storage import (
    BlobStorage,
    BlobStorageError,
    build_problem_prefix,
    build_test_case_key,
    get_storage,
)


class MemoryStorage(BlobStorage):
    """In-memory backend used to test the interface contract itself."""

    provider = "memory"

    def __init__(self, container="testcases", fail_on=None):
        super().__init__(container)
        self.objects = {}
        self.fail_on = set(fail_on or ())
        self.calls = []

    def _guard(self, key, op):
        self.calls.append((op, key))
        if key in self.fail_on or "*" in self.fail_on:
            raise BlobStorageError(f"simulated {op} failure for {key!r}")

    def upload_bytes(self, key, data):
        self._guard(key, "put")
        self.objects[key] = bytes(data)

    def delete_prefix(self, prefix):
        removed = [k for k in self.objects if k.startswith(prefix)]
        for key in removed:
            self._guard(key, "delete")
            del self.objects[key]
        return len(removed)

    def list_keys(self, prefix=""):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def read_bytes(self, key):
        self._guard(key, "get")
        if key not in self.objects:
            raise BlobStorageError(f"missing object {key!r}")
        return self.objects[key]


class KeyLayoutTests(SimpleTestCase):
    """The assessment fixes this layout; a regression here breaks consumers."""

    def test_input_key_has_no_extension(self):
        self.assertEqual(build_test_case_key(7, 1), "test_cases/7/1")

    def test_answer_key_has_dot_a_suffix(self):
        self.assertEqual(build_test_case_key(7, 1, is_answer=True), "test_cases/7/1.a")

    def test_numbers_are_not_zero_padded(self):
        for number in (1, 9, 10, 11, 15, 100):
            self.assertEqual(build_test_case_key(3, number), f"test_cases/3/{number}")
            self.assertEqual(build_test_case_key(3, number, is_answer=True),
                             f"test_cases/3/{number}.a")

    def test_problem_id_is_used_not_the_polygon_id(self):
        self.assertTrue(build_test_case_key(42, 2).startswith("test_cases/42/"))

    def test_problem_prefix_matches_generated_keys(self):
        prefix = build_problem_prefix(9)
        self.assertEqual(prefix, "test_cases/9/")
        for number in (1, 2, 15):
            self.assertTrue(build_test_case_key(9, number).startswith(prefix))
            self.assertTrue(build_test_case_key(9, number, is_answer=True).startswith(prefix))


class UploadTestCaseTests(SimpleTestCase):
    def test_uploads_both_objects_under_the_required_keys(self):
        store = MemoryStorage()
        store.upload_test_case(4, 3, "1 2\n", "3\n")
        self.assertEqual(store.objects, {
            "test_cases/4/3": b"1 2\n",
            "test_cases/4/3.a": b"3\n",
        })

    def test_input_failure_propagates_rather_than_being_swallowed(self):
        store = MemoryStorage(fail_on={"test_cases/4/3"})
        with self.assertRaises(BlobStorageError):
            store.upload_test_case(4, 3, "1 2\n", "3\n")

    def test_answer_failure_propagates(self):
        store = MemoryStorage(fail_on={"test_cases/4/3.a"})
        with self.assertRaises(BlobStorageError):
            store.upload_test_case(4, 3, "1 2\n", "3\n")

    def test_readback_matches_exact_bytes_including_trailing_newline(self):
        store = MemoryStorage()
        store.upload_test_case(4, 1, "3 4\n", "7\n")
        self.assertEqual(store.read_text(build_test_case_key(4, 1)), "3 4\n")
        self.assertEqual(store.read_text(build_test_case_key(4, 1, is_answer=True)), "7\n")

    def test_unicode_and_empty_payloads_survive(self):
        store = MemoryStorage()
        store.upload_test_case(5, 1, "é你好\n", "")
        self.assertEqual(store.read_bytes("test_cases/5/1"), "é你好\n".encode("utf-8"))
        self.assertEqual(store.read_bytes("test_cases/5/1.a"), b"")

    def test_delete_prefix_only_clears_the_target_problem(self):
        store = MemoryStorage()
        store.upload_test_case(4, 1, "a", "b")
        store.upload_test_case(5, 1, "c", "d")
        self.assertEqual(store.delete_prefix(build_problem_prefix(4)), 2)
        self.assertEqual(store.list_keys(), ["test_cases/5/1", "test_cases/5/1.a"])

    def test_upload_overwrites_existing_object(self):
        store = MemoryStorage()
        store.upload_text("test_cases/4/1", "old")
        store.upload_text("test_cases/4/1", "new")
        self.assertEqual(store.read_text("test_cases/4/1"), "new")


class ProviderFactoryTests(SimpleTestCase):
    def test_unknown_provider_is_rejected_with_a_clear_message(self):
        with override_settings(STORAGE_PROVIDER="dropbox", STORAGE_CONTAINER_NAME="c"):
            with self.assertRaises(BlobStorageError) as ctx:
                get_storage()
        self.assertIn("Unknown STORAGE_PROVIDER", str(ctx.exception))

    def test_missing_container_is_rejected(self):
        with override_settings(STORAGE_PROVIDER="s3", STORAGE_CONTAINER_NAME=""):
            with self.assertRaises(BlobStorageError) as ctx:
                get_storage()
        self.assertIn("STORAGE_CONTAINER_NAME", str(ctx.exception))

    def test_missing_s3_credentials_are_reported_by_name(self):
        with override_settings(STORAGE_PROVIDER="s3", STORAGE_CONTAINER_NAME="c",
                               S3_ACCESS_KEY_ID="", S3_SECRET_ACCESS_KEY=""):
            with self.assertRaises(BlobStorageError) as ctx:
                get_storage()
        self.assertIn("S3_ACCESS_KEY_ID", str(ctx.exception))

    def test_missing_azure_credentials_are_reported_by_name(self):
        with override_settings(STORAGE_PROVIDER="azure", STORAGE_CONTAINER_NAME="c",
                               AZURE_STORAGE_ACCOUNT_URL="", AZURE_TENANT_ID="",
                               AZURE_CLIENT_ID="", AZURE_USERNAME="",
                               AZURE_PASSWORD=""):
            with self.assertRaises(BlobStorageError) as ctx:
                get_storage()
        self.assertIn("AZURE_TENANT_ID", str(ctx.exception))

    def test_factory_returns_the_requested_provider_class(self):
        with override_settings(
            STORAGE_PROVIDER="s3", STORAGE_CONTAINER_NAME="c",
            S3_ACCESS_KEY_ID="k", S3_SECRET_ACCESS_KEY="s",
            S3_REGION_NAME="us-east-1", S3_ENDPOINT_URL="http://localhost:9091",
            S3_ADDRESSING_STYLE="path",
        ):
            store = get_storage()
        self.assertIsInstance(store, storage_mod.S3BlobStorage)
        self.assertEqual(store.provider, "s3")
        self.assertEqual(store.container, "c")

    def test_missing_local_storage_directory_is_reported(self):
        with override_settings(STORAGE_PROVIDER="local", STORAGE_CONTAINER_NAME="c",
                               STORAGE_LOCAL_DIR=""):
            with self.assertRaises(BlobStorageError) as ctx:
                get_storage()
        self.assertIn("STORAGE_LOCAL_DIR", str(ctx.exception))

    def test_migration_flow_has_no_provider_sdk_dependency(self):
        """The migration flow itself must not reference a provider SDK.

        Asserted on the already-imported modules, so a module-level
        ``import azure`` / ``import boto3`` in views.py or polygon_api.py would
        be visible as a module attribute.
        """
        import sys

        import problems.polygon_api as polygon_api
        import problems.views as views

        for module in (views, polygon_api):
            for sdk in ("azure", "boto3", "botocore"):
                self.assertFalse(hasattr(module, sdk),
                                 f"{module.__name__} must not import {sdk} at module level")
        # and the Azure SDK is genuinely absent from sys.modules on the S3 path
        with mock.patch.dict(sys.modules, {"azure.storage.blob": None, "azure.identity": None}):
            with override_settings(
                STORAGE_PROVIDER="s3", STORAGE_CONTAINER_NAME="c",
                S3_ACCESS_KEY_ID="k", S3_SECRET_ACCESS_KEY="s",
                S3_REGION_NAME="us-east-1", S3_ENDPOINT_URL="http://localhost:9091",
                S3_ADDRESSING_STYLE="path",
            ):
                self.assertEqual(get_storage().provider, "s3")


def _backend_available():
    try:
        store = get_storage()
        store.ensure_container()
        return True
    except BlobStorageError:
        return False


class LocalStorageIntegrationTests(SimpleTestCase):
    """REAL filesystem backend: real writes, real independent read-back."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="polygon-storage-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = storage_mod.LocalBlobStorage(container="testcases", root_dir=self.tmp)
        self.store.ensure_container()
        self.problem_id = 770001

    def test_round_trip_uses_the_required_layout(self):
        self.store.upload_test_case(self.problem_id, 1, "1 2\n", "3\n")
        self.store.upload_test_case(self.problem_id, 12, "10 20\n", "30\n")
        self.assertEqual(self.store.list_keys(build_problem_prefix(self.problem_id)),
                         [f"test_cases/{self.problem_id}/1",
                          f"test_cases/{self.problem_id}/1.a",
                          f"test_cases/{self.problem_id}/12",
                          f"test_cases/{self.problem_id}/12.a"])

    def test_readback_matches_exact_bytes_including_newlines(self):
        self.store.upload_test_case(self.problem_id, 1, "1 2\n", "3\n")
        fresh = storage_mod.LocalBlobStorage(container="testcases", root_dir=self.tmp)
        self.assertEqual(fresh.read_text(f"test_cases/{self.problem_id}/1"), "1 2\n")
        self.assertEqual(fresh.read_text(f"test_cases/{self.problem_id}/1.a"), "3\n")

    def test_delete_prefix_clears_only_the_target_problem(self):
        self.store.upload_test_case(self.problem_id, 1, "a\n", "b\n")
        self.store.upload_test_case(self.problem_id + 1, 1, "c\n", "d\n")
        self.assertEqual(self.store.delete_prefix(build_problem_prefix(self.problem_id)), 2)
        self.assertEqual(self.store.list_keys(),
                         [f"test_cases/{self.problem_id + 1}/1",
                          f"test_cases/{self.problem_id + 1}/1.a"])

    def test_missing_object_raises_blobstorageerror(self):
        with self.assertRaises(BlobStorageError):
            self.store.read_bytes("test_cases/770001/absent")

    def test_key_traversal_outside_the_root_is_refused(self):
        # Backslash is only a separator on Windows, so only assert it there.
        keys = ["../../escape", "a/../../../escape", "a/../../b", "/abs/path", "C:/Windows/x"]
        if os.sep == "\\":
            keys += ["..\\..\\escape", "a\\..\\..\\b"]
        for key in keys:
            with self.subTest(key=key):
                with self.assertRaises(BlobStorageError):
                    self.store.upload_text(key, "x")

    def test_sentinel_outside_the_root_survives_a_traversal_attempt(self):
        outside = os.path.join(os.path.dirname(self.tmp), "sentinel.txt")
        with open(outside, "wb") as handle:
            handle.write(b"do not delete")
        self.addCleanup(lambda: os.path.exists(outside) and os.remove(outside))
        with self.assertRaises(BlobStorageError):
            self.store.upload_text("../sentinel.txt", "overwritten")
        self.assertTrue(os.path.exists(outside))
        with open(outside, "rb") as handle:
            self.assertEqual(handle.read(), b"do not delete")

    def test_deleting_the_whole_storage_root_is_refused(self):
        with self.assertRaises(BlobStorageError):
            self.store.delete_prefix("")
        self.assertTrue(os.path.isdir(self.store.root))

    def test_keys_inside_the_root_are_accepted(self):
        self.store.upload_text("test_cases/1/1", "ok")
        self.assertEqual(self.store.read_text("test_cases/1/1"), "ok")

    def test_unicode_survives(self):
        self.store.upload_test_case(self.problem_id, 1, "é你好\n", "✓\n")
        self.assertEqual(self.store.read_text(f"test_cases/{self.problem_id}/1"), "é你好\n")


class StorageIntegrationTests(SimpleTestCase):
    """REAL round-trip against whatever backend the environment configures.

    Skipped when that backend is not usable, so ``manage.py test`` stays green
    on a machine without storage credentials or an S3 endpoint.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _backend_available():
            raise unittest.SkipTest(
                f"configured storage backend ({settings.STORAGE_PROVIDER}) is unavailable")

    def setUp(self):
        self.store = get_storage()
        self.problem_id = 990001

    def tearDown(self):
        try:
            self.store.delete_prefix(build_problem_prefix(self.problem_id))
        except BlobStorageError:
            pass

    def test_real_upload_then_independent_list_and_read(self):
        self.store.upload_test_case(self.problem_id, 1, "1 2\n", "3\n")
        self.store.upload_test_case(self.problem_id, 2, "10 20\n", "30\n")

        keys = self.store.list_keys(build_problem_prefix(self.problem_id))
        self.assertEqual(keys, [
            "test_cases/990001/1",
            "test_cases/990001/1.a",
            "test_cases/990001/2",
            "test_cases/990001/2.a",
        ])

        # Read back through a brand-new client so nothing is served from memory.
        fresh = get_storage()
        self.assertEqual(fresh.read_text("test_cases/990001/1"), "1 2\n")
        self.assertEqual(fresh.read_text("test_cases/990001/1.a"), "3\n")
        self.assertEqual(fresh.read_text("test_cases/990001/2"), "10 20\n")
        self.assertEqual(fresh.read_text("test_cases/990001/2.a"), "30\n")

    def test_real_object_sizes_are_consistent(self):
        payload = "x" * 5000
        self.store.upload_test_case(self.problem_id, 7, payload, "y")
        self.assertEqual(len(self.store.read_bytes("test_cases/990001/7")), 5000)

    def test_real_migration_replaces_previous_objects(self):
        self.store.upload_test_case(self.problem_id, 1, "a", "b")
        self.store.upload_test_case(self.problem_id, 2, "c", "d")
        deleted = self.store.delete_prefix(build_problem_prefix(self.problem_id))
        self.assertEqual(deleted, 4)
        self.assertEqual(self.store.list_keys(build_problem_prefix(self.problem_id)), [])

    def test_real_missing_object_raises_blobstorageerror(self):
        with self.assertRaises(BlobStorageError):
            self.store.read_bytes("test_cases/990001/does-not-exist")