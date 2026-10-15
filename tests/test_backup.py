import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.backup import BackupConfig, POSTGRES_ENTRA_SCOPE, run_backup


def make_config(**overrides):
    values = {
        "host": "my-postgres-server.postgres.database.azure.com",
        "port": 5432,
        "database": "sampledb",
        "user": "id-backup-identity",
        "sslmode": "verify-full",
        "sslrootcert": "system",
        "blob_account_url": "https://mystorageaccount.blob.core.windows.net",
        "blob_container": "postgres-backups",
        "password": "test-password",
    }
    values.update(overrides)
    return BackupConfig(**values)


class BackupTests(unittest.TestCase):
    def test_config_requires_valid_database(self):
        base = {
            "PGHOST": "server",
            "PGPORT": "5432",
            "PGUSER": "id-backup-identity",
            "PGDATABASE": "sampledb",
            "BLOB_ACCOUNT_URL": "https://storage.example",
            "BLOB_CONTAINER": "backups",
        }
        config = BackupConfig.from_env(base)
        self.assertEqual(config.database, "sampledb")
        self.assertEqual(config.sslmode, "verify-full")
        self.assertIsNone(config.password)
        with self.assertRaises(ValueError):
            BackupConfig.from_env({**base, "PGDATABASE": "invalid db name with spaces!"})
        with self.assertRaises(ValueError):
            BackupConfig.from_env({**base, "PGPORT": "invalid"})

    def _run(self, *, subprocess_run, config=None, temp_dir_factory=tempfile.TemporaryDirectory,
             credential=None):
        uploaded = {}
        blob_client = Mock()
        blob_client.upload_blob.side_effect = lambda stream, **kwargs: uploaded.update(
            data=stream.read(), kwargs=kwargs
        )
        blob_service = Mock()
        blob_service.get_blob_client.return_value = blob_client
        credential = credential or Mock()

        def blob_factory(**kwargs):
            self.assertEqual(kwargs["credential"], credential)
            return blob_service

        name = run_backup(
            config or make_config(),
            credential_factory=lambda **kwargs: credential,
            blob_service_factory=blob_factory,
            subprocess_run=subprocess_run,
            temp_dir_factory=temp_dir_factory,
            clock=lambda: datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc),
        )
        return name, uploaded, blob_client, credential

    def test_dumps_one_database_validates_and_uploads_with_metadata(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "pg_dump" and "--file" in args:
                Path(args[args.index("--file") + 1]).write_bytes(b"valid archive")
            if args == ["pg_dump", "--version"]:
                return SimpleNamespace(stdout="pg_dump (PostgreSQL) 16.4 (Debian 16.4-1.pgdg120+1)\n")
            return SimpleNamespace(stdout="")

        name, uploaded, client, credential = self._run(subprocess_run=fake_run)
        self.assertEqual(name, "sampledb/sampledb_20261013_100000.dump")
        dump_args = calls[0][0]
        self.assertEqual(dump_args[dump_args.index("--dbname") + 1], "sampledb")
        self.assertTrue(dump_args[dump_args.index("--file") + 1].endswith("sampledb_20261013_100000.dump"))
        self.assertIn("--format=custom", dump_args)
        self.assertEqual(calls[0][1]["env"]["PGPASSWORD"], "test-password")
        self.assertEqual(calls[1][0][:2], ["pg_restore", "--list"])
        self.assertEqual(uploaded["data"], b"valid archive")
        metadata = uploaded["kwargs"]["metadata"]
        self.assertEqual(metadata["sha256"], hashlib.sha256(b"valid archive").hexdigest())
        self.assertEqual(metadata["bytes"], str(len(b"valid archive")))
        self.assertEqual(metadata["database"], "sampledb")
        self.assertEqual(metadata["pg_version"], "pg_dump (PostgreSQL) 16.4 (Debian 16.4-1.pgdg120+1)")
        self.assertFalse(uploaded["kwargs"]["overwrite"])
        credential.close.assert_called_once()
        client.upload_blob.assert_called_once()

    def test_authenticates_with_entra_managed_identity_token(self):
        config = make_config(password=None, managed_identity_client_id="identity-client-id")
        credential = Mock()
        credential.get_token.return_value = SimpleNamespace(token="ey-entra-access-token")
        calls = []

        def fake_run(args, **kwargs):
            calls.append(kwargs)
            if args[0] == "pg_dump" and "--file" in args:
                Path(args[args.index("--file") + 1]).write_bytes(b"archive")
            if args == ["pg_dump", "--version"]:
                return SimpleNamespace(stdout="pg_dump (PostgreSQL) 16.4")
            return SimpleNamespace(stdout="")

        self._run(
            subprocess_run=fake_run,
            config=config,
            credential=credential,
        )
        credential.get_token.assert_called_once_with(POSTGRES_ENTRA_SCOPE)
        self.assertEqual(calls[0]["env"]["PGPASSWORD"], "ey-entra-access-token")

    def test_postgres_18_client_uploads_with_version_metadata(self):
        for version in (
            "pg_dump (PostgreSQL) 18.0",
            "pg_dump (PostgreSQL) 18.3 (Debian 18.3-1.pgdg12+1)",
        ):
            with self.subTest(version=version):
                def fake_run(args, **kwargs):
                    if args[0] == "pg_dump" and "--file" in args:
                        Path(args[args.index("--file") + 1]).write_bytes(b"archive")
                    return SimpleNamespace(
                        stdout=version if args == ["pg_dump", "--version"] else ""
                    )

                _, uploaded, client, _ = self._run(subprocess_run=fake_run)
                self.assertEqual(uploaded["kwargs"]["metadata"]["pg_version"], version)
                client.upload_blob.assert_called_once()

    def test_unsupported_client_never_uploads(self):
        blob_service = Mock()

        def fake_run(args, **kwargs):
            if args[0] == "pg_dump" and "--file" in args:
                Path(args[args.index("--file") + 1]).write_bytes(b"archive")
            return SimpleNamespace(stdout="pg_dump (PostgreSQL) 17.0")

        with self.assertRaisesRegex(RuntimeError, "PostgreSQL 16 or 18"):
            run_backup(
                make_config(),
                credential_factory=lambda **kwargs: Mock(),
                blob_service_factory=lambda **kwargs: blob_service,
                subprocess_run=fake_run,
            )
        blob_service.get_blob_client.assert_not_called()

    def test_subprocess_failure_cleans_temporary_archive_and_does_not_upload(self):
        root = tempfile.TemporaryDirectory()
        created = []

        class TrackedTempDir:
            def __enter__(self):
                self.path = Path(root.name) / "work"
                self.path.mkdir()
                created.append(self.path)
                return str(self.path)

            def __exit__(self, *args):
                for child in self.path.iterdir():
                    child.unlink()
                self.path.rmdir()

        blob_service = Mock()

        def fail_dump(args, **kwargs):
            if args[0] == "pg_dump":
                if "--file" in args:
                    Path(args[args.index("--file") + 1]).write_bytes(b"partial")
                raise OSError("contains details that must not reach logs")

        credential = Mock()
        with self.assertRaises(OSError):
            run_backup(
                make_config(),
                credential_factory=lambda **kwargs: credential,
                blob_service_factory=lambda **kwargs: blob_service,
                subprocess_run=fail_dump,
                temp_dir_factory=lambda **kwargs: TrackedTempDir(),
            )
        self.assertFalse(created[0].exists())
        blob_service.get_blob_client.assert_not_called()
        credential.close.assert_called_once()
        root.cleanup()

    def test_invalid_archive_never_uploads(self):
        blob_service = Mock()
        credential = Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            def bad_archive(args, **kwargs):
                if args[0] == "pg_dump":
                    Path(args[args.index("--file") + 1]).write_bytes(b"not valid")
                raise RuntimeError("restore list failed")

            with self.assertRaises(RuntimeError):
                run_backup(
                    make_config(),
                    credential_factory=lambda **kwargs: credential,
                    blob_service_factory=lambda **kwargs: blob_service,
                    subprocess_run=bad_archive,
                    temp_dir_factory=lambda **kwargs: tempfile.TemporaryDirectory(dir=temp_dir),
                )
            self.assertEqual(os.listdir(temp_dir), [])


if __name__ == "__main__":
    unittest.main()
