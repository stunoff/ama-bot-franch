"""Private, deterministic database/file snapshots used only by the host operator."""

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
from sqlalchemy import MetaData, select, text
from ama.db import engine
from ama.config import settings
from ama.errors import BusinessError
from ama.preflight import VERSION


def file_inventory(root):
    root = Path(root)
    if root.is_symlink():
        raise BusinessError("Storage root must not be a symlink")
    if not root.is_dir():
        raise BusinessError("Storage root missing")
    result = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise BusinessError("Storage symlink not allowed")
        if p.is_dir():
            continue
        if not p.is_file():
            raise BusinessError("Unsupported storage entry")
        digest = sha256()
        with p.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result[p.relative_to(root).as_posix()] = {"size": p.stat().st_size, "sha256": digest.hexdigest()}
    return result


def snapshot(db_engine=engine, root=None):
    meta = MetaData()
    meta.reflect(db_engine)
    tables = {}
    with db_engine.connect() as conn:
        for t in sorted(meta.tables.values(), key=lambda t: t.name):
            digest = sha256()
            count = 0
            stmt = select(t).order_by(*t.primary_key.columns)
            for row in conn.execute(stmt).mappings():
                payload = json.dumps(
                    dict(row), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
                ).encode()
                digest.update(payload + b"\n")
                count += 1
            tables[t.name] = {"rows": count, "sha256": digest.hexdigest()}
        revision = (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if "alembic_version" in meta.tables
            else None
        )
    return {
        "format": 1,
        "version": VERSION,
        "revision": revision,
        "tables": tables,
        "files": file_inventory(root or settings().file_root),
    }


def extract(stream, root, max_bytes, max_files):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise BusinessError("Restore destination must be empty")
    count = 0
    total = 0
    seen = set()
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "files":
                raise BusinessError("Unsafe archive path")
            if member.isdir():
                continue
            if not member.isfile() or len(name.parts) < 2 or str(name) in seen:
                raise BusinessError("Unsafe or duplicate archive member")
            seen.add(str(name))
            count += 1
            total += member.size
            if count > max_files or total > max_bytes:
                raise BusinessError("Archive exceeds declared inventory")
            target = root.joinpath(*name.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            remaining = member.size
            with target.open("xb") as dest:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise BusinessError("Truncated archive")
                    dest.write(chunk)
                    remaining -= len(chunk)
            target.chmod(0o600)
            if hasattr(__import__("os"), "chown") and __import__("os").geteuid() == 0:
                __import__("os").chown(target, 10001, 10001)
    # Directories must remain traversable to the unprivileged app user.
    import os

    if os.geteuid() == 0:
        for directory in [root, *[p for p in root.rglob("*") if p.is_dir()]]:
            os.chown(directory, 10001, 10001)
            directory.chmod(0o700)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["snapshot", "archive", "extract"])
    parser.add_argument("--max-bytes", type=int, default=0)
    parser.add_argument("--max-files", type=int, default=0)
    args = parser.parse_args()
    root = Path(settings().file_root)
    if args.action == "snapshot":
        print(json.dumps(snapshot(), ensure_ascii=False, sort_keys=True))
    elif args.action == "extract":
        extract(sys.stdin.buffer, root, args.max_bytes, args.max_files)
    else:
        inventory = file_inventory(root)
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
            for name in inventory:
                archive.add(root / name, arcname="files/" + name, recursive=False)


if __name__ == "__main__":
    main()
