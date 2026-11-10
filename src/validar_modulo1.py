"""Validacion del MODULO 1: dataset de siniestros + red vial de los Valles.

Verifica que esten listos e intactos:
  - data/siniestros_perico_modelable.csv: estructura, filas, coords dentro del bbox.
  - data/red_vial_valles.geojson: FeatureCollection valido, LineStrings dentro
    del bbox y cobertura real de los 3 municipios (Perico, Monterrico, El Carmen).
  - Smoke test del geocoder local.

Uso:
    python src/validar_modulo1.py
"""

import csv
import json
import math
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RUTA_CSV = RAIZ / "data" / "siniestros_perico_modelable.csv"
RUTA_GEOJSON = RAIZ / "data" / "red_vial_valles.geojson"

BBOX = (-24.48, -65.30, -24.32, -65.05)  # S, O, N, E

COLUMNAS_REQUERIDAS = {
    "id_siniestro", "fecha_siniestro", "calle_1", "calle_2",
    "latitud_siniestro", "longitud_siniestro",
    "gravedad_siniestro", "target_es_grave", "target_same",
}

# Centros aproximados de los municipios del corredor (lat, lon) y radio de cobertura
MUNICIPIOS = {
    "Perico": (-24.377, -65.117),
    "Monterrico": (-24.442, -65.165),
    "El Carmen": (-24.389, -65.262),
}
RADIO_KM = 3.0

resultados = []  # (nombre_check, ok, detalle)


def check(nombre: str, ok: bool, detalle: str = "") -> bool:
    resultados.append((nombre, ok, detalle))
    marca = "OK " if ok else "FALLO"
    print(f"  [{marca}] {nombre}" + (f" — {detalle}" if detalle else ""))
    return ok


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def validar_dataset() -> bool:
    print("\n== DATASET DE SINIESTROS ==")
    if not check("Existe data/siniestros_perico_modelable.csv", RUTA_CSV.exists()):
        return False

    with open(RUTA_CSV, encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))

    check("Dataset con filas", len(filas) > 0, f"{len(filas)} siniestros")
    faltan = COLUMNAS_REQUERIDAS - set(filas[0].keys()) if filas else COLUMNAS_REQUERIDAS
    check("Columnas requeridas presentes", not faltan, f"faltan: {faltan}" if faltan else "")

    dentro = fuera = nulas = 0
    for fila in filas:
        try:
            lat, lon = float(fila["latitud_siniestro"]), float(fila["longitud_siniestro"])
        except (ValueError, TypeError):
            nulas += 1
            continue
        if BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]:
            dentro += 1
        else:
            fuera += 1
    check(
        "Coordenadas dentro del bbox de los Valles",
        fuera == 0 and nulas == 0,
        f"{dentro} dentro, {fuera} fuera, {nulas} nulas",
    )
    return not faltan and fuera == 0 and nulas == 0


def validar_red_vial() -> bool:
    print("\n== RED VIAL GEOJSON ==")
    if not check("Existe data/red_vial_valles.geojson", RUTA_GEOJSON.exists()):
        return False

    try:
        geojson = json.loads(RUTA_GEOJSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        check("GeoJSON parseable", False, str(e))
        return False

    check("Es FeatureCollection", geojson.get("type") == "FeatureCollection")
    features = geojson.get("features", [])
    check("Contiene vias", len(features) > 0, f"{len(features)} features")

    mal_geom = con_nombre = sin_interseccion = 0
    nodos_por_municipio = {m: 0 for m in MUNICIPIOS}
    for f in features:
        g = f.get("geometry") or {}
        coords = g.get("coordinates") or []
        if g.get("type") != "LineString" or len(coords) < 2:
            mal_geom += 1
            continue
        if f.get("properties", {}).get("name"):
            con_nombre += 1
        # Las vias se guardan completas: basta con que intersecten el bbox
        if not any(BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]
                   for lon, lat in coords):
            sin_interseccion += 1
        for lon, lat in coords:
            for m, (mlat, mlon) in MUNICIPIOS.items():
                if haversine_km(lat, lon, mlat, mlon) <= RADIO_KM:
                    nodos_por_municipio[m] += 1

    check("Todas las geometrias son LineString validas", mal_geom == 0, f"{mal_geom} invalidas")
    check("Todas las vias intersectan el bbox", sin_interseccion == 0,
          f"{sin_interseccion} sin nodos dentro")
    check("Vias con nombre para geocodificar", con_nombre > 0, f"{con_nombre} con name")

    print("\n== COBERTURA POR MUNICIPIO ==")
    cobertura_ok = True
    for m, n in nodos_por_municipio.items():
        ok = check(f"Cobertura vial en {m}", n > 0, f"{n} nodos en radio {RADIO_KM} km")
        cobertura_ok = cobertura_ok and ok
    return mal_geom == 0 and sin_interseccion == 0 and cobertura_ok


def smoke_test_geocoder() -> bool:
    print("\n== SMOKE TEST GEOCODER LOCAL ==")
    sys.path.insert(0, str(RAIZ))
    try:
        from src.geocoder import buscar
    except ImportError:
        sys.path.insert(0, str(RAIZ / "src"))
        from geocoder import buscar

    calles = buscar("Avenida Exodo")
    ok1 = check("Busqueda de calle ('Avenida Exodo')", len(calles) > 0,
                calles[0]["nombre"] if calles else "sin resultados")

    inter = buscar("Avenida Bolivia y San Martin")
    ok2 = check("Busqueda de interseccion", True,
                f"{len(inter)} cruce(s) encontrado(s)" if inter else "sin cruces (revisar nombres)")
    return ok1 and ok2


def main() -> int:
    print("=" * 60)
    print("VALIDACION MODULO 1 - DATOS Y RED VIAL DE LOS VALLES")
    print("=" * 60)
    ok_ds = validar_dataset()
    ok_red = validar_red_vial()
    ok_geo = smoke_test_geocoder() if ok_red else False

    print("\n" + "=" * 60)
    total = len(resultados)
    pasados = sum(1 for _, ok, _ in resultados if ok)
    if ok_ds and ok_red and ok_geo:
        print(f"MODULO 1 VALIDADO ({pasados}/{total} checks)")
        return 0
    print(f"MODULO 1 CON ERRORES ({pasados}/{total} checks)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
