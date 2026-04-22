from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .bundles import bundle_checksum
from .config import AppConfig
from .external_data import ExternalDataManager
from .imports import ImportInputError, copy_into_bundle


class BundleGenerationError(RuntimeError):
    pass


def generate_map_bundle(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    style_dir_value = payload.get("style_dir") or payload.get("source_style_dir")
    if not style_dir_value:
        raise BundleGenerationError("style_dir is required")
    style_dir = Path(str(style_dir_value)).resolve()
    if not style_dir.is_dir():
        raise BundleGenerationError(f"style_dir does not exist or is not a directory: {style_dir}")

    name = str(payload.get("name") or "offline-map")
    version = str(payload.get("version") or "1")
    output = _output_path(config, payload, name, version)
    fetch_external = bool(payload.get("fetch_external_data", False))
    source_mode = str(payload.get("source_mode") or config.import_source_mode or "local").lower()
    external_data_uri = payload.get("external_data_uri")

    with tempfile.TemporaryDirectory(prefix="map-bundle-build-") as tmp:
        root = Path(tmp) / "bundle"
        bundle_style = root / "style"
        bundle_data = root / "data"
        _copy_style(style_dir, bundle_style)
        bundle_data.mkdir(parents=True, exist_ok=True)

        external_data = ExternalDataManager(config).prepare_bundle(
            bundle_style,
            source_mode=source_mode,
            external_data_uri=str(external_data_uri) if external_data_uri else None,
            fetch=fetch_external,
        )
        imports = _copy_imports(payload, bundle_data)
        manifest = _manifest(name, version, bundle_style, imports, external_data)
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        checksum = bundle_checksum(root)
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_tarball(root, output)
        archive_sha256 = _sha256(output)
        checksum_path = output.with_suffix(output.suffix + ".sha256")
        checksum_path.write_text(f"{archive_sha256}  {output.name}\n")

    return {
        "bundle": str(output),
        "checksum_file": str(checksum_path),
        "bundle_checksum": checksum,
        "archive_sha256": archive_sha256,
        "manifest": manifest,
        "external_data": external_data,
        "imports": imports,
    }


def _output_path(config: AppConfig, payload: dict[str, Any], name: str, version: str) -> Path:
    if payload.get("output"):
        return Path(str(payload["output"])).resolve()
    safe_name = _safe_name(name)
    safe_version = _safe_name(version)
    return Path(config.bundle_output_dir).resolve() / f"{safe_name}-{safe_version}.tar.gz"


def _copy_style(source: Path, target: Path) -> None:
    ignore = shutil.ignore_patterns(".git", "node_modules", ".cache", "__pycache__")
    shutil.copytree(source, target, ignore=ignore)


def _copy_imports(payload: dict[str, Any], bundle_data: Path) -> dict[str, str]:
    imports: dict[str, str] = {}
    pbf = payload.get("pbf_uri") or payload.get("pbf_path")
    if pbf:
        source = _local_source(str(pbf))
        target = bundle_data / Path(source).name
        copy_into_bundle(source, target)
        imports["pbf"] = f"data/{target.name}"
    poly = payload.get("poly_uri") or payload.get("poly_path")
    if poly:
        source = _local_source(str(poly))
        target = bundle_data / Path(source).name
        copy_into_bundle(source, target)
        imports["poly"] = f"data/{target.name}"
    return imports


def _local_source(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "file":
        return urllib.request.url2pathname(parsed.path)
    if parsed.scheme:
        raise BundleGenerationError("bundle generator PBF/poly inputs must be local paths or file:// URIs")
    return value


def _manifest(
    name: str,
    version: str,
    bundle_style: Path,
    imports: dict[str, str],
    external_data: dict[str, Any],
) -> dict[str, Any]:
    style = {
        "directory": "style",
        "mapnikXml": _first_existing(bundle_style, ["mapnik.xml"]),
    }
    for key, names in {
        "lua": ["openstreetmap-carto.lua"],
        "styleFile": ["openstreetmap-carto.style"],
        "indexesSql": ["indexes.sql"],
    }.items():
        found = _first_existing(bundle_style, names, required=False)
        if found:
            style[key] = found
    return {
        "apiVersion": "tileserver.openstreetmap.local/v1",
        "name": name,
        "version": version,
        "style": style,
        "externalData": external_data,
        "imports": imports,
        "layers": [{"name": "default"}],
    }


def _first_existing(root: Path, names: list[str], required: bool = True) -> Optional[str]:
    for name in names:
        if (root / name).is_file():
            return name
    if required:
        raise BundleGenerationError(f"style must contain one of: {', '.join(names)}")
    return None


def _write_tarball(root: Path, output: Path) -> None:
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(root.rglob("*")):
            archive.add(path, arcname=str(path.relative_to(root)))


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip("-") or "bundle"


def _sha256(path: Path) -> str:
    try:
        return _hash_file(path)
    except ImportInputError as exc:
        raise BundleGenerationError(str(exc)) from exc


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
