"""Deterministic Git-revision app bundles and local, isolated installations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

MAX_TAR_BYTES = 20_000_000
MAX_FILES = 2000
ZIP_NAMES = {"manifest.json", "index.json", "source.tar"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_manifest(value: Any) -> dict:
    fields = {"schema_version", "id", "version", "description", "source", "python_min", "entry_module", "evidence_class"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("manifest fields do not match the v1 app contract")
    if value["schema_version"] != 1:
        raise ValueError("unsupported app manifest version")
    if not isinstance(value["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", value["id"]):
        raise ValueError("invalid app id")
    if not isinstance(value["version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", value["version"]):
        raise ValueError("app version must be major.minor.patch")
    if not isinstance(value["description"], str) or not value["description"].strip():
        raise ValueError("description required")
    source = value["source"]
    if not isinstance(source, dict) or set(source) != {"url", "commit"}:
        raise ValueError("source requires url and commit")
    if not isinstance(source["url"], str) or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", source["url"]):
        raise ValueError("source must be a GitHub HTTPS repository URL")
    if not isinstance(source["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("source.commit must be a full lowercase SHA-1")
    if not isinstance(value["python_min"], str) or not re.fullmatch(r"3\.(?:1[1-9]|[2-9]\d)", value["python_min"]):
        raise ValueError("python_min must be Python 3.11 or newer")
    if not isinstance(value["entry_module"], str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", value["entry_module"]):
        raise ValueError("entry_module must be a Python module path")
    if value["evidence_class"] != "SYNTHETIC_DEMO_ONLY":
        raise ValueError("v1 bundles support synthetic demo claims only")
    return value


def _git(source_dir: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(source_dir), *args], capture_output=True)
    if proc.returncode:
        raise ValueError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _validated_tar(raw: bytes) -> list[tarfile.TarInfo]:
    if len(raw) > MAX_TAR_BYTES:
        raise ValueError("source archive exceeds size limit")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
    if len(members) > MAX_FILES:
        raise ValueError("source archive contains too many entries")
    seen: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or not path.parts or ".." in path.parts or
                member.name.startswith("./") or "\\" in member.name or
                not (member.isfile() or member.isdir())):
            raise ValueError("source archive contains unsafe entry")
        if str(path) in seen:
            raise ValueError("source archive contains duplicate entry")
        seen.add(str(path))
        if member.isfile() and member.size > MAX_TAR_BYTES:
            raise ValueError("source file exceeds size limit")
    return members


def pack(manifest_path: Path, source_dir: Path, output: Path) -> dict:
    manifest = validate_manifest(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    source_dir = Path(source_dir).resolve()
    head = _git(source_dir, "rev-parse", "HEAD").decode().strip()
    if head != manifest["source"]["commit"]:
        raise ValueError("local source HEAD differs from pinned manifest commit")
    dirty = _git(source_dir, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise ValueError("tracked source files are modified; use a clean checkout")
    raw_tar = _git(source_dir, "archive", "--format=tar", head)
    _validated_tar(raw_tar)
    manifest_raw = _canonical(manifest)
    index = {"format": "a2z-app-bundle-v1", "manifest_sha256": _sha(manifest_raw),
             "source_tar_sha256": _sha(raw_tar), "source_commit": head}
    if output.exists():
        raise ValueError("bundle output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as bundle:
            for name, raw in (("manifest.json", manifest_raw), ("index.json", _canonical(index)), ("source.tar", raw_tar)):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                bundle.writestr(info, raw)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {"bundle": str(output), "bundle_sha256": _sha(output.read_bytes()),
            "source_commit": head, "source_tar_sha256": index["source_tar_sha256"]}


def _read_bundle(path: Path) -> tuple[dict, dict, bytes]:
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        if len(names) != len(ZIP_NAMES) or set(names) != ZIP_NAMES:
            raise ValueError("bundle has missing, duplicate or unexpected entries")
        if any(info.file_size > MAX_TAR_BYTES for info in bundle.infolist()):
            raise ValueError("bundle entry exceeds size limit")
        raw_manifest, raw_index, raw_tar = (bundle.read(name) for name in ("manifest.json", "index.json", "source.tar"))
    manifest = validate_manifest(json.loads(raw_manifest))
    index = json.loads(raw_index)
    if not isinstance(index, dict) or set(index) != {"format", "manifest_sha256", "source_tar_sha256", "source_commit"}:
        raise ValueError("invalid bundle index")
    if (index["format"] != "a2z-app-bundle-v1" or index["manifest_sha256"] != _sha(_canonical(manifest)) or
            index["source_tar_sha256"] != _sha(raw_tar) or index["source_commit"] != manifest["source"]["commit"]):
        raise ValueError("bundle checksum or pinned source mismatch")
    _validated_tar(raw_tar)
    return manifest, index, raw_tar


def _extract(raw_tar: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
        for member in archive:
            path = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("source archive has unreadable file")
                with source, path.open("xb") as target:
                    target.write(source.read())
                path.chmod(0o755 if member.mode & 0o111 else 0o644)


def _tree_digest(root: Path) -> str:
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("installed source contains a symlink")
    files = sorted(path for path in root.rglob("*") if path.is_file())
    return _sha(_canonical([{"path": path.relative_to(root).as_posix(), "sha256": _sha(path.read_bytes())} for path in files]))


def _python(target: Path) -> Path:
    return target / "venv" / "bin" / "python"


def install(bundle_path: Path, target: Path) -> dict:
    bundle_path, target = Path(bundle_path).resolve(), Path(target).resolve()
    manifest, index, raw_tar = _read_bundle(bundle_path)
    required = tuple(map(int, manifest["python_min"].split(".")))
    if sys.version_info[:2] < required:
        raise ValueError(f"app requires Python {manifest['python_min']}+")
    if target.exists():
        raise ValueError("install target already exists; installations are never overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".a2z-app-", dir=target.parent) as temporary:
        stage = Path(temporary)
        source = stage / "source"
        source.mkdir()
        _extract(raw_tar, source)
        if not (source / "src" / Path(*manifest["entry_module"].split(".")).with_suffix(".py")).is_file():
            raise ValueError("entry module is absent from the pinned source")
        venv.EnvBuilder(with_pip=False, clear=True).create(stage / "venv")
        receipt = {"format": "a2z-app-install-v1", "app_id": manifest["id"],
                   "app_version": manifest["version"], "source_commit": index["source_commit"],
                   "manifest_sha256": _sha(_canonical(manifest)),
                   "source_tar_sha256": index["source_tar_sha256"],
                   "bundle_sha256": _sha(bundle_path.read_bytes()),
                   "source_tree_sha256": _tree_digest(source),
                   "installed_at": datetime.now(UTC).isoformat()}
        (stage / "manifest.json").write_bytes(_canonical(manifest))
        (stage / "receipt.json").write_bytes(_canonical(receipt))
        stage.rename(target)
    return receipt


def verify(target: Path) -> dict:
    target = Path(target).resolve()
    manifest = validate_manifest(json.loads((target / "manifest.json").read_text(encoding="utf-8")))
    receipt = json.loads((target / "receipt.json").read_text(encoding="utf-8"))
    if (receipt.get("format") != "a2z-app-install-v1" or receipt.get("app_id") != manifest["id"] or
            receipt.get("app_version") != manifest["version"] or
            receipt.get("manifest_sha256") != _sha(_canonical(manifest)) or
            receipt.get("source_commit") != manifest["source"]["commit"] or
            receipt.get("source_tree_sha256") != _tree_digest(target / "source") or
            not _python(target).is_file()):
        raise ValueError("installed app differs from receipt")
    return {"valid": True, "app_id": manifest["id"], "version": manifest["version"],
            "source_commit": receipt["source_commit"],
            "scope": "LOCAL_FILE_INTEGRITY_ONLY_NOT_PUBLISHER_IDENTITY_OR_CUSTOMER_DEPLOYMENT"}


def run(target: Path, args: list[str], *, env: dict[str, str] | None = None) -> int:
    verify(target)
    target = Path(target).resolve()
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    variables = dict(os.environ if env is None else env)
    variables["PYTHONPATH"] = str(target / "source" / "src")
    proc = subprocess.run([str(_python(target)), "-m", manifest["entry_module"], *args], env=variables)
    return proc.returncode
