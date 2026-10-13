"""Create and upload a verified custom-format PostgreSQL backup."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

DATABASE_NAME = "salesdb"
POSTGRES_ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


@dataclass(frozen=True)
class BackupConfig:
    host: str
    port: int
    database: str
    user: str
    sslmode: str
    sslrootcert: str
    blob_account_url: str
    blob_container: str
    password: str | None = field(default=None, repr=False)
    managed_identity_client_id: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "BackupConfig":
        env = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError(f"Missing required environment variable: {name}")
            return value

        try:
            port = int(env.get("PGPORT", "5432"))
        except ValueError as exc:
            raise ValueError("PGPORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("PGPORT must be between 1 and 65535")

        database = required("PGDATABASE")
        if database != DATABASE_NAME:
            raise ValueError(f"This job is restricted to the {DATABASE_NAME} database")

        sslmode = env.get("PGSSLMODE", "verify-full").strip()
        if sslmode != "verify-full":
            raise ValueError("PGSSLMODE must be verify-full")

        return cls(
            host=required("PGHOST"),
            port=port,
            database=database,
            user=required("PGUSER"),
            sslmode=sslmode,
            sslrootcert=env.get("PGSSLROOTCERT", "system").strip() or "system",
            blob_account_url=required("BLOB_ACCOUNT_URL"),
            blob_container=required("BLOB_CONTAINER"),
            password=env.get("PGPASSWORD") or None,
            managed_identity_client_id=env.get("AZURE_CLIENT_ID", "").strip() or None,
        )


def _azure_credential(config: BackupConfig, credential_factory=None):
    if credential_factory is None:
        from azure.identity import DefaultAzureCredential

        credential_factory = DefaultAzureCredential
    kwargs = {}
    if config.managed_identity_client_id:
        kwargs["managed_identity_client_id"] = config.managed_identity_client_id
    return credential_factory(**kwargs)


def _get_password(config: BackupConfig, credential) -> str:
    if config.password:
        return config.password
    token = credential.get_token(POSTGRES_ENTRA_SCOPE)
    if not token or not getattr(token, "token", None):
        raise RuntimeError("Failed to obtain Entra ID access token for PostgreSQL")
    return token.token


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as archive:
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _backup_key(now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{DATABASE_NAME}/{DATABASE_NAME}_{stamp}.dump"


def run_backup(
    config: BackupConfig | None = None,
    *,
    credential_factory=None,
    blob_service_factory=None,
    subprocess_run: Callable = subprocess.run,
    temp_dir_factory=tempfile.TemporaryDirectory,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> str:
    """Back up the configured database, validate its archive, and upload it."""
    config = config or BackupConfig.from_env()
    credential = _azure_credential(config, credential_factory)
    try:
        password = _get_password(config, credential)
        if blob_service_factory is None:
            from azure.storage.blob import BlobServiceClient

            blob_service_factory = BlobServiceClient
        blob_service = blob_service_factory(
            account_url=config.blob_account_url, credential=credential
        )
        pg_env = os.environ.copy()
        pg_env.update(
            {
                "PGHOST": config.host,
                "PGPORT": str(config.port),
                "PGDATABASE": config.database,
                "PGUSER": config.user,
                "PGPASSWORD": password,
                "PGSSLMODE": config.sslmode,
                "PGSSLROOTCERT": config.sslrootcert,
            }
        )

        backed_up_at = clock().astimezone(timezone.utc)
        blob_name = _backup_key(backed_up_at)
        archive_filename = Path(blob_name).name

        with temp_dir_factory(prefix="pg-backup-") as temp_dir:
            archive_path = Path(temp_dir) / archive_filename
            subprocess_run(
                [
                    "pg_dump",
                    "--no-password",
                    "--format=custom",
                    "--host",
                    config.host,
                    "--port",
                    str(config.port),
                    "--username",
                    config.user,
                    "--dbname",
                    DATABASE_NAME,
                    "--file",
                    str(archive_path),
                ],
                env=pg_env,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess_run(
                ["pg_restore", "--list", str(archive_path)],
                env=pg_env,
                check=True,
                capture_output=True,
                text=True,
            )
            version_result = subprocess_run(
                ["pg_dump", "--version"],
                env=pg_env,
                check=True,
                capture_output=True,
                text=True,
            )
            version = version_result.stdout.strip()
            match = re.fullmatch(
                r"pg_dump \(PostgreSQL\) (16\.[0-9]+)(?: \([^\r\n]+\))?", version
            )
            if not match:
                raise RuntimeError("A PostgreSQL 16 pg_dump client is required")

            digest, size = _sha256_and_size(archive_path)
            blob_client = blob_service.get_blob_client(
                container=config.blob_container, blob=blob_name
            )
            with archive_path.open("rb") as archive:
                blob_client.upload_blob(
                    archive,
                    overwrite=False,
                    metadata={
                        "sha256": digest,
                        "bytes": str(size),
                        "database": DATABASE_NAME,
                        "server": config.host,
                        "backed_up_at": backed_up_at.isoformat(),
                        "pg_version": version,
                    },
                )
            return blob_name
    finally:
        close = getattr(credential, "close", None)
        if close:
            close()


def main() -> int:
    try:
        blob_name = run_backup()
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    print(f"Backup uploaded: {blob_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
