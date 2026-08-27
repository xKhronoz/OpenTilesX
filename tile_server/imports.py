from __future__ import annotations

import hashlib
import shutil
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig

try:
    import boto3
    from botocore.config import Config as BotocoreConfig
except ImportError as exc:
    boto3 = None
    BotocoreConfig = None
    BOTO3_IMPORT_ERROR = exc
else:
    BOTO3_IMPORT_ERROR = None


@dataclass(frozen=True)
class ResolvedInput:
    source_uri: str
    path: str
    source_mode: str
    size: Optional[int]
    sha256: Optional[str]
    staged: bool
    downloaded: bool
    planned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_uri": self.source_uri,
            "path": self.path,
            "source_mode": self.source_mode,
            "size": self.size,
            "sha256": self.sha256,
            "staged": self.staged,
            "downloaded": self.downloaded,
            "planned": self.planned,
        }


class ImportInputResolver:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def resolve_payload(self, payload: dict[str, Any], job_id: str, dry_run: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        mode = (payload.get("source_mode") or self.config.import_source_mode or "local").strip().lower()
        if mode not in {"local", "internal", "public"}:
            raise ImportInputError("source_mode must be one of local, internal, public")

        resolved_payload = dict(payload)
        result: dict[str, Any] = {"source_mode": mode, "inputs": {}}

        pbf = payload.get("pbf_uri") or payload.get("pbf_path")
        if pbf:
            resolved = self.resolve(str(pbf), mode, job_id, "pbf", payload.get("expected_sha256"), dry_run=dry_run)
            resolved_payload["pbf_uri"] = resolved.path
            result["inputs"]["pbf"] = resolved.to_dict()

        poly = payload.get("poly_uri") or payload.get("poly_path")
        if poly:
            resolved = self.resolve(str(poly), mode, job_id, "poly", payload.get("poly_expected_sha256"), dry_run=dry_run)
            resolved_payload["poly_uri"] = resolved.path
            result["inputs"]["poly"] = resolved.to_dict()

        return resolved_payload, result

    def resolve(
        self,
        uri: str,
        mode: str,
        job_id: str,
        label: str,
        expected_sha256: Optional[str] = None,
        dry_run: bool = False,
    ) -> ResolvedInput:
        parsed = urllib.parse.urlparse(uri)
        scheme = parsed.scheme
        if scheme in {"", "file"}:
            path = self._local_path(uri, parsed)
            if dry_run and not path.is_file():
                return ResolvedInput(uri, str(path), mode, None, None, staged=False, downloaded=False, planned=True)
            size, digest = _file_metadata(path)
            _verify_checksum(label, digest, expected_sha256)
            return ResolvedInput(uri, str(path), mode, size, digest, staged=False, downloaded=False)

        self._check_download_allowed(mode, scheme, uri)
        target = self._target_path(uri, job_id, label)
        if dry_run:
            return ResolvedInput(uri, str(target), mode, None, None, staged=True, downloaded=True, planned=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        if scheme in {"http", "https"}:
            urllib.request.urlretrieve(uri, target)
        elif scheme in {"s3", "oci"}:
            self._download_s3(parsed, target)
        else:
            raise ImportInputError(f"Unsupported import input scheme: {scheme}")

        size, digest = _file_metadata(target)
        _verify_checksum(label, digest, expected_sha256)
        return ResolvedInput(uri, str(target), mode, size, digest, staged=True, downloaded=True)

    def _check_download_allowed(self, mode: str, scheme: str, uri: str) -> None:
        if mode == "local":
            raise ImportInputError(f"source_mode=local only accepts mounted paths or file:// URIs: {uri}")
        if mode == "internal":
            if scheme not in {"http", "https", "s3", "oci"}:
                raise ImportInputError(f"source_mode=internal does not support {scheme}:// inputs")
            if not self.config.allow_internal_import_downloads:
                raise ImportInputError(
                    "Internal import downloads are disabled. Set ALLOW_INTERNAL_IMPORT_DOWNLOADS=true."
                )
            return
        if mode == "public":
            if scheme not in {"http", "https"}:
                raise ImportInputError("source_mode=public only supports explicit HTTP/S inputs")
            if not self.config.allow_public_import_downloads:
                raise ImportInputError("Public import downloads are disabled. Set ALLOW_PUBLIC_IMPORT_DOWNLOADS=true.")
            return
        raise ImportInputError("source_mode must be one of local, internal, public")

    def _target_path(self, uri: str, job_id: str, label: str) -> Path:
        parsed = urllib.parse.urlparse(uri)
        suffix = "".join(Path(parsed.path).suffixes) or ".input"
        safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in label)
        return Path(self.config.import_staging_path) / job_id / f"{safe_label}{suffix}"

    def _local_path(self, uri: str, parsed: urllib.parse.ParseResult) -> Path:
        path = urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else uri
        return Path(path).resolve()

    def _download_s3(self, parsed: urllib.parse.ParseResult, target: Path) -> None:
        if not self.config.s3:
            raise ImportInputError("S3/OCI import inputs require S3_* configuration")
        if boto3 is None or BotocoreConfig is None:
            raise ImportInputError("boto3 is required for s3:// and oci:// import inputs") from BOTO3_IMPORT_ERROR

        bucket = parsed.netloc or self.config.s3.bucket
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise ImportInputError("S3/OCI import input URI must include bucket and key")
        options = {"s3": {"addressing_style": "path"}} if self.config.s3.force_path_style else {}
        client = boto3.client(
            "s3",
            region_name=self.config.s3.region,
            endpoint_url=self.config.s3.endpoint_url,
            aws_access_key_id=self.config.s3.access_key_id,
            aws_secret_access_key=self.config.s3.secret_access_key,
            config=BotocoreConfig(signature_version="s3v4", **options),
        )
        client.download_file(bucket, key, str(target))


class ImportInputError(RuntimeError):
    pass


def copy_into_bundle(source: str, target: Path) -> dict[str, Any]:
    path = Path(source).resolve()
    if not path.is_file():
        raise ImportInputError(f"Bundle input does not exist or is not readable: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    size, digest = _file_metadata(target)
    return {"path": str(target), "size": size, "sha256": digest}


def _file_metadata(path: Path) -> tuple[int, str]:
    if not path.is_file():
        raise ImportInputError(f"Import input does not exist or is not readable: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def _verify_checksum(label: str, actual: str, expected: Optional[str]) -> None:
    if expected and actual.lower() != expected.lower():
        raise ImportInputError(f"{label} checksum mismatch: expected {expected}, got {actual}")
