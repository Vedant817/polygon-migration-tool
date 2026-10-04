"""S3 and Azure backends driven through botocore's Stubber / fake client.

These assert the exact request shapes each backend sends and that failures are
translated into :class:`BlobStorageError`, without needing a live endpoint.
"""

from unittest import mock

from django.test import SimpleTestCase

from problems.storage import AzureBlobStorage, BlobStorageError, S3BlobStorage, build_test_case_key


def make_s3():
    """Build the backend; the Stubber is attached to ``store.client``."""
    store = S3BlobStorage(container="testcases", access_key_id="k",
                          secret_access_key="s", region_name="us-east-1",
                          endpoint_url="http://localhost:9091",
                          addressing_style="path")
    return store


class S3BackendTests(SimpleTestCase):
    def setUp(self):
        from botocore.stub import Stubber
        self.store = make_s3()
        self.client = self.store.client
        self.stubber = Stubber(self.client)
        self.addCleanup(self.stubber.assert_no_pending_responses)

    def test_put_uses_the_required_object_key(self):
        self.stubber.add_response("put_object", {},
                                  {"Bucket": "testcases", "Key": "test_cases/4/3",
                                   "Body": b"1 2\n"})
        self.stubber.activate()
        self.store.upload_text("test_cases/4/3", "1 2\n")

    def test_upload_test_case_writes_input_then_answer(self):
        self.stubber.add_response("put_object", {},
                                  {"Bucket": "testcases", "Key": "test_cases/4/3",
                                   "Body": b"1 2\n"})
        self.stubber.add_response("put_object", {},
                                  {"Bucket": "testcases", "Key": "test_cases/4/3.a",
                                   "Body": b"3\n"})
        self.stubber.activate()
        self.store.upload_test_case(4, 3, "1 2\n", "3\n")

    def test_list_reads_every_page(self):
        page1 = {"Contents": [{"Key": "test_cases/4/1"}, {"Key": "test_cases/4/1.a"}],
                 "IsTruncated": True, "NextContinuationToken": "tok"}
        page2 = {"Contents": [{"Key": "test_cases/4/2"}], "IsTruncated": False}
        self.stubber.add_response("list_objects_v2", page1,
                                  {"Bucket": "testcases", "Prefix": "test_cases/4/"})
        self.stubber.add_response("list_objects_v2", page2,
                                  {"Bucket": "testcases", "Prefix": "test_cases/4/",
                                   "ContinuationToken": "tok"})
        self.stubber.activate()
        self.assertEqual(self.store.list_keys("test_cases/4/"),
                         ["test_cases/4/1", "test_cases/4/1.a", "test_cases/4/2"])

    def test_delete_prefix_removes_all_matching_objects(self):
        page1 = {"Contents": [{"Key": "test_cases/4/1"}], "IsTruncated": False}
        self.stubber.add_response("list_objects_v2", page1,
                                  {"Bucket": "testcases", "Prefix": "test_cases/4/"})
        self.stubber.add_response("delete_objects", {},
                                  {"Bucket": "testcases",
                                   "Delete": {"Objects": [{"Key": "test_cases/4/1"}]}})
        self.stubber.activate()
        self.assertEqual(self.store.delete_prefix("test_cases/4/"), 1)

    def test_read_bytes_returns_exact_payload(self):
        import io
        self.stubber.add_response(
            "get_object",
            {"Body": io.BytesIO(b"1 2\n")},
            {"Bucket": "testcases", "Key": "test_cases/4/1"})
        self.stubber.activate()
        self.assertEqual(self.store.read_bytes("test_cases/4/1"), b"1 2\n")

    def test_client_error_on_upload_becomes_blobstorageerror(self):
        from botocore.exceptions import ClientError
        self.stubber.add_client_error("put_object", service_error_code="AccessDenied",
                                      service_message="denied", http_status_code=403,
                                      expected_params={"Bucket": "testcases",
                                                       "Key": "test_cases/4/1",
                                                       "Body": b"x"})
        self.stubber.activate()
        with self.assertRaises(BlobStorageError) as ctx:
            self.store.upload_text("test_cases/4/1", "x")
        self.assertIn("test_cases/4/1", str(ctx.exception))

    def test_missing_object_becomes_blobstorageerror(self):
        from botocore.exceptions import ClientError
        self.stubber.add_client_error("get_object", service_error_code="NoSuchKey",
                                      service_message="missing", http_status_code=404)
        self.stubber.activate()
        with self.assertRaises(BlobStorageError):
            self.store.read_bytes("test_cases/4/nope")

    def test_key_helper_and_backend_agree(self):
        self.assertEqual(build_test_case_key(12, 11), "test_cases/12/11")
        self.assertEqual(build_test_case_key(12, 11, is_answer=True), "test_cases/12/11.a")


class S3EnsureContainerTests(SimpleTestCase):
    """Bucket creation must match the provider's CreateBucket expectations."""

    def _client(self, region):
        import boto3
        from botocore.config import Config
        return boto3.client("s3", aws_access_key_id="k", aws_secret_access_key="s",
                            region_name=region,
                            config=Config(s3={"addressing_style": "path"}))

    def _store(self, region):
        store = S3BlobStorage.__new__(S3BlobStorage)
        store.container = "testcases"
        store.client = self._client(region)
        return store

    def _capture_create_bucket(self, store, expected_params=None):
        """Queue a 404 for head_bucket, then capture the create_bucket call."""
        from botocore.stub import Stubber
        stubber = Stubber(store.client)
        stubber.add_client_error("head_bucket", service_error_code="404",
                                 service_message="Not Found", http_status_code=404)
        stubber.add_response("create_bucket", {},
                             expected_params if expected_params is not None else {})
        stubber.activate()
        store.ensure_container()
        return stubber

    def test_r2_auto_region_sends_no_location_constraint(self):
        """Cloudflare R2 uses region 'auto' and rejects any LocationConstraint."""
        store = self._store("auto")
        stubber = self._capture_create_bucket(store, {"Bucket": "testcases"})
        stubber.assert_no_pending_responses()

    def test_us_east_1_sends_no_location_constraint(self):
        store = self._store("us-east-1")
        stubber = self._capture_create_bucket(store, {"Bucket": "testcases"})
        stubber.assert_no_pending_responses()

    def test_real_region_sends_its_location_constraint(self):
        store = self._store("eu-west-1")
        stubber = self._capture_create_bucket(
            store, {"Bucket": "testcases",
                    "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"}})
        stubber.assert_no_pending_responses()

    def test_existing_bucket_is_not_recreated(self):
        from botocore.stub import Stubber
        store = self._store("auto")
        stubber = Stubber(store.client)
        stubber.add_response("head_bucket", {}, {"Bucket": "testcases"})
        stubber.activate()
        store.ensure_container()          # must not consume any create_bucket response
        stubber.assert_no_pending_responses()

    def test_already_owned_bucket_is_not_an_error(self):
        from botocore.stub import Stubber
        store = self._store("auto")
        stubber = Stubber(store.client)
        stubber.add_client_error("head_bucket", service_error_code="404",
                                 service_message="Not Found", http_status_code=404)
        stubber.add_client_error("create_bucket",
                                 service_error_code="BucketAlreadyOwnedByYou",
                                 service_message="owned", http_status_code=409)
        stubber.activate()
        store.ensure_container()          # must not raise
        stubber.assert_no_pending_responses()


class AzureBackendTests(SimpleTestCase):
    """Azure SDK calls are mocked; the contract (keys, error wrapping) is asserted."""

    def _backend(self, client):
        backend = AzureBlobStorage.__new__(AzureBlobStorage)
        backend.container = "testcases"
        backend.account_url = "https://example.blob.core.windows.net"
        backend.blob_service_client = client
        return backend

    def test_upload_uses_the_required_object_key(self):
        blob_client = mock.Mock()
        service = mock.Mock()
        service.get_blob_client.return_value = blob_client
        backend = self._backend(service)
        backend.upload_test_case(4, 3, "1 2\n", "3\n")
        keys = [c.kwargs["blob"] for c in service.get_blob_client.call_args_list]
        self.assertEqual(keys, ["test_cases/4/3", "test_cases/4/3.a"])
        self.assertEqual(blob_client.upload_blob.call_count, 2)
        self.assertEqual(blob_client.upload_blob.call_args_list[0].args[0], b"1 2\n")
        self.assertTrue(all(c.kwargs["overwrite"] for c in blob_client.upload_blob.call_args_list))

    def test_azure_error_is_wrapped(self):
        from azure.core.exceptions import ResourceNotFoundError
        blob_client = mock.Mock()
        blob_client.upload_blob.side_effect = ResourceNotFoundError("nope")
        service = mock.Mock()
        service.get_blob_client.return_value = blob_client
        backend = self._backend(service)
        with self.assertRaises(BlobStorageError) as ctx:
            backend.upload_text("test_cases/4/1", "x")
        self.assertIn("test_cases/4/1", str(ctx.exception))

    def test_list_keys_reads_the_prefix(self):
        from types import SimpleNamespace
        container = mock.Mock()
        container.list_blobs.return_value = [
            SimpleNamespace(name="test_cases/4/2"),
            SimpleNamespace(name="test_cases/4/1"),
        ]
        service = mock.Mock()
        service.get_container_client.return_value = container
        backend = self._backend(service)
        self.assertEqual(backend.list_keys("test_cases/4/"),
                         ["test_cases/4/1", "test_cases/4/2"])
        container.list_blobs.assert_called_once_with(name_starts_with="test_cases/4/")

    def test_same_key_layout_as_s3_backend(self):
        keys = [build_test_case_key(7, n) for n in (1, 10, 11)]
        self.assertEqual(keys, ["test_cases/7/1", "test_cases/7/10", "test_cases/7/11"])