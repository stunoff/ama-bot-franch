#!/usr/bin/env python3
"""Run explicitly queued maintenance on the Docker host. No network ports or user volumes are used for restore tests."""

import argparse
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ID = re.compile(r"^[a-f0-9]{32}$")


def command(argv, *, data=None, env=None, output=None):
    result = subprocess.run(argv, input=data, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        raise RuntimeError("Command failed: " + argv[0] + " (private stderr suppressed)")
    return result.stdout


def compose(*args, **kwargs):
    return command(["docker", "compose", *args], **kwargs)


def operator(*args, data=None):
    raw = compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "api",
        "-m",
        "ama.ops_cli",
        *args,
        data=json.dumps(data).encode() if data is not None else None,
    )
    return json.loads(raw)


def digest(path):
    h = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return {"size": Path(path).stat().st_size, "sha256": h.hexdigest()}


def safe_directory(root, ident):
    if not ID.fullmatch(ident):
        raise RuntimeError("Invalid operation ID")
    if Path(root).is_symlink():
        raise RuntimeError("Backup root must not be a symlink")
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / ident
    if target.is_symlink() or target.resolve().parent != root:
        raise RuntimeError("Unsafe backup directory")
    return target


def backup(root, ident):
    target = safe_directory(root, ident)
    if target.exists():
        raise RuntimeError("Backup ID already exists; inspect before reconciling")
    temp = target.parent / (".partial-" + ident)
    temp.mkdir(mode=0o700)
    running = compose("ps", "--status", "running", "--services").decode().splitlines()
    stopped = [s for s in ("api", "worker") if s in running]
    try:
        if stopped:
            compose("stop", *stopped)
        snap = json.loads(
            compose(
                "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "api", "-m", "ama.ops_snapshot", "snapshot"
            )
        )
        dump_args = [
            "docker",
            "compose",
            "exec",
            "-T",
            "mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --single-transaction --routines --triggers --no-tablespaces --hex-blob ama',
        ]
        with (temp / "mysql.sql.gz").open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as zipped:
            with tempfile.TemporaryFile() as errors:
                proc = subprocess.Popen(dump_args, stdout=subprocess.PIPE, stderr=errors)
                shutil.copyfileobj(proc.stdout, zipped)
                if proc.wait():
                    raise RuntimeError("MySQL dump failed")
        with (temp / "files.tar.gz").open("xb") as stream:
            compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python",
                "api",
                "-m",
                "ama.ops_snapshot",
                "archive",
                output=stream,
            )
        # Re-read after both exports while writes remain stopped.
        after = json.loads(
            compose(
                "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "api", "-m", "ama.ops_snapshot", "snapshot"
            )
        )
        if after != snap:
            raise RuntimeError("Source changed during backup; snapshot not committed")
        manifest = {
            "format": 1,
            "backup_id": ident,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot": snap,
            "artifacts": {name: digest(temp / name) for name in ("mysql.sql.gz", "files.tar.gz")},
        }
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
        for p in temp.iterdir():
            p.chmod(0o600)
        temp.rename(target)
        return {
            "backup_id": ident,
            "manifest_sha256": digest(target / "manifest.json")["sha256"],
            "version": snap["version"],
        }
    finally:
        if stopped:
            compose("start", *stopped)


def validate_backup(root, ident):
    folder = safe_directory(root, ident)
    manifest_path = folder / "manifest.json"
    if not folder.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("Backup manifest unavailable")
    data = json.loads(manifest_path.read_text())
    if (
        data.get("format") != 1
        or data.get("backup_id") != ident
        or set(data.get("artifacts", {})) != {"mysql.sql.gz", "files.tar.gz"}
    ):
        raise RuntimeError("Invalid backup manifest")
    for name, expected in data["artifacts"].items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise RuntimeError("Backup checksum mismatch")
    return folder, data


def verify_restore(root, ident):
    folder, manifest = validate_backup(root, ident)
    config = json.loads(compose("config", "--format", "json"))
    mysql_image = config["services"]["mysql"]["image"]
    app_image = compose("images", "-q", "api").decode().strip()
    if not app_image:
        raise RuntimeError("Build the application image first")
    suffix = uuid4().hex
    container = "ama-restore-" + suffix
    data_volume = container + "-db"
    files_volume = container + "-files"
    env = {**os.environ, "MYSQL_ROOT_PASSWORD": uuid4().hex + uuid4().hex, "MYSQL_DATABASE": "ama"}
    env["MYSQL_PWD"] = env["MYSQL_ROOT_PASSWORD"]
    db_url = "mysql+pymysql://root:" + env["MYSQL_ROOT_PASSWORD"] + "@127.0.0.1:3306/ama?charset=utf8mb4"
    env["DATABASE_URL"] = db_url
    made = []
    try:
        for volume in (data_volume, files_volume):
            command(["docker", "volume", "create", "--label", "ama.restore-test=true", volume])
            made.append(volume)
        command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--network",
                "none",
                "-e",
                "MYSQL_ROOT_PASSWORD",
                "-e",
                "MYSQL_DATABASE",
                "-v",
                data_volume + ":/var/lib/mysql",
                mysql_image,
            ],
            env=env,
        )
        deadline = time.monotonic() + 180
        while True:
            r = subprocess.run(
                ["docker", "exec", "-e", "MYSQL_PWD", container, "mysql", "-uroot", "-Nse", "SELECT 1"],
                env=env,
                capture_output=True,
            )
            if r.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Isolated MySQL did not become ready")
            time.sleep(1)
        with gzip.open(folder / "mysql.sql.gz", "rb") as source, tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(
                ["docker", "exec", "-i", "-e", "MYSQL_PWD", container, "mysql", "-uroot", "ama"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                env=env,
            )
            try:
                shutil.copyfileobj(source, proc.stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            if proc.wait():
                raise RuntimeError("Isolated database restore failed")
        inventory = manifest["snapshot"]["files"]
        with (folder / "files.tar.gz").open("rb") as stream:
            args = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--user",
                "0:0",
                "-e",
                "FILE_ROOT=/data/files",
                "-v",
                files_volume + ":/data/files",
                "--entrypoint",
                "python",
                app_image,
                "-m",
                "ama.ops_snapshot",
                "extract",
                "--max-bytes",
                str(sum(x["size"] for x in inventory.values())),
                "--max-files",
                str(len(inventory)),
            ]
            proc = subprocess.run(args, stdin=stream, capture_output=True)
            if proc.returncode:
                raise RuntimeError("Isolated file restore failed")
        raw = command(
            [
                "docker",
                "run",
                "--rm",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--network",
                "container:" + container,
                "-e",
                "DATABASE_URL",
                "-e",
                "FILE_ROOT=/data/files",
                "-v",
                files_volume + ":/data/files:ro",
                "--entrypoint",
                "python",
                app_image,
                "-m",
                "ama.ops_snapshot",
                "snapshot",
            ],
            env=env,
        )
        restored = json.loads(raw)
        if restored != manifest["snapshot"]:
            raise RuntimeError("Restored table/file fingerprints do not match")
        return {
            "backup_id": ident,
            "manifest_sha256": digest(folder / "manifest.json")["sha256"],
            "verified_tables": len(restored["tables"]),
            "verified_files": len(restored["files"]),
            "version": restored["version"],
        }
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for volume in made:
            subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backup-root", required=True)
    p.add_argument("--request-backup", action="store_true")
    p.add_argument("--verify-backup", metavar="ID")
    p.add_argument("--loop", action="store_true")
    args = p.parse_args()
    os.umask(0o077)
    if not Path("compose.yaml").is_file():
        p.error("Run from project root")
    if args.request_backup:
        operator("request", "backup", "--reason", "Operator requested consistent backup")
    if args.verify_backup:
        operator(
            "request",
            "verify_restore",
            "--reason",
            "Operator requested isolated restore test",
            "--backup-id",
            args.verify_backup,
        )
    while True:
        job = operator("claim")
        if job:
            success = False
            details = {"reason_code": "operator_procedure_failed"}
            try:
                if job["kind"] == "backup":
                    details = backup(args.backup_root, job["id"])
                elif job["kind"] == "verify_restore":
                    details = verify_restore(args.backup_root, job["backup_id"])
                else:
                    raise RuntimeError("Unsupported operation")
                success = True
            except Exception as error:
                print("Maintenance failed:", type(error).__name__, "job", job["id"], file=sys.stderr)
            finish_args = ["finish", job["id"]] + (["--success"] if success else [])
            operator(*finish_args, data={"claim_token": job["claim_token"], "details": details})
            print(json.dumps({"id": job["id"], "status": "succeeded" if success else "failed"}))
            if not success and not args.loop:
                raise SystemExit(1)
        elif not args.loop:
            print("No queued maintenance jobs")
        if not args.loop:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
