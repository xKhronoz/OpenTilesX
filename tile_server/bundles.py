from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig


MANIFEST_NAMES = ("map-bundle.yaml", "map-bundle.yml", "manifest.yaml", "manifest.yml", "manifest.json")


@dataclass
class BundleValidation:
    uri: str
    root: Optional[Path]
    manifest_path: Optional[Path]
    manifest: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "root": str(self.root) if self.root else None,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "manifest": self.manifest,
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class BundleResolver:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def resolve_to_path(self, uri: str) -> Path:
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme in {"", "file"}:
            return Path(urllib.request.url2pathname(parsed.path if parsed.scheme else uri)).resolve()
        if parsed.scheme in {"http", "https"}:
            if not self.config.allow_network_fetch:
                raise BundleError("HTTP bundle fetch is disabled. Set ALLOW_NETWORK_FETCH=true for internal mirrors.")
            return self._download(uri, suffix=Path(parsed.path).suffix or ".bundle")
        if parsed.scheme in {"s3", "oci"}:
            return self._download_s3(parsed)
        raise BundleError(f"Unsupported MAP_BUNDLE_URI scheme: {parsed.scheme}")

    def _download(self, uri: str, suffix: str) -> Path:
        fd, target = tempfile.mkstemp(prefix="map-bundle-", suffix=suffix)
        os.close(fd)
        urllib.request.urlretrieve(uri, target)
        return Path(target)

    def _download_s3(self, parsed: urllib.parse.ParseResult) -> Path:
        if not self.config.s3:
            raise BundleError("S3/OCI bundle URIs require S3_* configuration")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise BundleError("boto3 is required for s3:// and oci:// bundle URIs") from exc

        bucket = parsed.netloc or self.config.s3.bucket
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise BundleError("S3/OCI bundle URI must include bucket and key")

        options = {"s3": {"addressing_style": "path"}} if self.config.s3.force_path_style else {}
        client = boto3.client(
            "s3",
            region_name=self.config.s3.region,
            endpoint_url=self.config.s3.endpoint_url,
            aws_access_key_id=self.config.s3.access_key_id,
            aws_secret_access_key=self.config.s3.secret_access_key,
            config=Config(signature_version="s3v4", **options),
        )
        fd, target = tempfile.mkstemp(prefix="map-bundle-", suffix=Path(key).suffix or ".bundle")
        os.close(fd)
        client.download_file(bucket, key, target)
        return Path(target)


class MapBundleValidator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.resolver = BundleResolver(config)

    def validate(self, uri: str) -> BundleValidation:
        try:
            path = self.resolver.resolve_to_path(uri)
        except Exception as exc:
            return BundleValidation(uri=uri, root=None, manifest_path=None, errors=[str(exc)])

        root = self._materialize(path)
        result = BundleValidation(uri=uri, root=root, manifest_path=None)
        manifest_path = self._find_manifest(root)
        result.manifest_path = manifest_path
        if not manifest_path:
            result.errors.append("Map bundle must contain map-bundle.yaml, manifest.yaml, or manifest.json")
            return result

        try:
            manifest = self._load_manifest(manifest_path)
        except Exception as exc:
            result.errors.append(f"Unable to parse bundle manifest: {exc}")
            return result

        result.manifest = manifest
        self._validate_manifest(root, manifest, result)
        return result

    def _materialize(self, path: Path) -> Path:
        if path.is_dir():
            return path
        if path.suffix in {".tar", ".gz", ".tgz", ".zip"}:
            target = Path(tempfile.mkdtemp(prefix="map-bundle-"))
            shutil.unpack_archive(str(path), str(target))
            return target
        raise BundleError(f"Map bundle path is not a directory or supported archive: {path}")

    def _find_manifest(self, root: Path) -> Optional[Path]:
        for name in MANIFEST_NAMES:
            candidate = root / name
            if candidate.is_file():
                return candidate
        return None

    def _load_manifest(self, path: Path) -> dict[str, Any]:
        if path.suffix == ".json":
            return json.loads(path.read_text())
        try:
            import yaml
        except ImportError as exc:
            raise BundleError("pyyaml is required to read YAML map bundles") from exc
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise BundleError("Bundle manifest must be an object")
        return data

    def _validate_manifest(self, root: Path, manifest: dict[str, Any], result: BundleValidation) -> None:
        if not manifest.get("apiVersion"):
            result.warnings.append("apiVersion is not set")
        if not manifest.get("name") and not manifest.get("metadata", {}).get("name"):
            result.errors.append("Bundle manifest must define name or metadata.name")
        if not manifest.get("version") and not manifest.get("metadata", {}).get("version"):
            result.errors.append("Bundle manifest must define version or metadata.version")

        style = manifest.get("style") or {}
        if not isinstance(style, dict):
            result.errors.append("style must be an object")
            return

        mapnik_xml = style.get("mapnikXml") or style.get("mapnik_xml")
        style_dir = style.get("directory", ".")
        if mapnik_xml:
            if not (root / style_dir / mapnik_xml).is_file():
                result.errors.append(f"style.mapnikXml does not exist: {style_dir}/{mapnik_xml}")
        else:
            result.errors.append("Bundle must include a prebuilt style.mapnikXml for offline runtime")

        for key in ("lua", "styleFile", "indexesSql"):
            value = style.get(key)
            if value and not (root / style_dir / value).is_file():
                result.errors.append(f"style.{key} does not exist: {style_dir}/{value}")

        for layer in manifest.get("layers", [{"name": "default"}]):
            if not isinstance(layer, dict) or not layer.get("name"):
                result.errors.append("Each layer entry must be an object with a name")
                continue
            layer_style = layer.get("style")
            if isinstance(layer_style, dict):
                layer_dir = layer_style.get("directory", ".")
                layer_xml = layer_style.get("mapnikXml") or layer_style.get("mapnik_xml")
                if not layer_xml:
                    result.errors.append(f"layers.{layer.get('name')}.style.mapnikXml is required")
                elif not (root / layer_dir / layer_xml).is_file():
                    result.errors.append(f"layers.{layer.get('name')}.style.mapnikXml does not exist: {layer_dir}/{layer_xml}")

        imports = manifest.get("imports") or {}
        if isinstance(imports, dict):
            for key in ("pbf", "poly"):
                value = imports.get(key)
                if value and not (root / value).is_file():
                    result.errors.append(f"imports.{key} does not exist: {value}")


def bundle_checksum(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


class BundleError(RuntimeError):
    pass
