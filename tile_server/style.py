from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .bundles import MapBundleValidator
from .config import AppConfig


DEFAULT_STYLE_XML = "/opt/openstreetmap-carto-default/mapnik.xml"
STYLE_COPY_IGNORE = {".git", "node_modules", ".cache", "__pycache__"}
STYLE_SANITIZER_VERSION = "2"


def materialize_style_xml(config: AppConfig, bundle_uri: Optional[str] = None, layer: str = "default") -> Path:
    source = _style_source(config, bundle_uri, layer=layer)
    cache_root = Path(config.style_workdir)
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_key = _style_cache_key(source, config.render_database_url, layer)
    target_dir = cache_root / f"style-cache-{cache_key}"
    target = target_dir / f"mapnik-{layer}.xml"
    if target.is_file():
        return target

    lock_path = cache_root / f"style-cache-{cache_key}.lock"
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if target.is_file():
            return target
        temp_dir = cache_root / f"style-cache-{cache_key}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            _copy_style_assets(source.parent, temp_dir)
            temp_target = temp_dir / f"mapnik-{layer}.xml"
            shutil.copyfile(source, temp_target)
            patch_postgis_datasources(
                temp_target,
                config.render_database_url,
                statement_timeout_ms=config.render_db_statement_timeout_ms,
            )
            try:
                temp_dir.rename(target_dir)
            except FileExistsError:
                shutil.rmtree(temp_dir, ignore_errors=True)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
    return target


def _style_cache_key(source: Path, database_url: Optional[str], layer: str) -> str:
    digest = hashlib.sha256()
    digest.update(STYLE_SANITIZER_VERSION.encode("utf-8"))
    digest.update(b"\0")
    digest.update(layer.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(source.resolve()).encode("utf-8"))
    digest.update(b"\0")
    digest.update((database_url or "").encode("utf-8"))
    digest.update(b"\0")
    for path in sorted(_style_signature_paths(source.parent)):
        stat = path.stat()
        digest.update(str(path.relative_to(source.parent)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("utf-8"))
        digest.update(b":")
        digest.update(str(stat.st_size).encode("utf-8"))
        digest.update(b"\0")
        if path == source:
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()[:16]


def _style_signature_paths(source_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for path in source_dir.rglob("*"):
        if any(part.startswith(".") or part in STYLE_COPY_IGNORE for part in path.relative_to(source_dir).parts):
            continue
        if path.is_file():
            paths.append(path)
    return paths


def _copy_style_assets(source_dir: Path, target_dir: Path) -> None:
    source_dir = source_dir.resolve()
    target_dir = target_dir.resolve()
    if source_dir == target_dir:
        return
    for item in source_dir.iterdir():
        if _ignore_style_item(item):
            continue
        target = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True, ignore=_copy_ignore)
            _sanitize_svg_tree(target)
        elif item.is_file():
            shutil.copy2(item, target)
            _sanitize_svg_file(target)


def _ignore_style_item(path: Path) -> bool:
    return path.name.startswith(".") or path.name in STYLE_COPY_IGNORE


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name.startswith(".") or name in STYLE_COPY_IGNORE:
            ignored.add(name)
    return ignored


def _sanitize_svg_tree(root: Path) -> None:
    for path in root.rglob("*.svg"):
        if path.is_file():
            _sanitize_svg_file(path)


def _sanitize_svg_file(path: Path) -> None:
    if path.suffix.lower() != ".svg":
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return
    root = tree.getroot()
    changed = _normalize_svg_dimensions(root)
    changed = _remove_unused_markers(root, text) or changed
    if changed:
        tree.write(path, encoding="utf-8", xml_declaration=True)


def _normalize_svg_dimensions(root: ET.Element) -> bool:
    if _local_name(root.tag) != "svg":
        return False
    view_box = root.attrib.get("viewBox")
    if not view_box:
        return False
    try:
        _, _, width, height = [part for part in view_box.replace(",", " ").split()[:4]]
    except ValueError:
        return False
    changed = False
    if root.attrib.get("width") == "100%":
        root.set("width", width)
        changed = True
    if root.attrib.get("height") == "100%":
        root.set("height", height)
        changed = True
    return changed


def _remove_unused_markers(root: ET.Element, text: str) -> bool:
    if "<marker" not in text:
        return False
    if any(attribute in text for attribute in ("marker-start", "marker-mid", "marker-end")):
        return False
    changed = False
    for parent in list(root.iter()):
        for child in list(parent):
            if _local_name(child.tag) == "marker":
                parent.remove(child)
                changed = True
    return changed



def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _style_source(config: AppConfig, bundle_uri: Optional[str], layer: str) -> Path:
    if config.style_xml:
        path = Path(config.style_xml)
        if path.is_file():
            return path
        raise StyleError(f"STYLE_XML does not exist: {path}")

    uri = bundle_uri or config.map_bundle_uri
    if uri:
        validation = MapBundleValidator(config).validate(uri)
        if not validation.valid or not validation.root:
            raise StyleError("; ".join(validation.errors) or "Invalid map bundle")
        style = _layer_style(validation.manifest, layer) or validation.manifest.get("style") or {}
        return _style_path(validation.root, style)

    data_style = Path("/data/style/mapnik.xml")
    if data_style.is_file():
        return data_style

    default = Path(DEFAULT_STYLE_XML)
    if default.is_file():
        return default

    raise StyleError("No Mapnik XML found. Provide STYLE_XML or MAP_BUNDLE_URI with style.mapnikXml.")


def _layer_style(manifest: dict, layer: str) -> Optional[dict]:
    for entry in manifest.get("layers", []):
        if isinstance(entry, dict) and entry.get("name") == layer and isinstance(entry.get("style"), dict):
            return entry["style"]
    return None


def _style_path(root: Path, style: dict) -> Path:
    style_dir = style.get("directory", ".")
    mapnik_xml = style.get("mapnikXml") or style.get("mapnik_xml")
    if not mapnik_xml:
        raise StyleError("Map bundle does not define style.mapnikXml")
    return root / style_dir / mapnik_xml


def patch_postgis_datasources(
    xml_path: Path,
    database_url: Optional[str],
    statement_timeout_ms: int = 30000,
) -> None:
    if not database_url:
        return
    params = _postgres_params(database_url, statement_timeout_ms=statement_timeout_ms)
    if not params:
        return

    tree = ET.parse(xml_path)
    root = tree.getroot()
    changed = False
    for datasource in root.findall(".//Datasource"):
        type_param = datasource.find("./Parameter[@name='type']")
        if type_param is None or (type_param.text or "").strip() != "postgis":
            continue
        for name, value in params.items():
            param = datasource.find(f"./Parameter[@name='{name}']")
            if param is None:
                param = ET.SubElement(datasource, "Parameter", {"name": name})
            param.text = str(value)
            changed = True
    if changed:
        tree.write(xml_path, encoding="utf-8", xml_declaration=True)


def _postgres_params(database_url: str, statement_timeout_ms: int = 30000) -> dict[str, str]:
    if not (database_url.startswith("postgresql://") or database_url.startswith("postgres://")):
        return {}
    parsed = urllib.parse.urlparse(database_url)
    params = {
        "host": parsed.hostname or "",
        "dbname": parsed.path.lstrip("/"),
    }
    if parsed.port:
        params["port"] = str(parsed.port)
    if parsed.username:
        params["user"] = urllib.parse.unquote(parsed.username)
    if parsed.password:
        params["password"] = urllib.parse.unquote(parsed.password)
    query = urllib.parse.parse_qs(parsed.query)
    if "sslmode" in query:
        params["sslmode"] = query["sslmode"][0]
    existing_options = query.get("options", [""])[0].strip()
    options = [value for value in [existing_options, "-c default_transaction_read_only=on"] if value]
    options.append(f"-c statement_timeout={max(1, int(statement_timeout_ms))}")
    params["options"] = " ".join(options)
    params["application_name"] = "opentilesx-render"
    return {key: value for key, value in params.items() if value}


class StyleError(RuntimeError):
    pass
