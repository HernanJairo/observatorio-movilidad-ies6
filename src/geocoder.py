"""Geocoder para el corredor de los Valles (Perico, Monterrico, El Carmen).

Permite buscar calles e intersecciones por texto:
  - En la red vial local (data/red_vial_valles.geojson), offline y sin limites.
  - En la API abierta de Nominatim (OpenStreetMap), acotada a Jujuy.

Uso:
    from src.geocoder import buscar, buscar_interseccion, buscar_nominatim

    buscar("Avenida Exodo")
    buscar("Avenida Bolivia y Avenida San Martin")
    buscar_interseccion("BOLIVIA", "SAN MARTIN")
    buscar_nominatim("Terminal de omnibus Perico")
"""

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import requests

RUTA_GEOJSON = Path(__file__).resolve().parent.parent / "data" / "red_vial_valles.geojson"

# Mismo bounding box del corredor de los Valles, en formato Nominatim
# (min_lon, min_lat, max_lon, max_lat)
VIEWBOX_JUJUY = (-65.30, -24.48, -65.05, -24.32)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {"User-Agent": "mineria-valles-jujuy/1.0 (trabajo academico)"}

# Separadores tipicos de intersecciones en el dataset de siniestros
_SEPARADORES = re.compile(r"\s+(?:y|e|esq\.?|esquina|cruce|c/)\s+|\s*&\s*", re.IGNORECASE)


def normalizar(texto: str) -> str:
    """Mayusculas, sin tildes, sin espacios redundantes."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.upper().split())


def _similitud(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def cargar_red(geojson_path: Path = RUTA_GEOJSON) -> list[dict]:
    """Carga las features del GeoJSON local (cachea en el proceso)."""
    if not hasattr(cargar_red, "_cache"):
        with open(geojson_path, encoding="utf-8") as f:
            cargar_red._cache = json.load(f)["features"]
    return cargar_red._cache


def _punto_representativo(feature: dict) -> tuple[float, float]:
    """Punto medio de la polilinea de la via: (lat, lon)."""
    coords = feature["geometry"]["coordinates"]
    lon, lat = coords[len(coords) // 2]
    return lat, lon


def buscar_calle(texto: str, umbral: float = 0.6) -> list[dict]:
    """Busca vias por nombre en la red local. Devuelve coincidencias ordenadas
    por similitud con lat/lon del punto medio de la calle."""
    objetivo = normalizar(texto)
    resultados = []
    for feature in cargar_red():
        nombre = feature["properties"].get("name")
        if not nombre:
            continue
        nombre_norm = normalizar(nombre)
        if objetivo in nombre_norm or nombre_norm in objetivo:
            score = 1.0
        else:
            score = _similitud(objetivo, nombre_norm)
        if score >= umbral:
            lat, lon = _punto_representativo(feature)
            resultados.append(
                {
                    "nombre": nombre,
                    "highway": feature["properties"].get("highway"),
                    "lat": lat,
                    "lon": lon,
                    "score": round(score, 3),
                    "fuente": "local",
                }
            )
    resultados.sort(key=lambda r: r["score"], reverse=True)
    # Deduplicar por nombre (una calle puede estar en varios segmentos)
    vistos, dedup = set(), []
    for r in resultados:
        if r["nombre"] not in vistos:
            vistos.add(r["nombre"])
            dedup.append(r)
    return dedup


def buscar_interseccion(calle_1: str, calle_2: str, umbral: float = 0.6) -> list[dict]:
    """Encuentra los cruces reales entre dos calles: nodos compartidos entre
    los segmentos de cada via (o extremos a < 20 m)."""
    n1, n2 = normalizar(calle_1), normalizar(calle_2)

    def features_de(objetivo: str) -> list[dict]:
        return [
            f
            for f in cargar_red()
            if f["properties"].get("name")
            and _similitud(objetivo, normalizar(f["properties"]["name"])) >= umbral
        ]

    seg1, seg2 = features_de(n1), features_de(n2)
    if not seg1 or not seg2:
        return []

    nodos2 = {tuple(c) for f in seg2 for c in f["geometry"]["coordinates"]}
    cruces = set()
    for f in seg1:
        for lon, lat in f["geometry"]["coordinates"]:
            if (lon, lat) in nodos2:
                cruces.add((lat, lon))

    # Fallback: extremos cercanos si no comparten nodos exactos (~20 m)
    if not cruces:
        for f1 in seg1:
            for lon1, lat1 in f1["geometry"]["coordinates"]:
                for lon2, lat2 in nodos2:
                    if abs(lat1 - lat2) < 0.0002 and abs(lon1 - lon2) < 0.0002:
                        cruces.add((lat1, lon1))
                        break

    return [
        {
            "nombre": f"{seg1[0]['properties']['name']} y {seg2[0]['properties']['name']}",
            "lat": lat,
            "lon": lon,
            "score": 1.0,
            "fuente": "local-interseccion",
        }
        for lat, lon in sorted(cruces)
    ]


def buscar(texto: str) -> list[dict]:
    """Busqueda unificada: detecta intersecciones ("A y B", "A & B", "A esq. B")
    y delega en buscar_interseccion; si no, busca la calle por nombre."""
    partes = _SEPARADORES.split(texto.strip())
    if len(partes) == 2 and all(partes):
        return buscar_interseccion(partes[0], partes[1])
    return buscar_calle(texto)


def buscar_nominatim(texto: str, limite: int = 5) -> list[dict]:
    """Busca en Nominatim acotado al corredor de los Valles (Jujuy, AR)."""
    params = {
        "q": texto,
        "format": "jsonv2",
        "limit": limite,
        "countrycodes": "ar",
        "viewbox": ",".join(map(str, VIEWBOX_JUJUY)),
        "bounded": 1,
        "addressdetails": 1,
    }
    resp = requests.get(NOMINATIM_URL, params=params, headers=NOMINATIM_HEADERS, timeout=20)
    resp.raise_for_status()
    return [
        {
            "nombre": item.get("display_name"),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "tipo": item.get("type"),
            "score": float(item.get("importance", 0)),
            "fuente": "nominatim",
        }
        for item in resp.json()
    ]


if __name__ == "__main__":
    consultas = [
        "Avenida Exodo",
        "Avenida Bolivia y San Martin",
    ]
    for q in consultas:
        print(f"\n>>> buscar('{q}')")
        for r in buscar(q)[:5]:
            print(f"  {r}")
    print("\n>>> buscar_nominatim('Municipalidad de Perico, Jujuy')")
    for r in buscar_nominatim("Municipalidad de Perico, Jujuy"):
        print(f"  {r}")
