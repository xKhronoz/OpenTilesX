from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig, mask_secret


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tgz", ".tar.gz")


class ExternalDataError(RuntimeError):
    pass


class ExternalDataManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def prepare_bundle(
        self,
        style_dir: Path,
        source_mode: str = "local",
        external_data_uri: Optional[str] = None,
        fetch: bool = False,
    ) -> dict[str, Any]:
        config_path = style_dir / "external-data.yml"
        script_path = style_dir / "scripts" / "get-external-data.py"
        data_dir = style_dir / "data"
        result = {
            "available": config_path.is_file(),
            "mode": "none",
            "config": str(config_path) if config_path.is_file() else None,
            "offline_config": None,
            "data_dir": str(data_dir),
            "script": str(script_path) if script_path.is_file() else None,
            "sources": [],
        }
        if not config_path.is_file():
            return result

        definition = _read_yaml(config_path)
        sources = _external_sources(definition)
        data_dir.mkdir(parents=True, exist_ok=True)
        if external_data_uri:
            staged_root = self._stage_external_data_uri(
                external_data_uri,
                source_mode,
                Path(self.config.external_data_staging_path) / "bundle-preload",
                dry_run=False,
            )
            _copy_tree_contents(staged_root, data_dir)
            result["mode"] = "local-preload"
        elif fetch:
            self._fetch_sources(sources, source_mode, data_dir)
            result["mode"] = "fetch"
        elif any(data_dir.iterdir()):
            result["mode"] = "vendored"
        else:
            result["mode"] = "none"

        if result["mode"] != "none":
            offline_config = style_dir / "external-data.offline.yml"
            assets = _write_offline_config(config_path, data_dir, offline_config)
            result["offline_config"] = str(offline_config)
            result["sources"] = assets
        return result

    def load(
        self,
        payload: dict[str, Any],
        style_dir: Path,
        job_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        mode = str(payload.get("external_data_mode") or self.config.external_data_mode or "auto").lower()
        source_mode = str(payload.get("external_data_source_mode") or ("public" if mode == "fetch" else "local")).lower()
        config_path = style_dir / "external-data.yml"
        script_path = style_dir / "scripts" / "get-external-data.py"
        data_dir = style_dir / "data"
        result: dict[str, Any] = {
            "mode": mode,
            "source_mode": source_mode,
            "script": str(script_path) if script_path.is_file() else None,
            "config": str(config_path) if config_path.is_file() else None,
            "offline_config": None,
            "data_dir": str(data_dir),
            "fallback": None,
            "tables": [],
            "command": None,
            "dry_run": dry_run,
        }

        if mode == "placeholder" or not config_path.is_file():
            result["fallback"] = "placeholder"
            result["status"] = "placeholder"
            return result

        tables = [source["name"] for source in _external_sources(_read_yaml(config_path))]
        result["tables"] = tables

        if mode == "fetch":
            self._ensure_fetch_allowed(source_mode, [source["url"] for source in _external_sources(_read_yaml(config_path))])
            command = self._command(script_path, config_path, data_dir)
            result["command"] = _mask_command(command)
            if dry_run:
                result["status"] = "planned-fetch"
                return result
            completed = self._run(command)
            result.update(completed)
            result["status"] = "fetched"
            return result

        try:
            plan = self._offline_plan(payload, style_dir, job_id, dry_run=dry_run)
        except ExternalDataError:
            if mode == "auto":
                result["fallback"] = "placeholder"
                result["status"] = "placeholder"
                result["error"] = "offline external data not available"
                return result
            raise

        result["offline_config"] = str(plan["offline_config"])
        result["data_dir"] = str(plan["data_dir"])
        result["staged_paths"] = plan.get("staged_paths", [])
        command = self._command(script_path, plan["offline_config"], plan["data_dir"])
        result["command"] = _mask_command(command)
        if dry_run:
            result["status"] = "planned-local"
            return result
        completed = self._run(command)
        result.update(completed)
        result["status"] = "loaded"
        return result

    def summary(self, style_dir: Optional[Path]) -> dict[str, Any]:
        if not style_dir:
            return {"available": False, "status": "none"}
        config_path = style_dir / "external-data.yml"
        offline_path = style_dir / "external-data.offline.yml"
        data_dir = style_dir / "data"
        summary = {
            "available": config_path.is_file(),
            "config": str(config_path) if config_path.is_file() else None,
            "offline_config": str(offline_path) if offline_path.is_file() else None,
            "data_dir": str(data_dir),
            "vendored_files": 0,
            "status": "none",
        }
        if data_dir.is_dir():
            summary["vendored_files"] = sum(1 for path in data_dir.rglob("*") if path.is_file())
        if offline_path.is_file():
            summary["status"] = "offline-ready"
        elif config_path.is_file():
            summary["status"] = "fetch-only"
        return summary

    def _offline_plan(self, payload: dict[str, Any], style_dir: Path, job_id: str, dry_run: bool) -> dict[str, Any]:
        config_path = style_dir / "external-data.yml"
        data_dir = style_dir / "data"
        external_data_uri = payload.get("external_data_uri")
        if external_data_uri:
            staging_root = Path(self.config.external_data_staging_path) / job_id / "preload"
            if dry_run:
                staged_dir = staging_root
            else:
                staged_dir = self._stage_external_data_uri(
                    str(external_data_uri),
                    str(payload.get("external_data_source_mode") or "local"),
                    staging_root,
                    dry_run=False,
                )
            offline_config = Path(self.config.external_data_staging_path) / job_id / "external-data.offline.yml"
            assets = [] if dry_run else _write_offline_config(config_path, staged_dir, offline_config)
            return {
                "offline_config": offline_config,
                "data_dir": staged_dir,
                "staged_paths": assets,
            }

        offline_config = style_dir / "external-data.offline.yml"
        if offline_config.is_file():
            return {"offline_config": offline_config, "data_dir": data_dir, "staged_paths": []}
        if data_dir.is_dir() and any(data_dir.iterdir()):
            generated = Path(self.config.external_data_staging_path) / job_id / "generated-external-data.offline.yml"
            assets = [] if dry_run else _write_offline_config(config_path, data_dir, generated)
            return {"offline_config": generated, "data_dir": data_dir, "staged_paths": assets}
        raise ExternalDataError("No vendored or preloaded external data was found")

    def _command(self, script_path: Path, config_path: Path, data_dir: Path) -> list[str]:
        if not script_path.is_file():
            raise ExternalDataError(f"External data loader script does not exist: {script_path}")
        database_url = self.config.import_database_url or self.config.database_admin_url or ""
        parsed = urllib.parse.urlparse(database_url)
        if not parsed.path:
            raise ExternalDataError("IMPORT_DATABASE_URL or DATABASE_ADMIN_URL is required for external data loading")
        command = [
            "python3",
            str(script_path),
            "-c",
            str(config_path),
            "-D",
            str(data_dir),
            "-d",
            parsed.path.lstrip("/"),
        ]
        if parsed.hostname:
            command.extend(["-H", parsed.hostname])
        if parsed.port:
            command.extend(["-p", str(parsed.port)])
        if parsed.username:
            command.extend(["-U", urllib.parse.unquote(parsed.username)])
        if parsed.password:
            command.extend(["-w", urllib.parse.unquote(parsed.password)])
        render_user = urllib.parse.urlparse(self.config.render_database_url or "").username
        if render_user:
            command.extend(["-R", urllib.parse.unquote(render_user)])
        return command

    def _run(self, command: list[str]) -> dict[str, Any]:
        completed = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = {
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        }
        if completed.returncode != 0:
            raise ExternalDataError(
                f"external data load failed with status {completed.returncode}: "
                f"{result['stderr_tail'] or result['stdout_tail']}"
            )
        return result

    def _fetch_sources(self, sources: list[dict[str, str]], source_mode: str, target_dir: Path) -> None:
        self._ensure_fetch_allowed(source_mode, [source["url"] for source in sources])
        for source in sources:
            parsed = urllib.parse.urlparse(source["url"])
            filename = Path(parsed.path).name
            if not filename:
                raise ExternalDataError(f"External data source URL has no filename: {source['url']}")
            target = target_dir / filename
            if parsed.scheme in {"http", "https"}:
                urllib.request.urlretrieve(source["url"], target)
            elif parsed.scheme in {"s3", "oci"}:
                self._download_s3(parsed, target)
            elif parsed.scheme == "file":
                shutil.copy2(Path(urllib.request.url2pathname(parsed.path)), target)
            else:
                raise ExternalDataError(f"Unsupported external data source URL: {source['url']}")

    def _ensure_fetch_allowed(self, source_mode: str, urls: list[str]) -> None:
        if source_mode == "internal":
            if not self.config.allow_internal_external_data_downloads:
                raise ExternalDataError(
                    "Internal external-data downloads are disabled. Set ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS=true."
                )
            return
        if source_mode == "public":
            if not self.config.allow_public_external_data_downloads:
                raise ExternalDataError(
                    "Public external-data downloads are disabled. Set ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS=true."
                )
            return
        if source_mode != "local":
            raise ExternalDataError("external_data_source_mode must be one of local, internal, public")
        for url in urls:
            if urllib.parse.urlparse(url).scheme not in {"", "file"}:
                raise ExternalDataError(f"external_data_source_mode=local cannot fetch {url}")

    def _stage_external_data_uri(self, uri: str, source_mode: str, staging_root: Path, dry_run: bool) -> Path:
        parsed = urllib.parse.urlparse(uri)
        staging_root = staging_root.resolve()
        if dry_run:
            return staging_root
        if parsed.scheme in {"", "file"}:
            source = Path(urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else uri).resolve()
            return _materialize_source(source, staging_root)
        self._ensure_fetch_allowed(source_mode, [uri])
        staging_root.mkdir(parents=True, exist_ok=True)
        target_file = staging_root / (Path(parsed.path).name or "external-data.bundle")
        if parsed.scheme in {"http", "https"}:
            urllib.request.urlretrieve(uri, target_file)
        elif parsed.scheme in {"s3", "oci"}:
            self._download_s3(parsed, target_file)
        else:
            raise ExternalDataError(f"Unsupported external_data_uri scheme: {parsed.scheme}")
        return _materialize_source(target_file, staging_root / "materialized")

    def _download_s3(self, parsed: urllib.parse.ParseResult, target: Path) -> None:
        if not self.config.s3:
            raise ExternalDataError("S3/OCI external-data downloads require S3_* configuration")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise ExternalDataError("boto3 is required for s3:// and oci:// external-data downloads") from exc
        bucket = parsed.netloc or self.config.s3.bucket
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise ExternalDataError("S3/OCI external data URI must include bucket and key")
        options = {"s3": {"addressing_style": "path"}} if self.config.s3.force_path_style else {}
        client = boto3.client(
            "s3",
            region_name=self.config.s3.region,
            endpoint_url=self.config.s3.endpoint_url,
            aws_access_key_id=self.config.s3.access_key_id,
            aws_secret_access_key=self.config.s3.secret_access_key,
            config=Config(signature_version="s3v4", **options),
        )
        client.download_file(bucket, key, str(target))


def _materialize_source(source: Path, target_dir: Path) -> Path:
    if not source.exists():
        raise ExternalDataError(f"External data preload path does not exist: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        _copy_tree_contents(source, target_dir)
        return target_dir
    if source.name.endswith(ARCHIVE_SUFFIXES):
        shutil.unpack_archive(str(source), str(target_dir))
        return target_dir
    shutil.copy2(source, target_dir / source.name)
    return target_dir


def _copy_tree_contents(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def _write_offline_config(config_path: Path, data_dir: Path, output_path: Path) -> list[dict[str, Any]]:
    definition = _read_yaml(config_path)
    assets: list[dict[str, Any]] = []
    for name, source in (definition.get("sources") or {}).items():
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "")
        filename = Path(urllib.parse.urlparse(url).path).name
        if not filename:
            raise ExternalDataError(f"External data source has no downloadable filename: {name}")
        local_file = _find_named_file(data_dir, filename)
        if not local_file:
            raise ExternalDataError(f"Missing vendored external-data file for {name}: expected {filename}")
        source["url"] = local_file.resolve().as_uri()
        source.pop("last_modified", None)
        assets.append(
            {
                "name": name,
                "source_url": url,
                "vendored_path": str(local_file),
                "size": local_file.stat().st_size,
                "sha256": _sha256(local_file),
            }
        )
    definition.setdefault("settings", {})["data_dir"] = str(data_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_dump_yaml(definition))
    return assets


def _read_yaml(path: Path) -> dict[str, Any]:
    data = _load_yaml(path.read_text(), path)
    if not isinstance(data, dict):
        raise ExternalDataError(f"External data config must be an object: {path}")
    return data


def _external_sources(definition: dict[str, Any]) -> list[dict[str, str]]:
    result = []
    for name, source in (definition.get("sources") or {}).items():
        if isinstance(source, dict) and source.get("url"):
            result.append({"name": str(name), "url": str(source["url"])})
    return result


def _find_named_file(root: Path, filename: str) -> Optional[Path]:
    for path in root.rglob(filename):
        if path.is_file():
            return path
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mask_command(command: list[str]) -> list[str]:
    masked = list(command)
    for index, value in enumerate(masked):
        if value == "-w" and index + 1 < len(masked):
            masked[index + 1] = "***"
        if value == "-d" and index + 1 < len(masked):
            masked[index + 1] = mask_secret(f"postgresql://***@/{masked[index + 1]}").split("/")[-1]
    return masked


def _load_yaml(text: str, path: Path) -> Any:
    try:
        import yaml
    except ImportError:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExternalDataError(f"pyyaml is required to read YAML external-data config: {path}") from exc
    return yaml.safe_load(text)


def _dump_yaml(data: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError:
        return json.dumps(data, indent=2)
    return yaml.safe_dump(data, sort_keys=False)
