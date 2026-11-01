"""Pure lookups over the extracted model catalog; no device/vendor code calls."""
from __future__ import annotations
import json
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("drivers.json")


def load_catalog(path=CATALOG_PATH):
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    if catalog.get("schema") != "tint-ppf-driver-catalog.v1":
        raise ValueError("Unsupported printing driver catalog")
    profiles = [profile for driver in catalog["drivers"] for profile in driver["profiles"]]
    if len(profiles) != catalog["counts"]["variant_profiles"] or len({profile["key"] for profile in profiles}) != len(profiles):
        raise ValueError("Driver catalog profile counts/keys are inconsistent")
    for driver in catalog["drivers"]:
        if len(driver["profiles"]) != driver["factory_variant_count"]:
            raise ValueError("Driver profile count differs from its extracted factory count")
    return catalog


def get_driver(driver_id, catalog=None):
    catalog = catalog or load_catalog()
    selected = Path(driver_id).stem.casefold()
    return next((driver for driver in catalog["drivers"] if driver["id"].casefold() == selected), None)


def profiles_for_driver(driver_id, catalog=None):
    driver = get_driver(driver_id, catalog)
    return list(driver["profiles"]) if driver else []


def get_profile(key, catalog=None):
    catalog = catalog or load_catalog()
    return next((profile for driver in catalog["drivers"] for profile in driver["profiles"] if profile["key"].casefold() == key.casefold()), None)


def profiles_by_model_id(driver_id, model_id, catalog=None):
    """Return every match: the original catalog contains repeated model IDs."""
    return [profile for profile in profiles_for_driver(driver_id, catalog) if profile["model_id"].casefold() == model_id.casefold()]


def search_profiles(query="", driver_id=None, catalog=None):
    catalog = catalog or load_catalog()
    profiles = profiles_for_driver(driver_id, catalog) if driver_id else [profile for driver in catalog["drivers"] for profile in driver["profiles"]]
    needle = query.casefold().strip()
    return [profile for profile in profiles if needle in profile["name"].casefold() or needle in profile["model_id"].casefold() or needle in profile["key"].casefold()]
