"""Descarga la red vial del corredor de los Valles (Jujuy) desde OpenStreetMap.

Cubre Perico, Monterrico y El Carmen mediante un bounding box y la Overpass API.
Genera data/red_vial_valles.geojson con un Feature por cada via (LineString con
todos sus nodos) y sus propiedades (name, highway, maxspeed, etc.).

Uso:
    python src/descargar_red_vial_valles.py
"""

import json
import sys
import time
from pathlib import Path

import requests

# Bounding box del corredor de los Valles: (sur, oeste, norte, este)
BBOX = (-24.48, -65.30, -24.32, -65.05)

HIGHWAY_REGEX = "motorway|trunk|primary|secondary|tertiary|residential"

# Endpoints de Overpass con fallback (el principal suele estar saturado)
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "red_vial_valles.geojson"

# Propiedades de OSM que se preservan (el resto de tags tambien se incluyen,
# pero estas garantizan el schema minimo pedido)
QUERY = f"""
[out:json][timeout:180];
way["highway"~"{HIGHWAY_REGEX}"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
out geom;
"""


def consultar_overpass(query: str) -> dict:
    """Intenta la consulta contra varios mirrors de Overpass con reintentos."""
    ultimo_error = None
    for endpoint in OVERPASS_ENDPOINTS:
        for intento in range(1, 4):
            try:
                print(f"Consultando {endpoint} (intento {intento})...")
                resp = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=300,
                    headers={"User-Agent": "mineria-valles-jujuy/1.0 (trabajo academico)"},
                )
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                ultimo_error = exc
                print(f"  Error: {exc}")
                time.sleep(5 * intento)
    raise RuntimeError(f"Fallaron todos los endpoints de Overpass: {ultimo_error}")


def osm_a_geojson(osm_data: dict) -> dict:
    """Convierte elementos 'way' de OSM (con 'geometry') a GeoJSON Features."""
    features = []
    for el in osm_data.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        props = {"osm_id": el["id"], "osm_type": "way"}
        props.update(el.get("tags", {}))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": props,
            }
        )
    return {
        "type": "FeatureCollection",
        "metadata": {
            "fuente": "OpenStreetMap via Overpass API",
            "bbox": {"sur": BBOX[0], "oeste": BBOX[1], "norte": BBOX[2], "este": BBOX[3]},
            "municipios": ["Perico", "Monterrico", "El Carmen"],
        },
        "features": features,
    }


def main() -> int:
    print("=" * 60)
    print("DESCARGA DE RED VIAL - VALLES DE JUJUY")
    print(f"BBox (S,O,N,E): {BBOX}")
    print(f"Filtro: highway ~ {HIGHWAY_REGEX}")
    print("=" * 60)

    osm_data = consultar_overpass(QUERY)
    geojson = osm_a_geojson(osm_data)
    n = len(geojson["features"])

    if n == 0:
        print("ERROR: la consulta no devolvio vias.", file=sys.stderr)
        return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")

    con_nombre = sum(1 for f in geojson["features"] if f["properties"].get("name"))
    print(f"\nOK: {n} vias descargadas ({con_nombre} con nombre).")
    print(f"Archivo: {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
