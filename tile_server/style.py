from __future__ import annotations

import shutil
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .bundles import MapBundleValidator
from .config import AppConfig


DEFAULT_STYLE_XML = "/opt/openstreetmap-carto-default/mapnik.xml"


def materialize_style_xml(config: AppConfig, bundle_uri: Optional[str] = None, layer: str = "default") -> Path:
    source = _style_source(config, bundle_uri, layer=layer)
    target_dir = Path(config.style_workdir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"mapnik-{layer}.xml"
    shutil.copyfile(source, target)
    patch_postgis_datasources(target, config.render_database_url)
    return target


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


def patch_postgis_datasources(xml_path: Path, database_url: Optional[str]) -> None:
    if not database_url:
        return
    params = _postgres_params(database_url)
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


def _postgres_params(database_url: str) -> dict[str, str]:
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
    return {key: value for key, value in params.items() if value}


class StyleError(RuntimeError):
    pass
