from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MAKE = shutil.which("make") or "make"


@dataclass
class StorageHarness:
    root: Path
    env: dict[str, str] = field(repr=False)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [MAKE, "--no-print-directory", "-f", str(ROOT / "Makefile"), *args],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        log = self.root / "docker.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def prepare(self) -> None:
        for name in ("docker", "redis"):
            (self.root / "data" / name).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def storage(tmp_path: Path) -> StorageHarness:
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in (
        "mkdir",
        "rmdir",
        "grep",
        "mktemp",
        "rm",
        "readlink",
        "tar",
        "ls",
        "touch",
    ):
        executable = shutil.which(name)
        assert executable is not None
        (binary / name).symlink_to(executable)
    acl = binary / "setfacl"
    acl.write_text('#!/bin/sh\nexit "${FAKE_ACL_STATUS:-0}"\n')
    acl.chmod(0o755)
    docker = binary / "docker"
    docker.write_text(
        """#!/usr/bin/python3
import json
import os
import pathlib
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["volume", "ls"]:
    if os.environ.get("FAKE_VOLUME_ERROR"):
        sys.exit(1)
    print(os.environ.get("FAKE_VOLUMES", ""))
elif args[:2] == ["ps", "-q"]:
    print(os.environ.get("FAKE_VOLUME_RUNNING", ""))
elif args and args[0] == "compose" and "ps" in args:
    print(os.environ.get("FAKE_COMPOSE_RUNNING", ""))
elif args and args[0] == "compose" and "build" in args:
    pass
elif args and args[0] == "compose" and "run" in args:
    destination = pathlib.Path("data/docker/price-alert-bot.sqlite3")
    sidecars = [
        pathlib.Path(str(destination) + s) for s in ("-wal", "-shm", "-journal")
    ]
    if destination.exists() and "--overwrite" not in args:
        sys.exit(1)
    if any(p.exists() for p in sidecars):
        sys.exit(1)
    destination.write_bytes(sys.stdin.buffer.read())
elif args and args[0] == "run" and "redis:7.4.11-alpine" in args:
    if os.environ.get("FAKE_REDIS_PARTIAL"):
        pathlib.Path("data/redis/dump.rdb").write_bytes(b"incomplete")
        sys.exit(1)
    script = args[-1].replace("/tmp/source.tar", "__FAKE_TAR__")
    script = script.replace("/source", os.environ["FAKE_SOURCE"])
    script = script.replace("/destination", str(pathlib.Path("data/redis").resolve()))
    script = script.replace("__FAKE_TAR__", os.environ["FAKE_TAR"])
    sys.exit(subprocess.run(["/bin/sh", "-ec", script], check=False).returncode)
elif args and args[0] == "run":
    if os.environ.get("FAKE_SOURCE_ERROR"):
        sys.exit(1)
    if os.environ.get("FAKE_RACE"):
        pathlib.Path("data/docker/price-alert-bot.sqlite3").write_bytes(b"existing")
    sys.stdout.buffer.write(b"checked snapshot")
else:
    sys.exit(2)
"""
    )
    docker.chmod(0o755)
    (tmp_path / "Makefile").symlink_to(ROOT / "Makefile")
    source = tmp_path / "legacy"
    source.mkdir()
    env = {
        "PATH": str(binary),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_SOURCE": str(source),
        "FAKE_TAR": str(tmp_path / "source.tar"),
        "FAKE_VOLUMES": "",
        "TMPDIR": str(tmp_path),
    }
    return StorageHarness(tmp_path, env)


def test_prepare_keeps_existing_directory_and_its_contents(
    storage: StorageHarness,
) -> None:
    storage.prepare()
    db = storage.root / "data/docker/existing"
    db.write_text("existing")
    directory = db.parent
    directory.chmod(0o710)
    result = storage.run("storage-prepare")
    assert result.returncode == 0, result.stderr
    assert db.read_text() == "existing"
    assert directory.stat().st_mode & 0o777 == 0o710


def test_prepare_fails_without_acl_utility(storage: StorageHarness) -> None:
    (storage.root / "bin/setfacl").unlink()
    result = storage.run("storage-prepare")
    assert result.returncode != 0
    assert "setfacl" in result.stderr
    assert not (storage.root / "data").exists()


def test_prepare_fails_and_removes_new_directory_if_acl_fails(
    storage: StorageHarness,
) -> None:
    storage.env["FAKE_ACL_STATUS"] = "1"
    result = storage.run("storage-prepare")
    assert result.returncode != 0
    assert not (storage.root / "data/docker").exists()


@pytest.mark.parametrize(
    ("volume", "expected"),
    [
        ("price-alert-bot_sqlite_data", "SQLite"),
        ("price-alert-bot_redisdata", "Redis"),
    ],
)
def test_start_refuses_empty_storage_with_legacy_volume(
    storage: StorageHarness, volume: str, expected: str
) -> None:
    storage.prepare()
    storage.env["FAKE_VOLUMES"] = volume
    result = storage.run("docker-up")
    assert result.returncode != 0
    assert expected in result.stderr
    assert not any("build" in call for call in storage.calls())
    assert not any("up" in call for call in storage.calls())


def test_storage_check_fails_when_docker_cannot_list_volumes(
    storage: StorageHarness,
) -> None:
    storage.prepare()
    storage.env["FAKE_VOLUME_ERROR"] = "1"
    assert storage.run("storage-check").returncode != 0


@pytest.mark.parametrize("target", ["db-import-volume", "redis-import-volume"])
def test_import_refuses_missing_source(storage: StorageHarness, target: str) -> None:
    storage.prepare()
    result = storage.run(target)
    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert not any(call[0] == "run" for call in storage.calls())


@pytest.mark.parametrize("target", ["db-import-volume", "redis-import-volume"])
@pytest.mark.parametrize("busy", ["FAKE_COMPOSE_RUNNING", "FAKE_VOLUME_RUNNING"])
def test_import_refuses_while_source_is_in_use(
    storage: StorageHarness, target: str, busy: str
) -> None:
    storage.prepare()
    storage.env["FAKE_VOLUMES"] = (
        "price-alert-bot_sqlite_data\nprice-alert-bot_redisdata"
    )
    storage.env[busy] = "running-container"
    result = storage.run(target)
    assert result.returncode != 0
    assert not any(call[0] == "run" for call in storage.calls())


@pytest.mark.parametrize("symlink", [False, True])
def test_sqlite_import_never_replaces_destination_even_with_overwrite(
    storage: StorageHarness, symlink: bool
) -> None:
    storage.prepare()
    destination = storage.root / "data/docker/price-alert-bot.sqlite3"
    original = storage.root / "original"
    original.write_bytes(b"existing")
    if symlink:
        destination.symlink_to(original)
    else:
        destination.write_bytes(b"existing")
    storage.env["FAKE_VOLUMES"] = "price-alert-bot_sqlite_data"
    result = storage.run("db-import-volume", "OVERWRITE=1")
    assert result.returncode != 0
    assert original.read_bytes() == b"existing"
    assert destination.read_bytes() == b"existing"
    assert not any(call[0] == "run" for call in storage.calls())


def test_sqlite_import_does_not_overwrite_file_created_during_backup(
    storage: StorageHarness,
) -> None:
    storage.prepare()
    storage.env.update(FAKE_VOLUMES="price-alert-bot_sqlite_data", FAKE_RACE="1")
    result = storage.run("db-import-volume", "OVERWRITE=1")
    assert result.returncode != 0
    assert (
        storage.root / "data/docker/price-alert-bot.sqlite3"
    ).read_bytes() == b"existing"
    restore = [
        call for call in storage.calls() if call[0] == "compose" and "run" in call
    ]
    assert len(restore) == 1
    assert "--overwrite" not in restore[0]


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sqlite_import_refuses_destination_sidecars(
    storage: StorageHarness, suffix: str
) -> None:
    storage.prepare()
    sidecar = storage.root / f"data/docker/price-alert-bot.sqlite3{suffix}"
    sidecar.write_bytes(b"existing sidecar")
    storage.env["FAKE_VOLUMES"] = "price-alert-bot_sqlite_data"
    assert storage.run("db-import-volume").returncode != 0
    assert sidecar.read_bytes() == b"existing sidecar"
    assert not (storage.root / "data/docker/price-alert-bot.sqlite3").exists()


def test_sqlite_import_propagates_backup_failure(storage: StorageHarness) -> None:
    storage.prepare()
    storage.env.update(
        FAKE_VOLUMES="price-alert-bot_sqlite_data", FAKE_SOURCE_ERROR="1"
    )
    assert storage.run("db-import-volume").returncode != 0
    assert not (storage.root / "data/docker/price-alert-bot.sqlite3").exists()
    assert not any(call[0] == "compose" and "run" in call for call in storage.calls())
    assert not list(storage.root.glob("price-alert-import.*"))


def test_sqlite_import_publishes_checked_snapshot(storage: StorageHarness) -> None:
    storage.prepare()
    storage.env["FAKE_VOLUMES"] = "price-alert-bot_sqlite_data"
    result = storage.run("db-import-volume")
    assert result.returncode == 0, result.stderr
    destination = storage.root / "data/docker/price-alert-bot.sqlite3"
    assert destination.read_bytes() == b"checked snapshot"
    assert not list(storage.root.glob("price-alert-import.*"))


def test_redis_import_refuses_nonempty_destination(storage: StorageHarness) -> None:
    storage.prepare()
    destination = storage.root / "data/redis/dump.rdb"
    destination.write_bytes(b"existing")
    storage.env["FAKE_VOLUMES"] = "price-alert-bot_redisdata"
    assert storage.run("redis-import-volume").returncode != 0
    assert destination.read_bytes() == b"existing"
    assert not (storage.root / "data/redis/.volume-imported").exists()


def test_redis_import_refuses_reserved_marker_in_source(
    storage: StorageHarness,
) -> None:
    storage.prepare()
    source_marker = storage.root / "legacy/.volume-imported"
    source_marker.write_text("existing")
    storage.env["FAKE_VOLUMES"] = "price-alert-bot_redisdata"
    assert storage.run("redis-import-volume").returncode != 0
    assert source_marker.read_text() == "existing"
    assert not (storage.root / "data/redis/.volume-imported").exists()


def test_redis_import_marks_empty_volume_as_migrated(storage: StorageHarness) -> None:
    storage.prepare()
    storage.env["FAKE_VOLUMES"] = "price-alert-bot_redisdata"
    result = storage.run("redis-import-volume")
    assert result.returncode == 0, result.stderr
    assert (storage.root / "data/redis/.volume-imported").is_file()
    assert storage.run("storage-check").returncode == 0


def test_redis_import_copies_payload_without_changing_source(
    storage: StorageHarness,
) -> None:
    storage.prepare()
    source = storage.root / "legacy/dump.rdb"
    source.write_bytes(b"original state")
    storage.env["FAKE_VOLUMES"] = "price-alert-bot_redisdata"
    result = storage.run("redis-import-volume")
    assert result.returncode == 0, result.stderr
    assert (storage.root / "data/redis/dump.rdb").read_bytes() == b"original state"
    assert source.read_bytes() == b"original state"
    assert (storage.root / "data/redis/.volume-imported").is_file()
    assert storage.run("storage-check").returncode == 0


def test_redis_import_propagates_copy_failure(storage: StorageHarness) -> None:
    storage.prepare()
    tar = storage.root / "bin/tar"
    tar.unlink()
    tar.write_text("#!/bin/sh\nexit 1\n")
    tar.chmod(0o755)
    storage.env["FAKE_VOLUMES"] = "price-alert-bot_redisdata"
    assert storage.run("redis-import-volume").returncode != 0
    assert not (storage.root / "data/redis/.volume-imported").exists()


def test_failed_redis_import_cannot_be_mistaken_for_complete(
    storage: StorageHarness,
) -> None:
    storage.prepare()
    storage.env.update(FAKE_VOLUMES="price-alert-bot_redisdata", FAKE_REDIS_PARTIAL="1")
    assert storage.run("redis-import-volume").returncode != 0
    assert (storage.root / "data/redis/dump.rdb").exists()
    assert not (storage.root / "data/redis/.volume-imported").exists()
    assert storage.run("storage-check").returncode != 0
