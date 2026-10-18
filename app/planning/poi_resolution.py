"""Exact, identity-preserving selection of authoritative search results."""
from app.providers.amap.poi import poi_to_spot


def unique_named_poi(raw_pois, name, *, poi_id=None):
    matches = {}
    for raw in raw_pois:
        if raw.get("name") != name or not raw.get("id"):
            continue
        if poi_id is not None and str(raw["id"]) != str(poi_id):
            continue
        parsed = poi_to_spot(raw)
        if parsed:
            matches[str(parsed["id"])] = parsed
    return next(iter(matches.values())) if len(matches) == 1 else None


def named_poi_choices(raw_pois, name):
    return list(dict.fromkeys(
        str(item["name"]) for item in raw_pois
        if item.get("id") and item.get("name") and name in str(item["name"])
    ))[:5]
