"""Servidor ligero de datos - MODULO 3 (Reporte Ciudadano + Visualizador).

Endpoints:
    GET /                       -> index.html
    GET /api/datos              -> siniestros filtrados + kpis + ranking_arterias
    GET /api/geometria-calle    -> geometria exacta (MultiLineString) de una arteria
    POST /api/reportar          -> persiste un reporte ciudadano en el CSV

Parametros de /api/datos (todos opcionales):
    jurisdiccion   Perico | Monterrico | El Carmen | San Salvador de Jujuy
                   (defecto: Perico)
    modo           MOTO | AUTO | CAMIONETA | BICICLETA | PEATON | ... | Todos
    franja_horaria "desde-hasta" en horas (p.ej. 6-12) o bloque:
                   madrugada | manana | tarde | noche | todas
    dia_semana     Lunes..Domingo (o numero 0-6) | Todos
"""

import csv
import html
import json
import math
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import random
import re
import sys
import threading
import traceback
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path

import joblib
import pandas as pd
import requests
from flask import Flask, jsonify, request, send_file

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "src"))
from geocoder import (  # noqa: E402
    buscar as geo_buscar,
    buscar_nominatim as geo_nominatim,
    cruzar_calles as geo_cruzar,
    sugerir_palpala as geo_palpala,
    sugerir_sanpedro as geo_sanpedro,
    limpiar_calle,
    normalizar,
)

RUTA_CSV = RAIZ / "data" / "siniestros_perico_modelable.csv"
RUTA_GEOJSON = RAIZ / "data" / "red_vial_valles.geojson"
RUTA_INDEX = RAIZ / "index.html"

app = Flask(__name__)

CENTROS_MUNICIPIOS = {
    "PERICO": (-24.377, -65.117),
    "MONTERRICO": (-24.438, -65.161),
    "EL CARMEN": (-24.388, -65.259),
    "PALPALA": (-24.256, -65.212),
    "SAN PEDRO DE JUJUY": (-24.233, -64.866),
    "SAN SALVADOR DE JUJUY": (-24.185, -65.299),
}

# Arterias y puntos criticos reales por jurisdiccion: el copiloto los cita
# tal cual (sin alucinar vias) y el fallback por reglas los usa al
# responder por localidad.
PUNTOS_CRITICOS = {
    "PERICO": [
        "Av. Belgrano", "Av. Éxodo", "Av. Leandro N. Alem",
        "Av. San Martín", "Av. José Hernández", "Terminal de Ómnibus",
        "Rotonda Belgrano",
    ],
    "MONTERRICO": [
        "Cruce RP 42 y RP 43", "Av. San Martín", "Zona El Labrador",
        "Acceso San Antonio",
    ],
    "EL CARMEN": [
        "Empalme RN 9 y RP 42", "Av. Centenario",
        "Acceso Dique La Ciénaga", "RP 47",
    ],
    "SAN SALVADOR DE JUJUY": [
        "Av. 19 de Abril y Canónigo Gorriti", "Av. Fascio",
        "Rotonda Acceso Sur RN 66", "Av. Éxodo",
    ],
}

BLOQUES_HORARIOS = {
    "MADRUGADA": (0, 6),
    "MANANA": (6, 12),
    "TARDE": (12, 19),
    "NOCHE": (19, 24),
}


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _jurisdiccion_de(lat, lon):
    return min(CENTROS_MUNICIPIOS, key=lambda m: _haversine_km(lat, lon, *CENTROS_MUNICIPIOS[m]))


def _cargar_siniestros():
    filas = []
    with open(RUTA_CSV, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        columnas = lector.fieldnames
        for fila in lector:
            try:
                fila["_lat"] = float(fila["latitud_siniestro"])
                fila["_lon"] = float(fila["longitud_siniestro"])
                fila["_hora"] = int(fila["hora_num"])
            except (ValueError, TypeError, KeyError):
                continue
            fila["_jur"] = _jurisdiccion_de(fila["_lat"], fila["_lon"])
            fila["_modo"] = normalizar(fila["modo_desplazamiento_victima"])
            fila["_dia"] = normalizar(fila["dia_semana_nombre"])
            filas.append(fila)
    return columnas, filas


def _cargar_red():
    with open(RUTA_GEOJSON, encoding="utf-8") as f:
        return json.load(f)["features"]


COLUMNAS, SINIESTROS = _cargar_siniestros()
RED_VIAL = _cargar_red()

# Trazabilidad de prensa: cada siniestro incorporado desde el monitor
# conserva el enlace a la fuente original, el medio y el resumen.
COLUMNAS_TRAZA = ("url_noticia", "medio_prensa", "resumen_contexto")


def _asegurar_columnas_traza():
    """Agrega al CSV las columnas de trazabilidad si faltan."""
    faltan = [c for c in COLUMNAS_TRAZA if c not in COLUMNAS]
    if not faltan:
        return
    COLUMNAS.extend(faltan)
    with open(RUTA_CSV, encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))
    with open(RUTA_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        w.writeheader()
        for fila in filas:
            w.writerow({c: (fila.get(c) or "") for c in COLUMNAS})


_asegurar_columnas_traza()


def _parsear_franja(valor):
    """Devuelve (desde, hasta) en horas o None si no aplica."""
    if not valor or normalizar(valor) in ("TODAS", "TODOS"):
        return None
    v = normalizar(valor)
    if v in BLOQUES_HORARIOS:
        return BLOQUES_HORARIOS[v]
    try:
        desde, hasta = v.replace(" ", "").split("-")
        return int(desde), int(hasta)
    except ValueError:
        return None


def _en_franja(hora, franja):
    if franja is None:
        return True
    desde, hasta = franja
    if hasta > desde:
        return desde <= hora < hasta
    return hora >= desde or hora < hasta  # franja que cruza medianoche


@app.get("/")
def index():
    return send_file(RUTA_INDEX)


# Centro del corredor interurbano (Perico <-> San Salvador por RN 66/RP 42):
# vista de camara para "Toda la Region" y "Solo Rutas y Accesos".
CENTRO_REGION = (-24.29, -65.19)
JURS_AMPLIAS = ("TODOS", "RUTAS")


def _es_siniestro_ruta(s):
    """True si el siniestro ocurrio sobre un corredor vial provincial o
    nacional (rutas, accesos, empalmes) y no en el casco urbano."""
    return (
        normalizar(s.get("tipo_de_via")).startswith("RUTA")
        or bool(re.search(
            r"\b(RUTA|RN|RP|ACCESO|EMPALME|NACIONAL|PROVINCIAL)\b",
            normalizar(s.get("calle_1") or "")))
    )


def _subset_jurisdiccion(jur):
    """Siniestros del ambito elegido: TODOS | RUTAS | una ciudad."""
    if jur == "RUTAS":
        return [s for s in SINIESTROS if _es_siniestro_ruta(s)]
    if jur in CENTROS_MUNICIPIOS:
        return [s for s in SINIESTROS if s["_jur"] == jur]
    return list(SINIESTROS)  # TODOS / sin filtro


def _jurisdiccion_param():
    """Jurisdiccion del selector: TODOS | RUTAS | una ciudad del corredor."""
    jur = normalizar(request.args.get("jurisdiccion", ""))
    if jur in ("", "TODOS", "TODAS", "TODA LA REGION", "REGION", "TODO JUJUY"):
        return "TODOS"
    if jur == "RUTAS" or "SOLO RUTAS" in jur:
        return "RUTAS"
    return jur if jur in CENTROS_MUNICIPIOS else "PERICO"


def _centro_busqueda(jur):
    """(lat, lon, radio_km) para acotar busquedas al ambito activo."""
    centro = CENTROS_MUNICIPIOS.get(jur)
    if centro is None:
        return CENTRO_REGION[0], CENTRO_REGION[1], 45.0
    return centro[0], centro[1], RADIO_JURISDICCION_KM


# Bounding boxes estrictos (min_lon, min_lat, max_lon, max_lat): el
# geocoder y Nominatim solo pueden resolver dentro del poligono de la
# jurisdiccion elegida — una calle de Perico jamas resuelve en otra zona.
BBOX_JURISDICCION = {
    "PERICO": (-65.19, -24.43, -65.04, -24.32),
    "MONTERRICO": (-65.23, -24.50, -65.10, -24.38),
    "EL CARMEN": (-65.34, -24.45, -65.18, -24.33),
    "PALPALA": (-65.24, -24.29, -65.18, -24.23),
    "SAN PEDRO DE JUJUY": (-64.90, -24.26, -64.83, -24.21),
    "SAN SALVADOR DE JUJUY": (-65.42, -24.28, -65.18, -24.10),
    "TODOS": (-65.45, -24.52, -64.80, -24.08),
    "RUTAS": (-65.45, -24.52, -64.80, -24.08),
}

_TIPOS_VIA_NOMINATIM = {
    "road", "residential", "tertiary", "secondary", "primary", "trunk",
    "unclassified", "living_street", "service", "pedestrian", "cycleway",
    "footway", "track", "path", "steps",
}


def _jurisdiccion_query(valor):
    """Normaliza el parametro jurisdiccion de los buscadores (tambien
    acepta nombres de ciudad: 'Perico' -> 'PERICO')."""
    jur = normalizar(valor or "")
    if jur in BBOX_JURISDICCION:
        return jur
    ciudad = _norm_ciudad(jur)
    return normalizar(ciudad) if ciudad else "TODOS"


@app.get("/api/datos")
def api_datos():
    jurisdiccion = _jurisdiccion_param()
    modo = normalizar(request.args.get("modo", ""))
    dia = normalizar(request.args.get("dia_semana", ""))
    franja = _parsear_franja(request.args.get("franja_horaria", ""))

    filtrados = [
        s
        for s in _subset_jurisdiccion(jurisdiccion)
        if (modo in ("", "TODOS", "TODAS") or s["_modo"] == modo)
        and (dia in ("", "TODOS", "TODAS") or s["_dia"] == dia
             or s["dia_semana_num"] == dia)
        and _en_franja(s["_hora"], franja)
    ]

    total = len(filtrados)
    graves = sum(1 for s in filtrados if normalizar(s["gravedad_siniestro"]) == "GRAVE")
    motos = sum(1 for s in filtrados if s["_modo"] == "MOTO")

    ranking = {}
    for s in filtrados:
        clave = (s["calle_1"] or "SIN DATOS").strip().upper()
        ranking[clave] = ranking.get(clave, 0) + 1
    ranking_arterias = [
        {"calle": c, "siniestros": n}
        for c, n in sorted(ranking.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return jsonify(
        {
            "siniestros": [
                [
                    s["_lat"], s["_lon"], s["gravedad_siniestro"],
                    s["modo_desplazamiento_victima"], s["calle_1"], s["calle_2"],
                    s["fecha_siniestro"], s["hora_siniestro"],
                ]
                for s in filtrados
            ],
            "kpis": {
                "total": total,
                "pct_grave": round(100 * graves / total, 1) if total else 0.0,
                "pct_moto": round(100 * motos / total, 1) if total else 0.0,
            },
            "ranking_arterias": ranking_arterias[:25],
        }
    )


RADIO_JURISDICCION_KM = 5.0

PREFIJOS_VIA = {
    "AVENIDA", "AV", "AVDA", "CALLE", "RUTA", "PROVINCIAL", "NACIONAL",
    "RN", "RP", "PASAJE", "BOULEVARD", "BV", "DIAGONAL", "ACCESO", "COLECTORA",
}
TOKENS_RUTA = {"RUTA", "NACIONAL", "PROVINCIAL", "RN", "RP", "ACCESO"}


def _segmento_en_jurisdiccion(coords, jurisdiccion):
    """True si algun nodo del segmento cae dentro del radio del centro municipal."""
    centro = CENTROS_MUNICIPIOS.get(jurisdiccion)
    if centro is None:
        return True
    return any(
        _haversine_km(lat, lon, centro[0], centro[1]) <= RADIO_JURISDICCION_KM
        for lon, lat in coords
    )


def _raiz_nombre(texto):
    """Quita prefijos de tipo de via: 'AVENIDA JOSE HERNANDEZ' -> 'JOSE HERNANDEZ'."""
    limpio = normalizar(texto).replace(".", " ").replace("-", " ")
    return " ".join(t for t in limpio.split() if t not in PREFIJOS_VIA)


def _digitos(texto):
    return re.sub(r"\D", "", texto or "")


def _score_feature(feat, objetivo, raiz):
    """Puntaje de coincidencia entre la consulta y una via de OSM (name/ref)."""
    tags = feat["properties"] or {}
    n_name = normalizar(tags.get("name") or "").replace(".", " ")
    n_ref = normalizar(tags.get("ref") or "").replace(".", " ")
    if not n_name and not n_ref:
        return 0.0

    tokens_name = set(n_name.split())
    tokens_raiz = set(raiz.split())

    # Coincidencia por ref de ruta (RN66, RP 35, etc.)
    raiz_nums = _digitos(raiz)
    ref_nums = _digitos(n_ref)
    if raiz_nums and ref_nums and raiz_nums == ref_nums:
        return 1.0
    if raiz_nums and n_ref and raiz_nums in ref_nums:
        return 0.95

    if not n_name:
        return 0.0

    # Raiz o nombre completo contenidos en el nombre de la via
    if raiz and raiz in n_name:
        return 1.0
    if objetivo and (objetivo in n_name or n_name in objetivo):
        return 0.95

    # Todos los tokens de la raiz presentes en el nombre
    if tokens_raiz and tokens_raiz <= tokens_name:
        if raiz.replace(" ", "").isdigit():
            return 0.95 if tokens_name & TOKENS_RUTA else 0.0
        return 0.9

    # Rutas: nombre tipo 'Ruta Nacional 66' sin ref
    if raiz_nums and tokens_name & TOKENS_RUTA and raiz_nums in _digitos(n_name):
        return 0.9

    # Fuzzy para variantes/typos ('ALEM' vs 'LEANDRO N ALEM' parcial)
    if raiz:
        return SequenceMatcher(None, raiz, n_name).ratio()
    return 0.0


@app.get("/api/geometria-calle")
def api_geometria_calle():
    nombre = request.args.get("nombre", "").strip()
    if not nombre:
        return jsonify({"error": "falta parametro nombre"}), 400
    jurisdiccion = _jurisdiccion_param()

    objetivo = normalizar(nombre)
    raiz = _raiz_nombre(nombre)

    # Rankear TODOS los tramos candidatos por score
    candidatos = []
    for f in RED_VIAL:
        score = _score_feature(f, objetivo, raiz)
        if score >= 0.8:
            candidatos.append((score, f))

    if not candidatos:
        # Fallback laxo: cualquier via cuyo nombre contenga la raiz (o viceversa)
        for f in RED_VIAL:
            n_name = normalizar((f["properties"] or {}).get("name") or "")
            if n_name and raiz and (raiz in n_name or n_name in raiz):
                candidatos.append((0.7, f))

    if not candidatos:
        return jsonify({"error": f"arteria no encontrada: {nombre}"}), 404

    # Fusionar TODOS los tramos dentro de la jurisdiccion activa
    en_area = [
        f["geometry"]["coordinates"]
        for _, f in candidatos
        if _segmento_en_jurisdiccion(f["geometry"]["coordinates"], jurisdiccion)
    ]
    if not en_area:
        return jsonify(
            {"error": f"{nombre} no tiene tramos en {jurisdiccion.title()}"}
        ), 404
    lineas = en_area

    nombre_orig = max(
        ((f["properties"].get("name") or f["properties"].get("ref") or nombre)
         for _, f in candidatos),
        key=len,
    )
    lons = [c[0] for linea in lineas for c in linea]
    lats = [c[1] for linea in lineas for c in linea]

    return jsonify(
        {
            "nombre": nombre_orig,
            "score": round(max(s for s, _ in candidatos), 3),
            "n_segmentos": len(lineas),
            "bounds": [[min(lons), min(lats)], [max(lons), max(lats)]],
            "geometry": (
                {"type": "LineString", "coordinates": lineas[0]}
                if len(lineas) == 1
                else {"type": "MultiLineString", "coordinates": lineas}
            ),
        }
    )


RADIO_CRUCE_M = 30.0


def _distancia_punto_segmento_m(lat, lon, lat1, lon1, lat2, lon2):
    """Distancia en metros del punto (lat, lon) al segmento (lat1,lon1)-(lat2,lon2).

    Aproximacion equirectangular, valida para distancias cortas.
    """
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    ax, ay = (lon1 - lon) * kx, (lat1 - lat) * ky
    bx, by = (lon2 - lon) * kx, (lat2 - lat) * ky
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def _distancia_min_via_m(lat, lon, geom):
    """Menor distancia del punto a cualquier segmento de la via."""
    lineas = (
        [geom["coordinates"]]
        if geom["type"] == "LineString"
        else geom["coordinates"]
    )
    mejor = float("inf")
    for coords in lineas:
        for i in range(len(coords) - 1):
            lon1, lat1 = coords[i][:2]
            lon2, lat2 = coords[i + 1][:2]
            d = _distancia_punto_segmento_m(lat, lon, lat1, lon1, lat2, lon2)
            if d < mejor:
                mejor = d
    return mejor


def _cruce_en_punto(lat, lon):
    """calles_1/2: vias nombradas mas cercanas al punto dentro de RADIO_CRUCE_M."""
    cercanas = {}  # nombre de via -> distancia minima en metros
    for f in RED_VIAL:
        nombre = ((f["properties"] or {}).get("name") or "").strip()
        if not nombre:
            continue
        d = _distancia_min_via_m(lat, lon, f["geometry"])
        if d <= RADIO_CRUCE_M and d < cercanas.get(nombre, float("inf")):
            cercanas[nombre] = d

    ordenadas = sorted(cercanas.items(), key=lambda kv: kv[1])
    return {
        "calle_1": ordenadas[0][0] if ordenadas else "",
        "calle_2": ordenadas[1][0] if len(ordenadas) > 1 else "",
        "dist_m": round(ordenadas[0][1], 1) if ordenadas else None,
    }


@app.get("/api/cruce")
def api_cruce():
    """Detecta la calle y el cruce mas cercanos a un punto clickeado.

    Devuelve calle_1 (via mas cercana) y calle_2 (segunda via nombrada dentro
    de RADIO_CRUCE_M, interpretada como interseccion). Ambas pueden ser "".
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "coordenadas invalidas"}), 400

    return jsonify(_cruce_en_punto(lat, lon))


@app.post("/api/cruce-lote")
def api_cruce_lote():
    """Resuelve calle_1/calle_2 para varios puntos en una sola llamada.

    Body JSON: {"puntos": [[lat, lon], ...]} (max 80). Se usa para nombrar
    los tramos del itinerario de riesgo del simulador.
    """
    payload = request.get_json(silent=True) or {}
    puntos = payload.get("puntos")
    if not isinstance(puntos, list) or not (1 <= len(puntos) <= 80):
        return jsonify({"error": "puntos invalidos"}), 400

    resultados = []
    for p in puntos:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, IndexError, ValueError):
            resultados.append({"calle_1": "", "calle_2": "", "dist_m": None})
            continue
        resultados.append(_cruce_en_punto(lat, lon))
    return jsonify({"resultados": resultados})


@app.get("/api/buscar-calle")
def api_buscar_calle():
    """Autocompletado de arterias sobre la red vial local.

    Reutiliza src.geocoder.buscar: acepta nombres parciales y cruces
    ("Belgrano y 9 de Julio"). Devuelve hasta 8 sugerencias cuyo punto
    representativo cae dentro de la jurisdiccion activa.
    """
    consulta = request.args.get("q", "").strip()
    if len(consulta) < 2:
        return jsonify({"sugerencias": []})

    jur = _jurisdiccion_query(request.args.get("jurisdiccion", ""))
    lat_c, lon_c, radio = _centro_busqueda(jur)
    bbox = BBOX_JURISDICCION.get(jur, BBOX_JURISDICCION["TODOS"])

    # 1) Red vial local con fuzzy matching (raiz de via + difflib) y
    #    verificacion estricta: el punto medio debe caer dentro del bbox.
    sugerencias = []
    vistos = set()
    fuentes_locales = geo_buscar(consulta)
    if jur == "PALPALA":
        # Palpala esta fuera de la red vial GeoJSON: catalogo propio de
        # arterias troncales (RN 66, Martijena, Rio de la Plata, etc.).
        fuentes_locales = list(fuentes_locales) + list(
            geo_palpala(consulta))
    elif jur == "SAN PEDRO DE JUJUY":
        fuentes_locales = list(fuentes_locales) + list(
            geo_sanpedro(consulta))
    for r in fuentes_locales:
        if not (bbox[1] <= r["lat"] <= bbox[3]
                and bbox[0] <= r["lon"] <= bbox[2]):
            continue  # fuera de la jurisdiccion elegida: no resolver
        clave = normalizar(r["nombre"])
        if clave in vistos:
            continue
        vistos.add(clave)
        sugerencias.append(
            {"nombre": r["nombre"], "lon": r["lon"], "lat": r["lat"],
             "fuente": "local"}
        )
        if len(sugerencias) >= 8:
            break

    # 2) Nominatim acotado al MISMO bbox: cubre San Salvador (fuera de la
    #    red local) y arterias que la red no tiene mapeadas.
    if len(sugerencias) < 8:
        ciudad = {"PERICO": "Perico", "MONTERRICO": "Monterrico",
                  "EL CARMEN": "El Carmen",
                  "SAN SALVADOR DE JUJUY": "San Salvador"}.get(jur, "Jujuy")
        try:
            for r in geo_nominatim(
                    f"{consulta}, {ciudad}, Jujuy, Argentina",
                    limite=5, viewbox=bbox):
                if r.get("tipo") not in _TIPOS_VIA_NOMINATIM:
                    continue
                nombre = ", ".join(
                    str(r["nombre"]).split(",")[:2]).strip()
                clave = normalizar(nombre)
                if not nombre or clave in vistos:
                    continue
                vistos.add(clave)
                sugerencias.append(
                    {"nombre": nombre, "lon": r["lon"], "lat": r["lat"],
                     "fuente": "nominatim"}
                )
                if len(sugerencias) >= 8:
                    break
        except requests.RequestException:
            pass
    return jsonify({"sugerencias": sugerencias})


DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

MODOS_VALIDOS = {
    "MOTO", "AUTO", "CAMIONETA", "BICICLETA", "PEATON", "COLECTIVO", "CAMION",
}
GRAVEDADES_VALIDAS = {"LEVE": "LEVE", "GRAVE": "GRAVE", "SOLO DANOS": "SOLO DAÑOS"}

CONTRAPARTES_VALIDAS = {
    "AUTO", "MOTO", "CAMIONETA", "CAMION", "COLECTIVO", "NINGUNO",
    "OBJETO FIJO / COLUMNA", "OBJETO FIJO", "COLUMNA", "PEATON", "BICICLETA",
}

LOCK_CSV = threading.Lock()


def _periodo_dia(hora):
    """Mismo etiquetado de franja que usa el dataset."""
    if hora < 6:
        return "Madrugada (00-06h)", 0
    if hora < 12:
        return "Mañana (06-12h)", 1
    if hora < 19:
        return "Tarde (12-19h)", 2
    return "Noche (19-24h)", 3


# Palabras de menu/banner del diario que no deben llegar a Gemini.
_BASURA_NOTA = ("facebook", "twitter", "instagram", "youtube",
              "copyright", "todos los derechos", "newsletter",
              "secciones", "teads", "banner", "whatsapp", "suscrib",
              "publicidad", "compartir", "comentarios", "politica de",
              "close menu", "open menu", "menu principal")

# Vocabulario de secciones/menus: una linea formada mayormente por estas
# palabras ("Politica Economia Deportes Policiales Mundo Salud FNE
# Cultura") es la cabecera del diario, no cronica -> se descarta.
_CATEGORIAS_DIARIO = {
    "politica", "economia", "deportes", "policiales", "mundo",
    "salud", "fne", "cultura", "sociedad", "espectaculos",
    "nacionales", "provinciales", "tecnologia", "opinion",
    "editorial", "agro", "rural", "turismo", "ciencia", "clima",
    "transito", "servicios", "horoscopo", "edicion", "impresa",
    "inicio", "contacto", "newsletter", "ultimas", "noticias",
    "actualidad", "judiciales", "espectaculo", "gastronomia",
    "musica", "cine", "virales", "tendencias", "internacionales",
    "regionales", "locales", "sucesos", "secciones", "menu",
    "buscar", "login", "ingresar", "registrate", "suscribite",
    "momento", "vivo", "podcast", "videos", "fotos", "news",
    "columnistas", "cartas", "lectores", "clasificados",
    "necrologicas", "funerales", "farmacia", "turnos", "radio",
    "tiempo", "dolar", "euro", "merval", "cripto",
}


def _limpiar_texto_nota(texto_crudo):
    """Pre-procesador de la nota pegada/descargada: descarta las lineas
    cortas, las de menu/banner del diario y las cabeceras de secciones
    (lineas hechas casi por completo de nombres de categorias); conserva
    solo las 25 oraciones sustanciales que se envian a Gemini."""
    lineas = [l.strip() for l in str(texto_crudo or "").split("\n")
              if len(l.strip()) > 35]
    limpias = []
    for l in lineas:
        low = l.lower()
        if any(b in low for b in _BASURA_NOTA):
            continue
        toks = [t for t in re.split(r"[^\wáéíóúñü]+", low) if t]
        if (toks and sum(t in _CATEGORIAS_DIARIO for t in toks)
                / len(toks) >= 0.6):
            continue  # cabecera de categorias, no cronica
        limpias.append(l)
    return "\n".join(limpias[:25])


def _prompt_nota_completa(texto):
    return (
        "Eres un perito vial forense de Jujuy, Argentina. Analiza esta "
        "crónica periodística:\n\"\"\"" + texto + "\"\"\"\n\n"
        "Extrae rigurosamente:\n"
        "1. jurisdiccion: Debe ser estrictamente una de: ['San Pedro de "
        "Jujuy', 'Ciudad Perico', 'Monterrico', 'El Carmen', 'Palpalá', "
        "'San Salvador de Jujuy', 'Rutas']. Si menciona San Pedro, DEBE "
        "ser 'San Pedro de Jujuy'.\n"
        "2. arteria_principal: Nombre de la calle principal (ej: "
        "'Avenida Brasil').\n"
        "3. arteria_secundaria: Calle transversal del cruce si la hay "
        "(ej: 'Avenida Haití'), o null.\n"
        "4. vehiculo_1: Vehículo principal involucrado (ej: 'Moto').\n"
        "5. vehiculo_2: Contraparte (ej: 'Remís / Auto'). NUNCA pongas "
        "'Ninguno' si chocó contra un auto/remís.\n"
        "6. victimas_cantidad: Número entero de heridos/fallecidos.\n"
        "7. victimas_estado: 'Leve', 'Grave', 'Fatal' o 'Solo Daños'. "
        "(Fractura = 'Grave').\n"
        "8. resumen: Síntesis técnica de EXACTAMENTE 2 oraciones sobre "
        "la mecánica del choque y consecuencias: qué vehículos chocaron, "
        "en qué esquina/ruta de qué ciudad y el estado de los heridos. "
        "PROHIBIDO incluir palabras de navegación, banners o "
        "secciones.\n"
        "9. fecha_siniestro: fecha del hecho en formato 'AAAA-MM-DD'. "
        "Si la crónica dice 'ayer', 'anoche', 'hace algunas noches' o "
        "menciona un día/mes, calculá la fecha estimada a partir de la "
        "fecha de publicación del artículo (estamos en 2026). PROHIBIDO "
        "dejar la fecha vacía.\n"
        "10. Si el texto proviene de una publicación informal (Facebook, "
        "WhatsApp, cadenas), INFIERE la jurisdiccion y la arteria por "
        "las referencias locales (ej: 'en Perico frente a la feria "
        "minorista' -> jurisdiccion 'Ciudad Perico' y arteria_principal "
        "'Avenida República de Italia', el acceso a la feria); el hito o "
        "barrio citado va en referencia_local.\n\n"
        "Responde EXCLUSIVAMENTE un JSON valido con estas claves.")


def _extraer_nota_completa(texto):
    """Ingesta inteligente: extraccion contextual completa via Gemini.

    Gemini es OBLIGATORIO: no hay fallback de reglas locales. Ante
    cualquier fallo se imprime el traceback real en consola y se relanza
    la excepcion para que el endpoint informe la causa exacta."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")
    ultimo = RuntimeError("Gemini no respondio")
    for modelo in GEMINI_MODELOS:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{modelo}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={
                    "contents": [{"role": "user", "parts": [
                        {"text": _prompt_nota_completa(texto[:8000])}]}],
                    "generationConfig": {
                        "maxOutputTokens": 700, "temperature": 0.25,
                        "responseMimeType": "application/json"},
                },
                timeout=30)
            if r.status_code != 200:
                ultimo = RuntimeError(
                    f"{modelo}: HTTP {r.status_code} {r.text[:200]}")
                print(f"[extraer-nota] {ultimo}", file=sys.stderr)
                continue
            out = "".join(
                p.get("text", "")
                for c in r.json().get("candidates", [])
                for p in c.get("content", {}).get("parts", []))
            d = json.loads(re.sub(r"```(?:json)?", "", out).strip())
            if isinstance(d, dict):
                return d
            ultimo = RuntimeError(f"{modelo}: respuesta sin JSON valido")
        except Exception as exc:
            traceback.print_exc()
            ultimo = exc
    raise ultimo


_RESUMEN_BASURA = re.compile(
    r"(?i)(window\.[\w$.]+|teads_analytics|close\s*menu|open\s*menu|"
    r"men[uú]\s*principal|ir\s+al\s+contenido|saltar\s+al\s+contenido|"
    r"secciones|newsletter|suscrib\w*|publicidad|whatsapp\s*share|"
    r"facebook\s*share|compartir\s+en|leer\s+mas|ver\s+mas)")


_ABREV_RESUMEN = re.compile(
    r"(?i)(?:av|dr|dra|sr|sra|gral|ing|lic|km|n°|num)\.$|"
    r"(?:rn|rp|ruta)\s*\w*\.$")


def _limpiar_resumen_noticia(res):
    """Saca del resumen restos de navegacion/publicidad del diario y lo
    recorta a un maximo de 2 oraciones limpias."""
    s = " ".join(str(res or "").split())
    s = _RESUMEN_BASURA.sub(" ", s)
    s = re.sub(r"\.(\s*\.)+", ". ", s)  # restos ". ." de menus/scripts
    s = re.sub(r"(?i)^(?:(?:fne|cultura|deportes|policiales?|sociedad|"
               r"economia|espectaculos|nacionales|provinciales|"
               r"secciones|menu)[\s:.\-]*)+", "", s)
    partes = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", s)
              if p.strip()]
    frases = []
    for p in partes:
        # Reincorpora cortes falsos por abreviatura ("Av.", "RN 66.") o
        # fragmentos que quedaron sin cierre de oracion.
        if frases and (not frases[-1].endswith((".", "!", "?", "…"))
                       or _ABREV_RESUMEN.search(frases[-1])):
            frases[-1] += " " + p
        else:
            frases.append(p)
    s = " ".join(frases[:2])
    return " ".join(s.split())[:400]


_MESES_NOMBRE = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
    "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _fecha_nombre_mes(texto):
    """Parsea '24 de octubre de 2026' (nombre de mes en espanol)."""
    m = re.search(
        r"\b(\d{1,2})\s+de\s+([a-z\xe1\xe9\xed\xf3\xfa]+)\s+de?\s*(\d{4})",
        (texto or "").lower())
    if not m:
        return None
    mes = _MESES_NOMBRE.get(m.group(2))
    if not mes:
        return None
    try:
        return datetime(int(m.group(3)), mes, int(m.group(1))).date()
    except ValueError:
        return None


def _parse_fecha_libre(s):
    """Cualquier formato razonable -> date o None (ISO, dd/mm/aaaa,
    'D de mes de aaaa', RFC 2822)."""
    s = (s or "").strip()
    f = _fecha_nombre_mes(s) or _fecha_de_texto(s)
    if f:
        return f
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


def _extraer_fecha_html(doc):
    """Fecha de publicacion desde metadatos del HTML (JSON-LD,
    article:published_time, pubdate, datetime) o del propio cuerpo."""
    for pat in (
            r'"datePublished"\s*:\s*"([^"]{8,40})"',
            r"article:published_time[\"']?\s*content=[\"']([^\"']{8,40})",
            r"(?:pubdate|publish(?:ed)?(?:_| )date|fecha)[^>]{0,40}?"
            r"(?:datetime|content)=[\"']([^\"']{8,40})"):
        m = re.search(pat, doc, re.I)
        if m:
            f = _parse_fecha_libre(m.group(1))
            if f:
                return f
    return _fecha_nombre_mes(doc[:6000])


def _norm_victimas_estado(datos):
    """Normaliza victimas_estado a ILESO|LEVE|GRAVE|FATAL; lo deduce de
    la gravedad cuando Gemini no lo devuelve."""
    e = str(datos.get("victimas_estado") or "").strip().upper()
    e = {"ILESOS": "ILESO", "ILESO": "ILESO", "LEVE": "LEVE",
         "LEVES": "LEVE", "GRAVE": "GRAVE", "GRAVES": "GRAVE",
         "FATAL": "FATAL", "FATALES": "FATAL", "FALLECIDO": "FATAL",
         "FALLECIDOS": "FATAL", "MUERTO": "FATAL", "MUERTOS": "FATAL",
         "SOLO DANOS": "ILESO", "SOLO DAÑOS": "ILESO",
         "DANOS": "ILESO", "DAÑOS": "ILESO",
         "SIN LESIONES": "ILESO"}.get(e)
    if e:
        return e
    grv = str(datos.get("gravedad") or "").upper()
    if "FATAL" in grv:
        return "FATAL"
    if "GRAVE" in grv:
        return "GRAVE"
    if "DANO" in grv:
        return "ILESO"
    return "LEVE"


def _armar_extraccion_nota(texto):
    """Pipeline comun de la nota completa: SOLO Gemini + geocoding.

    PROHIBIDO el fallback de reglas locales (copiaban la cabecera del
    diario al resumen). Devuelve el dict 'extraido' confirmable por el
    usuario; lo comparten la ingesta por texto pegado y por URL."""
    datos = _extraer_nota_completa(texto)
    jur_txt = clasificar_jurisdiccion(texto)
    if jur_txt == "OTRA":
        jur_txt = ""
    jur = (_norm_jurisdiccion_valor(datos.get("jurisdiccion"))
           or _norm_jurisdiccion_valor(jur_txt))
    # Guard hermetico: si el texto habla de San Pedro jamas cae a Perico.
    if re.search(r"SAN PEDRO|INGENIO LA ESPERANZA|PATERSON",
                 normalizar(texto)):
        jur = "San Pedro de Jujuy"
    c1 = _sanear_arteria(datos.get("arteria_principal"),
                         datos.get("arteria_principal"))
    c2 = _sanear_arteria(datos.get("arteria_secundaria"),
                         datos.get("arteria_secundaria"))
    if (c2 and c1 and limpiar_calle(c2) == limpiar_calle(c1)):
        c2 = ""
    ref = str(datos.get("referencia_local") or "").strip()
    geo = geocodificar_noticia(c1, c2, jur or "")

    def _norm_v(v, defecto):
        n = str(v or "").strip().upper()
        if "/" in n:  # 'Remís / Auto' -> primer vehiculo valido
            n = n.split("/")[0].strip()
        n = {"BICI": "BICICLETA", "AUTO": "AUTO", "MOTO": "MOTO",
             "PEATON": "PEATON", "PEATÓN": "PEATON", "CAMION": "CAMION",
             "CAMIÓN": "CAMION", "CAMIONETA": "CAMIONETA",
             "COLECTIVO": "COLECTIVO", "NINGUNO": "NINGUNO",
             "REMIS": "AUTO", "REMÍS": "AUTO", "TAXI": "AUTO",
             "BUS": "COLECTIVO", "OMNIBUS": "COLECTIVO",
             "SOLO DAÑOS": "SOLO DAÑOS", "SOLO DANOS": "SOLO DAÑOS",
             "FATAL": "GRAVE"}.get(n, n)
        return n or defecto

    try:
        vic_n = max(0, int(datos.get("victimas_cantidad")))
    except (TypeError, ValueError):
        vic_n = 1
    estado = _norm_victimas_estado(datos)

    return {"status": "ok", "extraido": {
        "jurisdiccion": jur or "Región Valles",
        "arteria_principal": c1.title() if c1 else "",
        "arteria_secundaria": c2.title() if c2 else "",
        "referencia_local": ref,
        "vehiculo_1": _norm_v(datos.get("vehiculo_1"), "AUTO"),
        "vehiculo_2": _norm_v(datos.get("vehiculo_2"), "NINGUNO"),
        "gravedad": {"ILESO": "SOLO DAÑOS", "FATAL": "GRAVE"}.get(
            estado, estado),
        "victimas_cantidad": vic_n,
        "victimas_estado": estado,
        "fecha_siniestro": (
            lambda f: f.isoformat() if f else ""
        )(_parse_fecha_libre(str(datos.get("fecha_siniestro") or ""))
          or _parse_fecha_libre(texto)),
        "resumen": _limpiar_resumen_noticia(datos.get("resumen")),
        "lat": geo["lat"], "lon": geo["lon"],
        "geo_fuente": geo["fuente"],
        "geo_nombre": geo.get("nombre") or "",
        "geo_valido": bool(geo.get("coordenadas_validas")),
        "fuente": "gemini",
    }}


@app.post("/api/extraer-nota-completa")
def api_extraer_nota_completa():
    """Pegado completo de una nota -> Gemini OBLIGATORIO + geocoding.

    Sin fallback de reglas locales: si Gemini falla se informa el error
    real (502) en lugar de disfrazarlo con datos inventados."""
    payload = request.get_json(silent=True) or {}
    crudo = str(payload.get("texto") or "").strip()
    if len(crudo) < 40:
        return jsonify({"status": "error",
                        "mensaje": "pegá el cuerpo completo de la nota"}
                       ), 400
    # Pre-procesador: afuera menus/banners del diario antes de Gemini.
    texto = _limpiar_texto_nota(crudo) or crudo[:8000]
    try:
        return jsonify(_armar_extraccion_nota(texto))
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"status": "error",
                        "mensaje": f"Gemini no respondió: {exc}"}), 502


@app.post("/api/extraer-nota-url")
def api_extraer_nota_url():
    """URL de una nota -> descarga el HTML con headers de navegador real,
    extrae el texto de los parrafos del articulo limpiando menus y
    scripts, y lo procesa con la misma logica de Gemini que el pegado."""
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    if not re.match(r"^https?://\S+$", url):
        return jsonify({"status": "error",
                        "mensaje": "pegá una URL válida (https://…)"}
                       ), 400
    try:
        descargado, url_final = _descargar_cuerpo_noticia(url)
    except requests.RequestException:
        return jsonify({"status": "error",
                        "mensaje": ("no se pudo descargar la nota desde "
                                    "ese enlace; probá con la pestaña "
                                    "de texto")}
                       ), 400
    if len(descargado) < 40:
        return jsonify({"status": "error",
                        "mensaje": ("la página no expuso el cuerpo de la "
                                    "nota; probá pegando el texto")}
                       ), 400
    texto = _limpiar_texto_nota(descargado) or descargado[:8000]
    try:
        resp = _armar_extraccion_nota(texto)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"status": "error",
                        "mensaje": f"Gemini no respondió: {exc}"}), 502
    resp["url_final"] = url_final
    return jsonify(resp)


@app.post("/api/reportar")
def api_reportar():
    payload = request.get_json(silent=True) or {}

    try:
        lat = float(payload["latitud_siniestro"])
        lon = float(payload["longitud_siniestro"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"status": "error", "mensaje": "coordenadas invalidas"}), 400

    calle_1 = " ".join(str(payload.get("calle_1") or "").strip().upper().split())
    calle_2 = " ".join(str(payload.get("calle_2") or "").strip().upper().split())
    if not calle_1:
        return jsonify(
            {"status": "error", "mensaje": "falta la calle del siniestro"}
        ), 400

    modo = normalizar(payload.get("modo_desplazamiento_victima"))
    if modo not in MODOS_VALIDOS:
        return jsonify(
            {"status": "error", "mensaje": "modo de desplazamiento invalido"}
        ), 400

    gravedad = GRAVEDADES_VALIDAS.get(normalizar(payload.get("gravedad_siniestro")))
    if gravedad is None:
        return jsonify(
            {"status": "error", "mensaje": "gravedad invalida"}
        ), 400

    contraparte = normalizar(payload.get("contraparte_siniestro") or "NINGUNO")
    if contraparte in ("OBJETO FIJO", "COLUMNA"):
        contraparte = "OBJETO FIJO / COLUMNA"
    if contraparte not in CONTRAPARTES_VALIDAS:
        return jsonify(
            {"status": "error", "mensaje": "contraparte invalida"}
        ), 400

    try:
        total_vic = max(1, int(payload.get("total_victimas")))
    except (TypeError, ValueError):
        total_vic = 1

    ahora = datetime.now()

    fecha_txt = str(payload.get("fecha_siniestro") or "").strip()
    fecha = ahora.date()
    if fecha_txt:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                fecha = datetime.strptime(fecha_txt, fmt).date()
                break
            except ValueError:
                continue

    hora_txt = str(payload.get("hora_siniestro") or "").strip()
    hh, mm = ahora.hour, ahora.minute
    try:
        partes = hora_txt.split(":")
        hh, mm = int(partes[0]), int(partes[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            hh, mm = ahora.hour, ahora.minute
    except (ValueError, IndexError):
        pass

    hora = hh
    dia_num = fecha.weekday()
    fin_sem = int(dia_num >= 5)
    periodo_nombre, periodo_num = _periodo_dia(hora)
    es_grave = gravedad == "GRAVE"

    with LOCK_CSV:
        siguiente = max(
            int(s["id_siniestro"].rsplit("-", 1)[1])
            for s in SINIESTROS
            if s["id_siniestro"].startswith("PER-")
        ) + 1
        nuevo_id = f"PER-{siguiente:04d}"

        causa = str(
            payload.get("descripcion") or payload.get("causa_probable") or ""
        ).strip().upper()

        fila = {
            "id_siniestro": nuevo_id,
            "fecha_siniestro": fecha.strftime("%d/%m/%Y"),
            "hora_siniestro": f"{hh:02d}:{mm:02d}",
            "calle_1": calle_1,
            "calle_2": calle_2,
            "tipo_de_via": "RUTA/ACCESO" if "RUTA" in calle_1 else "URBANA",
            "latitud_siniestro": f"{lat:.6f}",
            "longitud_siniestro": f"{lon:.6f}",
            "modo_desplazamiento_victima": modo,
            "contraparte_siniestro": contraparte,
            "total_victimas": str(total_vic),
            "victimas_leves": str(total_vic) if gravedad == "LEVE" else "0",
            "victimas_graves": str(total_vic) if es_grave else "0",
            "sexo_victima": normalizar(payload.get("sexo_victima")),
            "edad_victima": str(payload.get("edad_victima") or ""),
            "gravedad_victima": gravedad,
            "gravedad_siniestro": gravedad,
            "asistencia_same": "SÍ" if es_grave else "NO",
            "causa_probable": causa or "NO ESPECIFICADA",
            "fecha_dt": fecha.isoformat(),
            "anio_siniestro": str(fecha.year),
            "mes_siniestro": str(fecha.month),
            "dia_siniestro": str(fecha.day),
            "dia_semana_nombre": DIAS_SEMANA[dia_num],
            "dia_semana_num": str(dia_num),
            "fin_semana": str(fin_sem),
            "es_dia_feria": str(1 - fin_sem),
            "es_feriado": "0",
            "hora_num": str(hora),
            "periodo_dia_nombre": periodo_nombre,
            "periodo_dia_num": str(periodo_num),
            "rango_etario": str(payload.get("rango_etario") or ""),
            "es_post_semaforos": "0",
            "target_es_grave": "1" if es_grave else "0",
            "target_same": "1" if es_grave else "0",
            "url_noticia": str(payload.get("url_noticia") or "").strip()[:500],
            "medio_prensa": str(
                payload.get("medio_prensa") or "").strip()[:160],
            "resumen_contexto": str(
                payload.get("resumen_contexto") or "").strip()[:400],
        }

        with open(RUTA_CSV, "a", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=COLUMNAS).writerow(fila)

        # Misma derivacion de campos internos que en _cargar_siniestros
        fila["_lat"], fila["_lon"], fila["_hora"] = lat, lon, hora
        fila["_jur"] = _jurisdiccion_de(lat, lon)
        fila["_modo"] = modo
        fila["_dia"] = normalizar(DIAS_SEMANA[dia_num])
        SINIESTROS.append(fila)

    return jsonify(
        {
            "status": "ok",
            "mensaje": "Siniestro registrado exitosamente",
            "id": nuevo_id,
        }
    )


# ================== MODULO 4: Machine Learning + Copiloto IA ==================

RUTA_MODELO = RAIZ / "data" / "modelo_riesgo.pkl"
RUTA_METRICAS_ML = RAIZ / "data" / "metricas_modelos.json"

CAT_COLS_ML = [
    "modo_desplazamiento_victima",
    "contraparte_siniestro",
    "tipo_de_via",
    "dia_semana_nombre",
    "periodo_dia_nombre",
    "calle_1",
]
NUM_COLS_ML = [
    "hora_num", "edad_victima", "total_victimas",
    "es_dia_feria", "es_feriado", "fin_semana", "es_post_semaforos",
]

_MODELO = None
_MAPA_CAT = None  # columna -> {valor_normalizado: categoria_original}
_IMPS = None      # feature -> importancia global del Random Forest


def _modelo():
    global _MODELO
    if _MODELO is None:
        _MODELO = joblib.load(RUTA_MODELO)
    return _MODELO


def _mapa_categorias():
    """Mapea entrada del usuario (normalizada) a la categoria exacta del OHE."""
    global _MAPA_CAT
    if _MAPA_CAT is None:
        m = _modelo()
        pipe = m["pipelines"][m.get("modelo_inferencia") or m["mejor_modelo"]]
        ohe = pipe.named_steps["pre"].named_transformers_["cat"]
        _MAPA_CAT = {
            col: {normalizar(str(c)): str(c) for c in cats}
            for col, cats in zip(m.get("cat_cols", CAT_COLS_ML), ohe.categories_)
        }
    return _MAPA_CAT


def _canon(col, valor):
    """Categoria canonica para el OHE: exacta, o por contencion ('RUTA' -> 'RUTA/ACCESO')."""
    nv = normalizar(valor)
    m = _mapa_categorias()[col]
    if nv in m:
        return m[nv]
    for ncat, cat in m.items():
        if nv and (nv in ncat or ncat in nv):
            return cat
    return nv  # desconocida -> OHE la ignora (handle_unknown='ignore')


def _importancias():
    global _IMPS
    if _IMPS is None:
        _IMPS = {}
        try:
            with open(RUTA_METRICAS_ML, encoding="utf-8") as f:
                _IMPS = {
                    d["feature"]: d["importance"]
                    for d in json.load(f).get("feature_importance", [])
                }
        except (OSError, json.JSONDecodeError):
            pass
    return _IMPS


def _fila_inferencia(p):
    """Traduce el payload del simulador a una fila con las columnas del modelo."""
    try:
        hora = int(str(p.get("hora", 12)).split(":")[0])
    except (TypeError, ValueError):
        hora = 12
    hora = min(max(hora, 0), 23)

    dia = str(p.get("dia_semana") or "Lunes").strip().title()
    fin_semana = 1 if normalizar(dia) in ("SABADO", "DOMINGO") else 0
    try:
        es_feria = int(p.get("es_feria"))
    except (TypeError, ValueError):
        es_feria = 0 if fin_semana else 1
    try:
        edad = int(p.get("edad") or 30)
    except (TypeError, ValueError):
        edad = 30
    try:
        total_vic = max(1, int(p.get("total_victimas") or 1))
    except (TypeError, ValueError):
        total_vic = 1
    try:
        es_feriado = int(p.get("es_feriado") or 0)
    except (TypeError, ValueError):
        es_feriado = 0
    try:
        es_semaforos = int(
            p.get("es_post_semaforos", p.get("semaforo")) or 0)
    except (TypeError, ValueError):
        es_semaforos = 0

    periodo_nombre, _ = _periodo_dia(hora)
    return {
        "modo_desplazamiento_victima": _canon(
            "modo_desplazamiento_victima", p.get("modo")),
        "contraparte_siniestro": _canon(
            "contraparte_siniestro", p.get("contraparte") or "NINGUNO"),
        "tipo_de_via": _canon("tipo_de_via", p.get("tipo_via") or "URBANA"),
        "dia_semana_nombre": _canon("dia_semana_nombre", dia),
        "periodo_dia_nombre": _canon("periodo_dia_nombre", periodo_nombre),
        "calle_1": _canon(
            "calle_1", p.get("calle_1") or p.get("calle") or "OTRA"),
        "hora_num": hora,
        "edad_victima": edad,
        "total_victimas": total_vic,
        "es_dia_feria": es_feria,
        "es_feriado": es_feriado,
        "fin_semana": fin_semana,
        "es_post_semaforos": es_semaforos,
    }


def _factor_principal(fila):
    """Variable que mas peso tuvo en el veredicto, ponderada por la
    importancia global del Random Forest cuando esta disponible."""
    imps = _importancias()

    def peso(prefijo):
        return sum(v for k, v in imps.items() if k.startswith(prefijo))

    modo = fila["modo_desplazamiento_victima"]
    hora = fila["hora_num"]
    edad = fila["edad_victima"]
    cand = []
    if modo == "MOTO":
        cand.append((peso("modo_desplazamiento_victima"), "Modo motocicleta"))
    elif modo in ("PEATON", "BICICLETA"):
        cand.append((peso("modo_desplazamiento_victima"),
                     f"Victima vulnerable ({modo.lower()})"))
    if hora >= 19 or hora < 6:
        cand.append((peso("hora_num") + peso("periodo_dia_nombre"),
                     "Horario nocturno"))
    if str(fila["tipo_de_via"]).startswith("RUTA"):
        cand.append((peso("tipo_de_via"), "Circulacion por ruta/acceso"))
    if fila["contraparte_siniestro"] in ("CAMION", "COLECTIVO", "OBJETO FIJO",
                                         "GUARDARRAIL"):
        cand.append((peso("contraparte_siniestro"),
                     "Contraparte de alto impacto"))
    if edad < 26 or edad > 65:
        cand.append((peso("edad_victima"), "Franja etaria de riesgo"))
    if fila["total_victimas"] > 1:
        cand.append((peso("total_victimas"), "Multiples victimas"))
    calle = normalizar(fila["calle_1"])
    if calle and calle not in ("OTRA", "SIN DATOS"):
        cand.append((peso("calle_1"),
                     f"Arteria critica ({str(fila['calle_1']).title()})"))
    if fila["fin_semana"]:
        cand.append((peso("fin_semana"), "Fin de semana"))
    if fila["es_feriado"]:
        cand.append((peso("es_feriado"), "Feriado"))
    if not cand:
        return "Contexto urbano diurno"
    return max(cand)[1]


@app.post("/api/predecir")
def api_predecir():
    """Inferencia del mejor modelo sobre un escenario de siniestro.

    Body JSON: modo, contraparte, tipo_via, hora, dia_semana, edad,
               es_feria (+ opcionales total_victimas, es_feriado).
    """
    if not RUTA_MODELO.exists():
        return jsonify({
            "status": "error",
            "mensaje": "modelo no entrenado: ejecutar src/entrenar_modelos.py",
        }), 503

    payload = request.get_json(silent=True) or {}
    try:
        fila = _fila_inferencia(payload)
        m = _modelo()
        cols = m.get("cat_cols", CAT_COLS_ML) + m.get("num_cols", NUM_COLS_ML)
        X = pd.DataFrame([fila])[cols]
        pipe = m["pipelines"][m.get("modelo_inferencia") or m["mejor_modelo"]]
        proba = float(pipe.predict_proba(X)[0][1])
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({"status": "error", "mensaje": str(exc)}), 400

    prob = round(100 * proba, 1)
    nivel = "Crítico" if prob >= 70 else ("Moderado" if prob >= 40 else "Bajo")
    return jsonify({
        "probabilidad_grave": prob,
        "nivel_riesgo": nivel,
        "factor_principal": _factor_principal(fila),
        "modelo": m.get("modelo_inferencia") or m["mejor_modelo"],
    })


@app.get("/api/metricas-ml")
def api_metricas_ml():
    """Metricas academicas KDD: ambos modelos, matrices y feature importance."""
    if not RUTA_METRICAS_ML.exists():
        return jsonify({
            "status": "error",
            "mensaje": "metricas no generadas: ejecutar src/entrenar_modelos.py",
        }), 503
    with open(RUTA_METRICAS_ML, encoding="utf-8") as f:
        return jsonify(json.load(f))


# ---------------- Copiloto IA (Gemini con fallback por reglas) ----------------

def _leer_env():
    """Lee pares CLAVE=VALOR del .env local sin dependencias externas."""
    valores = {}
    try:
        with open(RAIZ / ".env", encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                clave, valor = linea.split("=", 1)
                valores[clave.strip()] = valor.strip().strip("\"'")
    except OSError:
        pass
    return valores


# Prioriza la variable de entorno del sistema; si falta, lee el .env local.
GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY") or _leer_env().get("GEMINI_API_KEY")
    or ""
)
# Modelos verificados contra la API (nov-2026): gemini-2.5-flash-lite ya
# no existe (404) y la cuota diaria de gemini-2.5-flash se agota rapido;
# los 3.1-flash-lite responden 200 con cuota propia. 2.5-flash queda
# ultimo por si renueva su cupo.
GEMINI_MODELOS = (
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
)

_STATS = None


def _stats_globales():
    """Resumen del dataset para contexto del asistente y respuestas por reglas."""
    global _STATS
    if _STATS is None:
        total = len(SINIESTROS)
        graves = sum(
            1 for s in SINIESTROS
            if normalizar(s["gravedad_siniestro"]) == "GRAVE")
        motos = sum(1 for s in SINIESTROS if s["_modo"] == "MOTO")
        same = sum(
            1 for s in SINIESTROS
            if normalizar(s.get("asistencia_same")) in ("SI",))
        feria = sum(
            1 for s in SINIESTROS if str(s.get("es_dia_feria")) == "1")
        finsem = sum(
            1 for s in SINIESTROS if str(s.get("fin_semana")) == "1")
        noche = sum(
            1 for s in SINIESTROS if s["_hora"] >= 19 or s["_hora"] < 6)
        edades = []
        for s in SINIESTROS:
            try:
                e = float(s.get("edad_victima") or 0)
                if 3 <= e <= 100:
                    edades.append(e)
            except (TypeError, ValueError):
                continue
        ranking, periodos, modos, contras = {}, {}, {}, {}
        for s in SINIESTROS:
            c1 = (s["calle_1"] or "SIN DATOS").strip().upper()
            ranking[c1] = ranking.get(c1, 0) + 1
            p = s.get("periodo_dia_nombre") or "SIN DATOS"
            periodos[p] = periodos.get(p, 0) + 1
            modos[s["_modo"]] = modos.get(s["_modo"], 0) + 1
            contra = (s.get("contraparte_siniestro") or "").strip().upper()
            if contra:
                contras[contra] = contras.get(contra, 0) + 1
        top = sorted(ranking.items(), key=lambda kv: -kv[1])[:6]
        peor = max(periodos.items(), key=lambda kv: kv[1])
        _STATS = {
            "total": total,
            "graves": graves,
            "pct_grave": round(100 * graves / total, 1) if total else 0,
            "pct_moto": round(100 * motos / total, 1) if total else 0,
            "pct_same": round(100 * same / total, 1) if total else 0,
            "pct_feria": round(100 * feria / total, 1) if total else 0,
            "pct_fin_semana": round(100 * finsem / total, 1) if total else 0,
            "pct_noche": round(100 * noche / total, 1) if total else 0,
            "edad_promedio": round(sum(edades) / len(edades), 1) if edades else 0,
            "modo_top": max(modos.items(), key=lambda kv: kv[1]),
            "contraparte_top": max(contras.items(), key=lambda kv: kv[1]),
            "modos": sorted(modos.items(), key=lambda kv: -kv[1]),
            "top_calles": top,
            "peor_franja": peor,
            "periodos": sorted(periodos.items(), key=lambda kv: -kv[1]),
        }
    return _STATS


@app.get("/api/stats")
def api_stats():
    """Curiosidades rapidas del dataset para la seccion ludica."""
    s = _stats_globales()
    return jsonify({
        "total": s["total"],
        "pct_grave": s["pct_grave"],
        "pct_moto": s["pct_moto"],
        "pct_same": s["pct_same"],
        "pct_feria": s["pct_feria"],
        "pct_noche": s["pct_noche"],
        "edad_promedio": s["edad_promedio"],
        "peor_franja": s["peor_franja"][0],
        "top_calle": s["top_calles"][0][0],
        "top_calle_n": s["top_calles"][0][1],
    })


def _contexto_ia():
    s = _stats_globales()
    calles = ", ".join(f"{c} ({n})" for c, n in s["top_calles"][:3])
    puntos = " | ".join(
        f"{jur.title()}: {', '.join(pts)}"
        for jur, pts in PUNTOS_CRITICOS.items())
    return (
        "Sos el Copiloto Vial Inteligente de los Valles de Jujuy (Perico, "
        "Monterrico, El Carmen), el asistente del Observatorio de "
        "Siniestralidad Vial.\n"
        "TONO: neutro, claro, respetuoso, empático y profesional, como un "
        "asistente GPS de navegación. Español rioplatense estándar: sin "
        "modismos regionales forzados (prohibido 'chango', 'picante', "
        "'che', 'a pata' y similares).\n"
        "REGLAS:\n"
        "1. Respuestas concisas: máximo 2 párrafos cortos, o viñetas "
        "claras cuando des recomendaciones.\n"
        "2. NUNCA repitas estadísticas como un contestador: solo cita una "
        "cifra del observatorio cuando aporte valor real a la charla.\n"
        "3. Adaptate al modo del usuario: si va caminando hablá de "
        "peatones, si va en auto de conductores; no fuerces datos de "
        "motos si no vienen al caso.\n"
        "4. Podés cerrar con una pregunta breve de seguimiento si sirve "
        "para personalizar el consejo.\n"
        "5. Cuando pregunten por calles o zonas de Monterrico o El "
        "Carmen, citá los puntos críticos reales de la lista: no inventes "
        "otras arterias ni cifras.\n"
        f"PUNTOS CRITICOS REALES POR JURISDICCION: {puntos}.\n"
        "DATOS DEL OBSERVATORIO (usar solo si aportan valor): "
        f"{s['total']} siniestros registrados; {s['pct_grave']}% graves; "
        f"{s['pct_same']}% con asistencia SAME; {s['pct_moto']}% involucran "
        f"motos; {s['pct_feria']}% en dias de alta concurrencia comercial; "
        f"{s['pct_noche']}% nocturnos (19-06h); edad promedio de victimas: "
        f"{s['edad_promedio']} anos; franja pico: {s['peor_franja'][0]} "
        f"({s['peor_franja'][1]} casos); arterias mas criticas: {calles}. "
        "No inventes estadisticas ni cifras que no esten en este contexto."
    )


def _preguntar_gemini(mensaje, historial=None, sistema=None, max_tokens=400,
                    temperatura=0.6):
    """Llama a Gemini con contexto. sistema puede sobreescribirse (trivia).

    Envia el historial COMPLETO de la sesion (contents multi-turno) para que
    el copiloto recuerde el hilo de la charla. Acepta tanto el formato
    {'role','text'} como el legado {'rol','texto'}.
    """
    contents = [
        {"role": "user" if (h.get("role") or h.get("rol")) == "user"
         else "model",
         "parts": [{"text": str(h.get("text", h.get("texto", "")))[:900]}]}
        for h in (historial or [])
    ]
    contents.append({"role": "user", "parts": [{"text": mensaje[:1400]}]})
    ultimo_error = None
    for modelo in GEMINI_MODELOS:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{modelo}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={
                    "system_instruction": {
                        "parts": [{"text": sistema or _contexto_ia()}]},
                    "contents": contents,
                    "generationConfig": {
                        "maxOutputTokens": max_tokens,
                        "temperature": temperatura,
                        # Los flash "thinking" consumen tokens razonando:
                        # sin esto pueden agotar max_tokens y no devolver texto.
                        "thinkingConfig": {"thinkingBudget": 0},
                    },
                },
                timeout=20,
            )
            if r.status_code != 200:
                ultimo_error = f"HTTP {r.status_code}"
                continue
            data = r.json()
            texto = "".join(
                p.get("text", "")
                for c in data.get("candidates", [])
                for p in (c.get("content") or {}).get("parts", [])
            ).strip()
            if texto:
                return texto
        except requests.RequestException as exc:
            ultimo_error = str(exc)
    raise RuntimeError(f"Gemini no disponible ({ultimo_error})")


def _calle_mencionada(msg_norm):
    """Detecta si el mensaje nombra una arteria del dataset.

    Compara los tokens significativos de cada calle_1 (sin prefijos de via)
    contra los tokens del mensaje; gana la coincidencia mas larga.
    """
    tokens_msg = set(re.sub(r"[^A-Z0-9 ]", " ", msg_norm).split())
    mejor, mejor_len = None, 0
    for s in SINIESTROS:
        nombre = (s["calle_1"] or "").strip()
        if not nombre:
            continue
        raiz = set(
            t for t in normalizar(nombre).split() if t not in PREFIJOS_VIA)
        if raiz and raiz <= tokens_msg and len(raiz) > mejor_len:
            mejor, mejor_len = nombre.upper(), len(raiz)
    return mejor


def _stats_calle(nombre):
    """Estadisticas de una arteria concreta del dataset."""
    objetivo = normalizar(nombre)
    rows = [s for s in SINIESTROS if normalizar(s["calle_1"]) == objetivo]
    if not rows:
        return None
    n = len(rows)
    graves = sum(1 for r in rows if normalizar(r["gravedad_siniestro"]) == "GRAVE")
    franjas = {}
    for r in rows:
        p = r.get("periodo_dia_nombre") or "SIN DATOS"
        franjas[p] = franjas.get(p, 0) + 1
    return {
        "nombre": nombre,
        "n": n,
        "pct_grave": round(100 * graves / n, 1),
        "franja_pico": max(franjas.items(), key=lambda kv: kv[1])[0],
    }


def _respuesta_reglas(mensaje):
    """Fallback offline: responde con estadisticas reales del dataset."""
    m = normalizar(mensaje)
    s = _stats_globales()
    calles = ", ".join(f"{c} ({n})" for c, n in s["top_calles"][:4])

    # Si menciona una arteria concreta del dataset, responder con sus stats
    calle = _calle_mencionada(m)
    if calle:
        st = _stats_calle(calle)
        if st:
            return (
                f"{st['nombre']} registra {st['n']} siniestros en el dataset "
                f"({st['pct_grave']}% graves). Su franja más conflictiva es "
                f"{st['franja_pico']}. Circulá con precaución extra en sus "
                "intersecciones y reducí velocidad de noche.")

    # Preguntas por jurisdiccion: citar puntos criticos reales (no alucinar)
    if "MONTERRICO" in m or "MONTERICO" in m:
        pts = ", ".join(PUNTOS_CRITICOS["MONTERRICO"])
        return (f"En Monterrico los puntos a vigilar son: {pts}. El cruce "
                "RP 42 y RP 43 concentra el tránsito pesado del valle: "
                "reducí velocidad al pasar y de noche doblá la distancia "
                "de seguimiento.")
    if "CARMEN" in m:
        pts = ", ".join(PUNTOS_CRITICOS["EL CARMEN"])
        return (f"En El Carmen los puntos críticos son: {pts}. El empalme "
                "RN 9 con RP 42 mezcla tránsito rápido con circulación "
                "local: entrá y salí de la ruta con máxima precaución.")
    if "PERICO" in m:
        pts = ", ".join(PUNTOS_CRITICOS["PERICO"])
        return (f"En el casco urbano de Perico las arterias críticas son: "
                f"{pts}. Belgrano y Éxodo lideran el ranking de siniestros "
                "del observatorio: máxima atención en sus intersecciones.")

    if any(k in m for k in ("HOLA", "BUENAS", "BUEN DIA", "BUENAS TARDES")):
        return ("Hola, soy el copiloto del Observatorio Vial de los Valles. "
                "Puedo contarte cuáles son las calles más críticas, los "
                "horarios de mayor riesgo o darte consejos de prevención.")
    if "MOTO" in m or "MOTOCICLET" in m:
        return (f"Las motos concentran el {s['pct_moto']}% de los siniestros "
                f"del corredor. Franja más peligrosa: {s['peor_franja'][0]}. "
                "Recomendación: casco certificado, ropa reflectante de noche "
                "y extrema precaución en intersecciones.")
    if any(k in m for k in ("NOCHE", "NOCTURN", "HORARIO", "HORA", "FRANJA")):
        detalle = ", ".join(f"{p}: {n}" for p, n in s["periodos"])
        return (f"Distribución por franja horaria — {detalle} siniestros. "
                f"El tramo más crítico es {s['peor_franja'][0]}: si circulás "
                "en ese horario reducí velocidad en cruces y avenidas.")
    if any(k in m for k in ("CALLE", "ARTERIA", "PELIGROS", "CRITIC", "ZONA",
                            "DONDE", "LUGAR", "PUNTO")):
        return (f"Las arterias con más siniestros registrados son: {calles}. "
                "En el mapa, las zonas rojas del simulador marcan tramos con "
                "3 o más siniestros históricos a menos de 60 m.")
    if any(k in m for k in ("GRAVE", "MUERT", "FATAL", "LETAL", "VICTIMA")):
        return (f"El {s['pct_grave']}% de los siniestros registrados fueron "
                "graves (asistencia SAME). La gravedad se concentra en "
                "motociclistas, horario nocturno y vías de acceso/ruta.")
    if any(k in m for k in ("PREVENCION", "CONSEJO", "CUIDADO", "EVITAR",
                            "SEGUR")):
        return ("Prevención según el patrón local: no exceder velocidad en "
                "avenidas céntricas, doble precaución en la franja " +
                s["peor_franja"][0] + ", distancia extra detrás de motos y "
                "nunca circular alcoholizado. Las intersecciones con más "
                f"casos: {calles.split(', ')[0]}.")
    if any(k in m for k in ("RUTA", "RN", "ACCESO", "66")):
        return ("En vías de acceso/ruta los siniestros tienden a ser más "
                "graves por la mayor velocidad. Respetá los límites al salir "
                "del casco urbano y atención en las rotondas de acceso.")
    return (f"Resumen del observatorio: {s['total']} siniestros registrados "
            f"en Perico, Monterrico y El Carmen; {s['pct_grave']}% graves y "
            f"{s['pct_moto']}% con motos. La arteria más crítica es "
            f"{s['top_calles'][0][0]} ({s['top_calles'][0][1]} casos). "
            "Preguntame por calles, horarios, motos o prevención.")


@app.post("/api/chat-gemini")
def api_chat_gemini():
    """Chat del copiloto: Gemini si hay GEMINI_API_KEY, si no reglas del
    dataset (siempre con estadisticas reales)."""
    payload = request.get_json(silent=True) or {}
    mensaje = str(payload.get("mensaje") or "").strip()
    if not mensaje:
        return jsonify({"status": "error", "mensaje": "mensaje vacío"}), 400

    if GEMINI_API_KEY:
        try:
            return jsonify({
                "respuesta": _preguntar_gemini(
                    mensaje, payload.get("historial")),
                "fuente": "gemini",
            })
        except RuntimeError:
            pass  # cae al modo reglas sin cortar la conversación
    return jsonify({
        "respuesta": _respuesta_reglas(mensaje),
        "fuente": "reglas",
    })


# ================== MODULO LUDICO: Trivia + Simulador de decisiones ==================

def _banco_trivia():
    """Afirmaciones verdadero/falso construidas con cifras reales del dataset."""
    s = _stats_globales()
    top_calle, top_n = s["top_calles"][0]
    segunda_calle, seg_n = s["top_calles"][1]
    base = [
        {"pregunta": f"El {s['pct_moto']}% de los siniestros del corredor involucra motocicletas.",
         "es_verdadero": True,
         "explicacion": f"Exacto: las motos concentran el {s['pct_moto']}% de los {s['total']} siniestros registrados.",
         "fuente_dato": "Dataset: modo_desplazamiento_victima"},
        {"pregunta": "La mayoría de los siniestros ocurre de madrugada (00-06h).",
         "es_verdadero": False,
         "explicacion": f"Falso: la franja pico es {s['peor_franja'][0]}, con {s['peor_franja'][1]} casos.",
         "fuente_dato": "Dataset: periodo_dia_nombre"},
        {"pregunta": f"La edad promedio de las víctimas ronda los {int(s['edad_promedio'])} años.",
         "es_verdadero": True,
         "explicacion": f"Verdadero: la edad promedio calculada es {s['edad_promedio']} años — víctimas muy jóvenes.",
         "fuente_dato": "Dataset: edad_victima"},
        {"pregunta": "Los domingos concentran más siniestros que los días laborables.",
         "es_verdadero": False,
         "explicacion": f"Falso: el {s['pct_feria']}% de los siniestros ocurre en días de alta concurrencia comercial.",
         "fuente_dato": "Dataset: es_dia_feria"},
        {"pregunta": f"{top_calle.title()} es la arteria con más siniestros del corredor.",
         "es_verdadero": True,
         "explicacion": f"Verdadero: {top_calle.title()} lidera el ranking con {top_n} siniestros.",
         "fuente_dato": "Dataset: calle_1"},
        {"pregunta": "Los peatones concentran más siniestros que los motociclistas.",
         "es_verdadero": False,
         "explicacion": f"Falso: el modo más frecuente es {s['modo_top'][0]} con el {s['pct_moto']}%.",
         "fuente_dato": "Dataset: modo_desplazamiento_victima"},
        {"pregunta": "Más de la mitad de los siniestros ocurre en días de semana laborable.",
         "es_verdadero": True,
         "explicacion": f"Verdadero: el {s['pct_feria']}% ocurre en días de alta concurrencia comercial; el fin de semana solo {s['pct_fin_semana']}%.",
         "fuente_dato": "Dataset: es_dia_feria"},
        {"pregunta": "Los siniestros graves superan el 50% del total registrado.",
         "es_verdadero": False,
         "explicacion": f"Falso: los graves son el {s['pct_grave']}% del total — alarmante pero no mayoritario.",
         "fuente_dato": "Dataset: gravedad_siniestro"},
        {"pregunta": "Las camionetas son el modo de transporte más frecuente de las víctimas.",
         "es_verdadero": False,
         "explicacion": f"Falso: el modo dominante es {s['modo_top'][0].title()} ({s['pct_moto']}%).",
         "fuente_dato": "Dataset: modo_desplazamiento_victima"},
        {"pregunta": "Los fines de semana concentran más del 50% de los siniestros.",
         "es_verdadero": False,
         "explicacion": f"Falso: el fin de semana concentra solo el {s['pct_fin_semana']}%.",
         "fuente_dato": "Dataset: fin_semana"},
        {"pregunta": "La franja de 12 a 19 horas es la de mayor frecuencia siniestral.",
         "es_verdadero": s["peor_franja"][0].startswith("Tarde"),
         "explicacion": f"La franja más crítica real es {s['peor_franja'][0]} ({s['peor_franja'][1]} casos).",
         "fuente_dato": "Dataset: periodo_dia_nombre"},
        {"pregunta": "La asistencia del SAME se requirió en menos del 20% de los siniestros.",
         "es_verdadero": False,
         "explicacion": f"Falso: el SAME intervino en el {s['pct_same']}% de los casos.",
         "fuente_dato": "Dataset: asistencia_same"},
        {"pregunta": f"{top_calle.title()} registra más siniestros que {segunda_calle.title()}.",
         "es_verdadero": True,
         "explicacion": f"Verdadero: {top_n} vs {seg_n} siniestros registrados.",
         "fuente_dato": "Dataset: calle_1"},
        {"pregunta": f"El {s['pct_noche']}% de los siniestros ocurre entre las 19 y las 06 horas.",
         "es_verdadero": True,
         "explicacion": f"Verdadero: {s['pct_noche']}% del total ocurre en horario nocturno.",
         "fuente_dato": "Dataset: hora_num"},
        {"pregunta": "La mayoría de las víctimas de siniestros tiene más de 50 años.",
         "es_verdadero": False,
         "explicacion": f"Falso: la edad promedio es {s['edad_promedio']} años — predominan los jóvenes.",
         "fuente_dato": "Dataset: edad_victima"},
    ]
    return [{"dificultad": 1, **it} for it in base] + TRIVIA_INGENIERIA


# Preguntas de ingenieria vial y seguridad general, ordenadas por
# dificultad tecnica: 2 = avanzado, 3 = experto en transito.
TRIVIA_INGENIERIA = [
    {"pregunta": "Duplicar la velocidad duplica la distancia de frenado.",
     "es_verdadero": False,
     "explicacion": "Falso: la distancia de frenado crece con el cuadrado de la velocidad (d ≈ v²/2μg). Al doble de velocidad se necesita ~4 veces más distancia.",
     "fuente_dato": "Física del frenado",
     "dificultad": 2},
    {"pregunta": "Con calzada mojada, la distancia de frenado puede aumentar hasta un 50% o más a igual velocidad.",
     "es_verdadero": True,
     "explicacion": "Verdadero: el coeficiente de adherencia μ del asfalto húmedo cae frente al seco, y la frenada se estira en proporción inversa.",
     "fuente_dato": "Adherencia neumático-calzada",
     "dificultad": 2},
    {"pregunta": "La energía que debe disipar el cuerpo en un choque a 60 km/h es cuatro veces la de uno a 30 km/h.",
     "es_verdadero": True,
     "explicacion": "Verdadero: Ec = ½mv². El doble de velocidad cuadruplica la energía cinética a absorber en el impacto.",
     "fuente_dato": "Cinemática del choque",
     "dificultad": 2},
    {"pregunta": "Una moto es más visible para los autos que otro auto porque es más angosta y ágil.",
     "es_verdadero": False,
     "explicacion": "Falso: el frente angosto y el único faro hacen que la moto 'desaparezca' en los puntos ciegos del auto (pilar A y espejos) — causa clásica de giros con invasión.",
     "fuente_dato": "Puntos ciegos / detectabilidad",
     "dificultad": 2},
    {"pregunta": "Frenar fuerte con la moto inclinada en curva es tan seguro como frenar en recta.",
     "es_verdadero": False,
     "explicacion": "Falso: inclinado, parte de la adherencia disponible ya se gasta en la fuerza lateral de la curva; frenar agota el 'presupuesto de fricción' y produce la caída low-side.",
     "fuente_dato": "Dinámica de la moto",
     "dificultad": 2},
    {"pregunta": "Un camión cargado necesita más distancia para detenerse que un auto a igual velocidad.",
     "es_verdadero": True,
     "explicacion": "Verdadero: su masa es mucho mayor y la adherencia específica por rueda no escala igual; por eso invadir su distancia de seguridad es tan peligroso.",
     "fuente_dato": "Física del frenado",
     "dificultad": 2},
    {"pregunta": "La distancia total de detención incluye los metros recorridos durante el tiempo de reacción, antes de pisar el freno.",
     "es_verdadero": True,
     "explicacion": "Verdadero: con ~1 s de reacción, a 90 km/h se recorren ~25 m antes de que el freno empiece a actuar.",
     "fuente_dato": "Tiempo de reacción",
     "dificultad": 3},
    {"pregunta": "Al frenar fuerte, la rueda trasera de la moto gana adherencia y es la que más conviene bloquear.",
     "es_verdadero": False,
     "explicacion": "Falso: la frenada transfiere carga hacia adelante y alivia la trasera, que pierde adherencia y se bloquea fácil — por eso el freno delantero hace el 70-80% del trabajo.",
     "fuente_dato": "Transferencia de carga",
     "dificultad": 3},
    {"pregunta": "Un camión puede ocultar un auto completo en sus 'ángulos muertos' laterales y traseros.",
     "es_verdadero": True,
     "explicacion": "Verdadero: las 'no-zones' de un camión cubren carriles enteros; la regla práctica es: si no ves sus espejos, el chofer no te ve.",
     "fuente_dato": "Puntos ciegos de pesados",
     "dificultad": 3},
    {"pregunta": "El casco protege aumentando el tiempo de desaceleración de la cabeza en el impacto.",
     "es_verdadero": True,
     "explicacion": "Verdadero: la carcasa y el liner extienden el Δt del impacto, reduciendo el pico de fuerza sobre el cráneo (impulso = F·Δt).",
     "fuente_dato": "Biomecánica del impacto",
     "dificultad": 3},
    {"pregunta": "A velocidad de ruta, para doblar a la derecha en moto hay que empujar el manubrio hacia la derecha (contramanillar).",
     "es_verdadero": True,
     "explicacion": "Verdadero: por encima de ~30 km/h rige el contravolante — empujar el manillar del lado del giro inclina la moto hacia esa curva.",
     "fuente_dato": "Dinámica de la moto",
     "dificultad": 3},
    {"pregunta": "El aquaplaning ocurre cuando una cuña de agua levanta el neumático y rompe el contacto con el asfalto.",
     "es_verdadero": True,
     "explicacion": "Verdadero: a partir de cierta velocidad el dibujo no alcanza a evacuar el agua y el vehículo flota — frenar o girar ahí es perder el control.",
     "fuente_dato": "Adherencia hidrodinámica",
     "dificultad": 3},
    {"pregunta": "El pilar A del parabrisas nunca puede ocultar a un motociclista que se acerca a una intersección.",
     "es_verdadero": False,
     "explicacion": "Falso: el pilar A genera un cono de sombra donde una moto entera queda oculta; por eso hay que mover la cabeza al mirar antes de doblar.",
     "fuente_dato": "Puntos ciegos / intersecciones",
     "dificultad": 3},
    {"pregunta": "En moto, usar solo el freno trasero detiene casi igual que usar ambos frenos.",
     "es_verdadero": False,
     "explicacion": "Falso: la rueda trasera se alivia en la frenada y aporta ~20-30%; usar solo ese freno casi duplica la distancia de detención.",
     "fuente_dato": "Reparto de frenada",
     "dificultad": 3},
    {"pregunta": "En una rotonda sin semáforo tiene prioridad el vehículo que ya circula dentro de la glorieta.",
     "es_verdadero": True,
     "explicacion": "Verdadero: la Ley 24.449 da prioridad al circulante dentro de la rotonda; quien ingresa debe ceder el paso.",
     "fuente_dato": "Ley 24.449 — prioridades",
     "dificultad": 2},
    {"pregunta": "Al adelantar a un ciclista la ley exige una separación lateral mínima de 1,5 metros.",
     "es_verdadero": True,
     "explicacion": "Verdadero: el art. 42 bis de la Ley 24.449 exige 1,5 m al adelantar peatones, ciclistas y motoristas (o reducir la velocidad).",
     "fuente_dato": "Ley 24.449 art. 42 bis",
     "dificultad": 2},
    {"pregunta": "Ante un paso a nivel sin barreras ni señales se puede cruzar a la velocidad de la vía.",
     "es_verdadero": False,
     "explicacion": "Falso: la norma exige velocidad precautoria y detenerse si es necesario hasta verificar que no viene el tren — el tren no puede frenar.",
     "fuente_dato": "Ley 24.449 — cruces ferroviarios",
     "dificultad": 2},
    {"pregunta": "Si el semáforo está apagado o en intermitente, la bocacalle funciona con prioridad de la derecha.",
     "es_verdadero": True,
     "explicacion": "Verdadero: con señalización inoperante rigen las reglas generales — hay que ceder al vehículo que viene por la derecha.",
     "fuente_dato": "Ley 24.449 — prioridades",
     "dificultad": 2},
    {"pregunta": "Un camión que dobla a la derecha deja siempre un carril seguro entre su lateral y el cordón.",
     "es_verdadero": False,
     "explicacion": "Falso: ese hueco es el ángulo muerto del acoplado y se cierra al completar el giro — la 'barreda' es la trampa clásica de los giros de pesados.",
     "fuente_dato": "Zonas ciegas de pesados",
     "dificultad": 3},
    {"pregunta": "En una frenada de emergencia con ABS hay que 'bombear' el pedal para que el sistema funcione.",
     "es_verdadero": False,
     "explicacion": "Falso: el ABS ya modula la presión ~15 veces por segundo; se pisa firme y sostenido. Bombear alarga la frenada.",
     "fuente_dato": "Sistemas de frenado",
     "dificultad": 3},
    {"pregunta": "Usar el celular al volante multiplica ~4 veces el riesgo de siniestro, comparable a conducir con alcohol en sangre.",
     "es_verdadero": True,
     "explicacion": "Verdadero: la distracción por celular iguala el deterioro de tiempo de reacción de ~0,5 g/l de alcohol — por eso está prohibido en todo momento.",
     "fuente_dato": "Distracción al conducir",
     "dificultad": 2},
    {"pregunta": "Con luces altas se ve mejor en niebla densa porque el haz es más potente.",
     "es_verdadero": False,
     "explicacion": "Falso: la luz alta rebota en las gotas y forma una pared blanca: en niebla se usan las bajas (y balizas si es extrema) con velocidad muy reducida.",
     "fuente_dato": "Conducción con niebla",
     "dificultad": 2},
]


def _mito_gemini(usados, nivel=1):
    """Pide a Gemini un mito en JSON estricto; None si falla."""
    sistema = (
        "Sos un generador de trivia de seguridad vial para el Observatorio de "
        "Siniestralidad de los Valles de Jujuy (Perico, Monterrico, El "
        "Carmen). Respondé SOLO con un objeto JSON valido, sin markdown ni "
        "texto extra, con las claves: pregunta (afirmacion corta para "
        "verdadero/falso), es_verdadero (booleano), explicacion (1-2 "
        "oraciones), fuente_dato (origen breve, ej. 'Dataset local' o "
        "'Seguridad vial general'). Mezclá afirmaciones verdaderas y falsas "
        "plausibles sobre el corredor vial jujeño.")
    dificultad = {
        1: "basica, para principiantes (mitos comunes de seguridad vial)",
        2: "intermedia (fisica del frenado, adherencia, visibilidad)",
        3: "tecnica avanzada (cinematica del choque, dinamica de la moto, "
           "puntos ciegos de vehiculos pesados)",
    }.get(nivel, "basica")
    previas = "; ".join(usados[-4:]) if usados else "ninguna"
    prompt = (f"Generá UN mito/afirmacion NUEVO de dificultad {dificultad} "
              f"(no repitas estos: {previas}).")
    try:
        texto = _preguntar_gemini(prompt, sistema=sistema,
                                max_tokens=500, temperatura=0.9)
        datos = json.loads(re.sub(r"^```(?:json)?|```$", "",
                                  texto.strip(), flags=re.M).strip())
        if isinstance(datos.get("es_verdadero"), bool) and datos.get("pregunta"):
            return datos
    except (RuntimeError, json.JSONDecodeError, AttributeError):
        return None
    return None


@app.post("/api/trivia-mitos")
def api_trivia_mitos():
    """Tarjeta Verdadero/Falso sin repetir (el cliente envia 'usados')."""
    payload = request.get_json(silent=True) or {}
    usados = payload.get("usados") or []
    try:
        nivel = min(max(int(payload.get("nivel") or 1), 1), 3)
    except (TypeError, ValueError):
        nivel = 1

    if GEMINI_API_KEY:
        datos = _mito_gemini(usados, nivel)
        if datos:
            return jsonify({
                "pregunta": str(datos["pregunta"]),
                "es_verdadero": bool(datos["es_verdadero"]),
                "explicacion": str(datos.get("explicacion") or ""),
                "fuente_dato": str(datos.get("fuente_dato") or "Gemini"),
                "id": f"g-{random.randrange(10**6)}",
                "fuente": "gemini",
                "dificultad": nivel,
            })

    banco = _banco_trivia()
    disponibles = [(i, b) for i, b in enumerate(banco)
                   if f"b{i}" not in usados]
    if not disponibles:
        # Banco agotado en la partida: remezclar todo (modo infinito,
        # la partida solo termina al perder las 3 vidas).
        disponibles = list(enumerate(banco))
    # A mayor nivel (racha del jugador) se priorizan preguntas de mayor
    # dificultad tecnica; si se agotan, degrada a las disponibles.
    ideal = [x for x in disponibles if x[1].get("dificultad", 1) == nivel]
    candidatos = (ideal
                  or [x for x in disponibles
                      if x[1].get("dificultad", 1) <= nivel]
                  or disponibles)
    i, item = random.choice(candidatos)
    return jsonify({**item, "id": f"b{i}", "fuente": "banco"})


# ------------- Simulador de decisiones: situaciones por modo -------------

def _sit(titulo, lugar, texto, op_a, op_b, dificultad=1, modo="MOTO"):
    """op_*: (texto, es_segura, impacto, explicacion).
    dificultad: 1 principiante, 2 avanzado, 3 experto en transito.
    modo: vehiculo protagonista (para stats reales del feedback)."""
    return {"titulo": titulo, "lugar": lugar, "situacion": texto,
            "dificultad": dificultad, "modo": modo,
            "opciones": [
                {"texto": op_a[0], "segura": op_a[1], "impacto": op_a[2],
                 "explicacion": op_a[3]},
                {"texto": op_b[0], "segura": op_b[1], "impacto": op_b[2],
                 "explicacion": op_b[3]},
            ]}


SITUACIONES = {
    "MOTO": [
        _sit("Rotonda de noche con llovizna", "Rotonda Belgrano, Perico",
             "Viernes 22 h, llovizna fina. Entrás a la rotonda y un auto viene rápido por tu izquierda sin señalizar.",
             ("Reducís, cedés el paso y esperás el hueco", True, "+90%",
              "En la rotación con calzada húmeda la frenada de una moto es crítica: ceder evita el siniestro típico de este punto."),
             ("Acelerás para salir de la rotonda antes", False, "-45%",
              "Acelerar en piso mojado dentro de una rotonda es la combinación con más despistes y caídas del dataset.")),
        _sit("Semáforo en amarillo", "Av. Éxodo y 9 de Julio",
             "Sábado 23 h, el semáforo pasa a amarillo a 40 m del cruce.",
             ("Frenás y esperás el próximo verde", True, "+85%",
              "Las colisiones en intersección semaforizada se dan cuando se 'corre' el amarillo: la espera cuesta 40 segundos."),
             ("Acelerás para alcanzar a pasar", False, "-50%",
              "Cruzar en rojo te expone al tránsito lateral que arranca con verde: choque lateral, el más grave para motos.")),
        _sit("El atajo oscuro", "Vuelta a casa, casco urbano",
             "Domingo 1 h. Tenés dos caminos: un atajo de callejón oscuro o la avenida iluminada, 6 minutos más larga.",
             ("La avenida iluminada aunque demore más", True, "+80%",
              "La iluminación y el tránsito residual reducen despistes y asaltos: el riesgo por metro es mucho menor."),
             ("El atajo oscuro para llegar rápido", False, "-55%",
              "Calles oscuras de madrugada concentran siniestros solitarios sin testigos: la demora en auxilio agrava el resultado.")),
        _sit("Frenada de emergencia en bajada", "Bajada con gravilla suelta",
             "Bajás una pendiente a 45 km/h y un perro cruza. Hay gravilla suelta en la banquina.",
             ("Frenás progresivo con ambos frenos, moto derecha", True, "+85%",
              "Con la moto vertical la adherencia se reparte entre ambas ruedas: el delantero aporta el 70% sin bloquear."),
             ("Clavás el freno trasero y doblás a la banquina", False, "-60%",
              "Bloquear la trasera sobre gravilla desliza la rueda y te lleva a la banquina igual: low-side casi seguro."),
             3),
        _sit("El camión que dobla", "Cruce con camión de acarreo",
             "Un camión lento ocupa carril y medio para doblar a la derecha; queda un hueco junto al cordón.",
             ("Esperás atrás hasta que complete la maniobra", True, "+80%",
              "El hueco que deja el camión al doblar es trampa: se cierra cuando el trailer 'corta' la curva."),
             ("Te metés por el hueco del cordón para pasarlo", False, "-65%",
              "El interior de la curva de un camión es su peor ángulo muerto: te vuelve invisible justo cuando el trailer barre ese espacio."),
             2),
    ],
    "BICICLETA": [
        _sit("Hora pico en la avenida", "Av. San Martín, 18 h",
             "Circulás en hora pico: la vereda va llena de gente y la calzada está pesada pero ordenada.",
             ("Calzada con casco, luz y distancia", True, "+75%",
              "En bicicleta la vereda peatonal no es refugio: los siniestros bici-peatón y los enganchones en bordillos son frecuentes."),
             ("La vereda esquivando peatones", False, "-35%",
              "Zigzaguear entre peatones multiplica el riesgo de caídas y choques con quienes salen de los negocios.")),
        _sit("De noche sin luces", "Regreso a barrio, 21 h",
             "Se te quedó la luz trasera. Faltan 15 cuadras.",
             ("Caminás guiando la bici por la vereda", True, "+95%",
              "Sin luz trasera sos invisible para los autos que te superan: caminar 15 minutos es un precio barato."),
             ("Pedaleás igual, son pocos minutos", False, "-60%",
              "La mayoría de los siniestros nocturnos con bici son embestidas por alcance: sin luz el auto no te calcula.")),
        _sit("El colectivo detenido", "Parada de colectivo, centro",
             "Un colectivo frena en la parada tapando toda tu visión hacia adelante.",
             ("Esperás detrás o lo rodeás señalizando", True, "+80%",
              "Detrás del colectivo no ves ni te ven: el peatón que baja por delante y el auto que viene son puntos ciegos."),
             ("Lo pasás por la derecha junto al cordón", False, "-50%",
              "El hueco junto al cordón es donde bajan los pasajeros y donde el colectivo arranca: trampa clásica.")),
        _sit("Lluvia recién caída", "Avenida con pintura resbalosa",
             "Primeras gotas después de días secos: la calzada tiene manchas de aceite y líneas de señalización mojadas.",
             ("Reducís y evitás pisar líneas y manchas oscuras", True, "+80%",
              "La primera lluvia emulsiona el aceite acumulado: la adherencia cae al mínimo justo sobre pintura y manchas."),
             ("Mantenés ritmo, la bici frena igual con lluvia fina", False, "-55%",
              "Los frenos de llanta mojados pierden potencia y la cubierta patina sobre la película de agua+aceite."),
             2),
        _sit("Puerta que se abre", "Calle angosta con autos estacionados",
             "Circulás pegado a los autos estacionados para dejar pasar el tránsito.",
             ("Dejás un metro de puerta aunque te apuren", True, "+85%",
              "El 'dooring' es el siniestro urbano más evitable: un metro de distancia anula la puerta sorpresa."),
             ("Pegado a los autos: el hueco es seguro", False, "-50%",
              "La puerta que se abre mide ~1 m: dentro de esa franja no hay tiempo de reacción posible."),
             2),
    ],
    "PEATON": [
        _sit("Cruce a mitad de cuadra", "Av. Belgrano, 19 h",
             "El negocio queda justo enfrente, pero el semáforo está a media cuadra.",
             ("Media cuadra extra hasta el cruce habilitado", True, "+90%",
              "Los atropellos peatonales se concentran en cruces a mitad de cuadra: el auto no te espera ahí."),
             ("Cruzás directo entre los autos frenados", False, "-65%",
              "El auto detenido en un carril te tapa la visión del siguiente: la 'trampa del segundo carril' es letal.")),
        _sit("Vuelta de la feria de noche", "Zona Feria Minorista",
             "Salís de la feria a las 21 h con bolsas. La vereda iluminada da la vuelta larga; la cuneta oscura es directa.",
             ("La vereda iluminada, aunque más larga", True, "+85%",
              "Ser visto es la mitad de la seguridad peatonal: en oscuridad los conductores detectan tarde a personas con bolsas."),
             ("La cuneta oscura para llegar antes", False, "-55%",
              "Caminar por la banquina oscura de espaldas al tránsito te vuelve invisible hasta último momento.")),
        _sit("Mensaje al cruzar", "Esquina Av. Villafañe",
             "Te llega un mensaje importante justo cuando vas a cruzar la avenida.",
             ("Guardás el celular y cruzás atento", True, "+95%",
              "Cruzar mirando el celular reduce a cero tu margen de reacción ante el auto que dobla sin frenar."),
             ("Contestás caminando, ya conocés la esquina", False, "-45%",
              "La familiaridad engaña: con la vista en el celular no registrás ni el auto que dobla ni el que pasa en amarillo.")),
        _sit("Avenida de doble mano, primer auto frenado", "Avenida ancha, 13 h",
             "Un auto frena en el primer carril para dejarte pasar a mitad de cuadra. El segundo carril sigue libre.",
             ("No cruzás hasta ver el segundo carril completo", True, "+90%",
              "La 'trampa del segundo carril': el auto frenado te tapa al vehículo que viene por el otro carril y a él lo tapás vos."),
             ("Cruzás rápido, el conductor te hizo seña", False, "-60%",
              "La seña del primer auto no controla el segundo carril: los atropellos así son de los más graves del dataset."),
             3),
        _sit("Ruta sin banquina iluminada", "Ruta Provincial 47, 21 h",
             "Debés caminar 800 m por la banquina de la ruta. No hay luz y el celular tiene poca batería.",
             ("Del lado contrario al tránsito, linterna y distancia del borde", True, "+85%",
              "Caminar de frente a los vehículos te da los segundos de reacción que la oscuridad roba; la linterna te hace visible a +150 m."),
             ("Del lado del tránsito, a oscuras y al borde", False, "-65%",
              "De espaldas al tránsito y sin luz sos invisible hasta que el auto está encima: el margen de maniobra se anula."),
             2),
    ],
    "AUTO": [
        _sit("Hora pico en la Terminal", "Salida de la Terminal, 18 h",
             "El tránsito sale lento de la terminal y tenés apuro por llegar.",
             ("Esperás el hueco y salís ordenado", True, "+85%",
              "En zonas de terminal los siniestros son por maniobras ansiosas: esperar cuesta minutos, chocar cuesta más."),
             ("Zigzagueás entre colectivos y taxis", False, "-40%",
              "Los cambios de carril bruscos entre colectivos detenidos son la causa típica de choques en este punto.")),
        _sit("Ruta de noche", "Ruta Provincial 47, 23 h",
             "Ruta despejada, río de autos a 90 km/h. Tu velocidad crucero habitual.",
             ("Bajás a 80 y aumentás la distancia", True, "+80%",
              "En ruta los siniestros son menos pero mucho más graves: velocidad menor = energía de impacto mucho menor."),
             ("Mantenés 90-100, la ruta está libre", False, "-50%",
              "La ruta 'libre' es la trampa: cruce de fauna, un pozo o una maniobra a 100 km/h no deja margen.")),
        _sit("Estacionar en la esquina", "Centro de Perico, sábado",
             "Solo hay lugar en la boca de la esquina, tapando la visión del cruce.",
             ("Media cuadra más allá, dejando la esquina libre", True, "+90%",
              "El auto estacionado en la esquina crea puntos ciegos para todos los que doblan: generás el siniestro de otro."),
             ("En la esquina igual, son unos minutos", False, "-40%",
              "Cinco minutos bloqueando la visibilidad bastan para que alguien doble a ciegas y choque.")),
        _sit("Caravana detrás del camión", "Ruta Nacional 9, lluvia leve",
             "Seguís a un camión a ~15 m para 'aprovechar la estela'; el spray de sus ruedas te empapa el parabrisas.",
             ("Abrís distancia a 3 segundos o más y bajás 10 km/h", True, "+80%",
              "El spray del camión te deja medio ciego y su distancia de frenado es mayor: la estela ahorra nafta y compra siniestro."),
             ("Te quedás cerca: si él frena, frenás vos también", False, "-55%",
              "A 90 km/h tu tiempo de reacción ya consumió ~25 m; con visibilidad por spray el margen es negativo."),
             3),
        _sit("Cruce ciego en barrio", "Calle sin señalizar, casco urbano",
             "Llegás a una esquina sin semáforo ni carteles, con setos altos que tapan ambos laterales.",
             ("Casi detenerte, asomar metro a metro y pasar", True, "+85%",
              "En cruce sin prioridad la regla es ceder al que viene por la derecha: asomar despacio te hace visible antes de exponerte."),
             ("Pasás firme: el de la derecha te tiene que ver", False, "-50%",
              "Los setos crean un punto ciego mutuo: si el otro hace lo mismo, se encuentran en el medio del cruce."),
             2),
    ],
}


# Banco universal de dilemas tecnicos (Ley 24.449 + fisica vial): se usa en
# el modo supervivencia infinito, mezclado con los bancos por vehiculo.
# Cada uno lleva 'modo' para que el feedback cite stats reales del dataset.
SITUACIONES_UNIVERSALES = [
    _sit("Prioridad real en la rotonda", "Rotonda Acceso Sur RN 66",
         "Vas a ingresar a una rotonda y un auto circula por la calzada circular acercándose por tu izquierda. ¿Quién pasa primero?",
         ("Vos, porque tenés prioridad reglamentaria de paso por la derecha", False, "-55%",
          "Falso: la prioridad de la derecha NO aplica dentro de una rotonda — el que ya circula no está 'a tu derecha' en el sentido legal, ambos viajan en el mismo sentido giratorio."),
         ("El vehículo que ya está circulando dentro del anillo giratorio", True, "+88%",
          "Correcto: el art. 43 de la Ley 24.449 prioriza al circulante dentro de la glorieta; quien ingresa debe ceder el paso sin importar la arteria por la que llegue."),
         3, "AUTO"),
    _sit("Sobrepaso de un ciclista", "Av. La Bandera hacia RN 9",
         "Vas en auto por avenida y adelante circula un ciclista por el borde. Para sobrepasarlo en forma segura:",
         ("Tocar bocina corta para avisarle y rebasarlo manteniendo 0,5 m de margen dentro del mismo carril", False, "-65%",
          "Incorrecto doblemente: la bocina imprevista desestabiliza al ciclista y 0,5 m es distancia de roce — la norma exige 1,5 m mínimos, lo que implica ocupar el carril vecino."),
         ("Cambiar de carril o abrirse dejando un margen lateral mínimo de 1,5 m, sin bocina imprevista", True, "+85%",
          "Correcto: el art. 42 bis de la Ley 24.449 exige 1,5 m de separación lateral al adelantar ciclistas, peatones y motoristas; si no hay espacio, se espera."),
         3, "BICICLETA"),
    _sit("Hidroplaneo (aquaplaning)", "RN 66, lluvia torrencial",
         "Empieza una lluvia torrencial y sentís que la dirección del auto 'flota', perdiendo contacto con el asfalto. ¿Qué maniobra corresponde?",
         ("Clavar el freno a fondo para detener el vehículo antes de perder la trayectoria", False, "-70%",
          "Incorrecto: con el neumático 'flotando' sobre la cuña de agua, frenar en seco bloquea y rompe el poco contacto que queda — producís el trompo que querías evitar."),
         ("Soltar suavemente el acelerador sin frenar brusco ni girar el volante hasta recuperar adherencia", True, "+80%",
          "Correcto: en aquaplaning la única recuperación es reducir velocidad sin inputs bruscos; al recuperar adherencia el auto responde solo. Frenar o girar agrava el desliz."),
         3, "AUTO"),
    _sit("Pendiente angosta: ¿quién retrocede?", "Calle de sierra en subida",
         "En una calle angosta con fuerte pendiente donde no entran dos vehículos, se encuentran de frente. ¿Quién debe retroceder?",
         ("El vehículo que va subiendo, porque tiene mejor visibilidad", False, "-50%",
          "Incorrecto: la regla prioriza al que sube porque retroceder en pendiente ascendente es la maniobra más difícil e insegura — no se le exige al que ya la está sorteando."),
         ("El vehículo que va descendiendo, salvo que lleve acoplado", True, "+85%",
          "Correcto: en pendientes la prioridad la tiene quien asciende; retrocede el que desciende — excepto si arrastra acoplado o semirremolque, donde el retroceso propio es más riesgoso."),
         3, "AUTO"),
    _sit("Semáforo amarillo intermitente", "Av. Éxodo, cruce nocturno",
         "Llegás a un cruce con semáforo en amarillo titilante:",
         ("Podés cruzar con prioridad si no ves luces en la transversal", False, "-55%",
          "Incorrecto: el amarillo intermitente NO concede prioridad — solo advierte precaución, y la transversal puede tener rojo intermitente y conservar su paso."),
         ("Equivale a cartel de 'Ceda el paso': aminorar a velocidad de paso de hombre y cruzar solo si la vía está despejada", True, "+85%",
          "Correcto: la Ley 24.449 equipara el amarillo intermitente a una señal de precaución/ceda el paso — se cruza a velocidad mínima cediendo a quien llega primero o por la derecha."),
         3, "AUTO"),
    _sit("El hueco que deja el camión al doblar", "Empalme RN 9 y RP 42",
         "Un camión con acoplado abre a la izquierda para doblar a la derecha. Queda un hueco entre su lateral y el cordón.",
         ("Esperás atrás hasta que complete el giro", True, "+80%",
          "El interior de la curva de un camión es su peor ángulo muerto: al completar el giro el trailer barre ese hueco — la 'barreda' te aplasta contra el cordón."),
         ("Te metés por el hueco: total él ya te vio", False, "-65%",
          "El chofer no te ve: los espejos cubren el lateral pero no el ángulo de barrido del acoplado. Es el siniestro característico de los giros de pesados."),
         3, "CAMION"),
    _sit("Paso a nivel sin barreras", "Paso a nivel, zona rural",
         "Llegás a un paso a nivel sin barreras ni luces. A lo lejos parece no venir nada.",
         ("Bajás a paso de hombre, mirás a ambos lados y cruzás", True, "+90%",
          "La norma exige velocidad precautoria y detener la marcha si es necesario hasta verificar que no se aproxima el tren: el tren no puede frenar, vos sí."),
         ("Cruzás a la velocidad de la ruta, total parece libre", False, "-70%",
          "La perspectiva engaña la velocidad del tren: a 90 km/h no alcanzás a corregir si aparece. Los siniestros en pasos a nivel suelen ser fatales."),
         3, "CAMION"),
    _sit("Filtrar entre carriles en la moto", "Av. San Martín, hora pico",
         "El tránsito está detenido en dos carriles. Entre los autos queda un canal para avanzar hasta el semáforo.",
         ("Esperás en tu carril: filtrar entre carriles no está permitido", True, "+75%",
          "La normativa argentina no habilita la circulación entre carriles: las puertas que se abren y los cambios de carril imprevistos lo convierten en la maniobra urbana más riesgosa de la moto."),
         ("Filtrás despacio: a baja velocidad es seguro", False, "-55%",
          "Aunque sea despacio, el auto que cambia de carril sin esperarte te deja sin escapatoria entre vehículos — no hay distancia de escape posible."),
         2, "MOTO"),
    _sit("La puerta que se abre", "Calle comercial, autos estacionados",
         "Circulás pegado a la fila de autos estacionados y una puerta delantera empieza a abrirse.",
         ("Circulás siempre a ~1 m de los estacionados, mirando ruedas y espejos", True, "+80%",
          "La 'door zone' mata: el espacio que ocupa una puerta abierta es el peor carril. Anticipar la maniobra mirando ruedas/espejos da los metros de escape."),
         ("Clavás los frenos pegado a la puerta", False, "-45%",
          "Frenar a centímetros de la puerta te deja caer o te pone debajo del vehículo siguiente: la distancia preventiva, no la reacción, es lo que salva."),
         2, "BICICLETA"),
    _sit("Niebla en el corredor", "RP 42, 6:30 h, niebla densa",
         "Niebla cerrada: visibilidad de ~40 m. Encendiste las luces altas para 'ver mejor'.",
         ("Luces BAJAS + velocidad muy reducida + distancia extra", True, "+85%",
          "La luz alta rebota en las gotas de niebla y te ciega más: con bajas, balizas si hace falta y velocidad precautoria proporcional a los ~40 m de visión."),
         ("Altas y velocidad normal: con luz alcanza", False, "-50%",
          "Las altas crean una pared blanca frente al parabrisas y el peatón o vehículo detenido aparece a 40 m: a 80 km/h recorrés 22 m por segundo."),
         2, "AUTO"),
    _sit("La carga que sobresale", "Camioneta con caños largos",
         "Vas a transportar caños de 5 m en una camioneta: sobresalen más de medio metro por atrás de la caja.",
         ("Señalizás con bandera V10/paño rojo y lo trasladás de día", True, "+80%",
          "La norma exige señalizar toda carga que sobresalga: sin señalización, la moto que viene atrás no calcula el 'remo' invisible que le viene encima."),
         ("Lo llevás igual a baja velocidad, son pocos km", False, "-50%",
          "Aun a 40 km/h el extremo sobresaliente queda a la altura del pecho de un motociclista: sin señalización es una trampa invisible."),
         3, "CAMIONETA"),
]


def _stats_modo(modo):
    """Stats del dataset filtradas por modo de desplazamiento."""
    rows = [s for s in SINIESTROS if s["_modo"] == modo]
    if not rows:
        return {"n": 0, "pct_grave": 0, "top_calle": "-", "franja_pico": "-"}
    n = len(rows)
    graves = sum(1 for r in rows if normalizar(r["gravedad_siniestro"]) == "GRAVE")
    calles, franjas = {}, {}
    for r in rows:
        c = (r["calle_1"] or "").strip().upper()
        calles[c] = calles.get(c, 0) + 1
        p = r.get("periodo_dia_nombre") or "SIN DATOS"
        franjas[p] = franjas.get(p, 0) + 1
    return {
        "n": n,
        "pct_grave": round(100 * graves / n, 1),
        "top_calle": max(calles.items(), key=lambda kv: kv[1])[0].title(),
        "franja_pico": max(franjas.items(), key=lambda kv: kv[1])[0],
    }


def _hora_texto(texto):
    """Primera hora explicita (0-23) mencionada en el texto, o None."""
    for m in re.finditer(r"(\d{1,2})\s*h", texto):
        h = int(m.group(1))
        if 0 <= h <= 23:
            return h
    return None


def _stats_modo_nocturno(modo):
    """Stats del modo restringidas estrictamente al horario nocturno (19-06h).

    Se usa cuando el dilema ocurre de noche: mostrar la franja pico global
    del modo (que suele ser 'Tarde') contradice la situacion planteada.
    """
    rows = [s for s in SINIESTROS
            if s["_modo"] == modo and (s["_hora"] >= 19 or s["_hora"] < 6)]
    if not rows:
        return None
    total_modo = sum(1 for s in SINIESTROS if s["_modo"] == modo)
    n = len(rows)
    graves = sum(1 for r in rows if normalizar(r["gravedad_siniestro"]) == "GRAVE")
    calles = {}
    for r in rows:
        c = (r["calle_1"] or "").strip().upper()
        calles[c] = calles.get(c, 0) + 1
    return {
        "n": n,
        "pct_del_modo": round(100 * n / total_modo, 1) if total_modo else 0,
        "pct_grave": round(100 * graves / n, 1),
        "top_calle": max(calles.items(), key=lambda kv: kv[1])[0].title(),
    }


def _stats_edad(edad):
    """Porcentaje de victimas y de gravedad para el rango etario del jugador."""
    def en_rango(e):
        return ((edad < 18 and e < 18) or (18 <= edad < 30 and 18 <= e < 30)
                or (30 <= edad < 46 and 30 <= e < 46)
                or (edad >= 46 and e >= 46))

    tot = n_r = g_r = 0
    for s in SINIESTROS:
        try:
            e = float(s.get("edad_victima") or 0)
        except (TypeError, ValueError):
            continue
        if not (3 <= e <= 100):
            continue
        tot += 1
        if en_rango(e):
            n_r += 1
            g_r += normalizar(s["gravedad_siniestro"]) == "GRAVE"

    nombre = ("menores de 18" if edad < 18 else "18-29" if edad < 30
              else "30-45" if edad < 46 else "46+")
    return {
        "rango": nombre,
        "pct_victimas": round(100 * n_r / tot, 1) if tot else 0,
        "pct_grave": round(100 * g_r / n_r, 1) if n_r else 0,
    }


def _desafio_gemini(usados, nivel=1):
    """Genera un dilema tecnico nuevo con Gemini (modo infinito)."""
    sistema = (
        "Sos un generador de dilemas de ingenieria vial y Ley Nacional de "
        "Transito 24.449 para el Observatorio de Siniestralidad de los "
        "Valles de Jujuy (Perico, Monterrico, El Carmen, San Salvador). "
        "Respondé SOLO con un objeto JSON valido, sin markdown ni texto "
        "extra, con las claves: titulo, lugar (un cruce o tramo real del "
        "corredor jujeño), situacion (2-3 oraciones tecnicas), modo "
        "(MOTO | AUTO | CAMIONETA | BICICLETA | PEATON | CAMION), opciones "
        "(lista de EXACTAMENTE 2 objetos con claves texto, segura "
        "(booleano), impacto ('+80%' o '-50%'), explicacion (la norma vial "
        "o el dato tecnico que la fundamenta)). Exactamente UNA opcion "
        "debe ser reglamentariamente correcta; la otra debe sonar "
        "verosimil pero estar mal. Prioriza: prioridades de paso, "
        "distancias de seguridad, fisica del frenado, zonas ciegas de "
        "vehiculos pesados, manejo defensivo en mojado/niebla.")
    dificultad = {1: "intermedia", 2: "alta", 3: "muy alta"}.get(nivel, "alta")
    previas = "; ".join(str(u) for u in usados[-6:]) or "ninguna"
    prompt = (f"Generá UN dilema de dificultad {dificultad} "
              f"(no repitas estos: {previas}).")
    try:
        texto = _preguntar_gemini(prompt, sistema=sistema,
                                  max_tokens=900, temperatura=0.9)
        datos = json.loads(re.sub(r"^```(?:json)?|```$", "",
                                  texto.strip(), flags=re.M).strip())
        ops = datos.get("opciones")
        if (isinstance(ops, list) and len(ops) == 2
                and sum(1 for o in ops if o.get("segura")) == 1
                and datos.get("situacion")):
            return datos
    except (RuntimeError, json.JSONDecodeError, AttributeError, TypeError):
        return None
    return None


def _stats_dilema(modo, edad, sit):
    """Stats reales del modo para el feedback post-respuesta."""
    stats = {"modo": modo, **_stats_modo(modo)}
    if edad:
        stats["edad_rango"] = _stats_edad(edad)
    # Si el dilema ocurre de noche, el feedback se contextualiza solo con
    # datos nocturnos (19-06h): evita contradicciones tipo 'peor franja tarde'.
    hora_sit = _hora_texto(f"{sit['lugar']} {sit['situacion']}")
    if hora_sit is not None and (hora_sit >= 19 or hora_sit < 6):
        noche = _stats_modo_nocturno(modo)
        if noche:
            stats["nocturno"] = {"hora": hora_sit, **noche}
    return stats


_DILEMA_IA_SISTEMA = (
    "Eres un evaluador de ingenieria de transito y peritaje vial segun la "
    "Ley Nacional de Transito de Argentina (Ley 24.449). Genera un dilema "
    "vial extremo y complejo para un conductor adulto experimentado. "
    "REGLAS CRITICAS DE CALIDAD: 1. PROHIBIDAS las opciones absurdas o de "
    "sentido comun evidente (nada de 'chocar vs frenar', 'mirar el "
    "celular', 'pasar por el cordon'). 2. Ambas opciones (A y B) deben "
    "parecer correctas, logicas y ser maniobras que conductores "
    "experimentados realizan habitualmente. 3. Solo UNA opcion es "
    "reglamentaria y tecnicamente valida segun la normativa o la fisica "
    "del frenado. 4. Temas a cubrir aleatoriamente: prioridad de la "
    "derecha en cruces con avenidas no semaforizadas (Art. 41); salida de "
    "carril interior en rotondas multicarril; uso de balizas "
    "reglamentarias en frenadas de emergencia en autopista/rutas vs "
    "desaceleracion; operacion ABS en piso mojado (presion continua vs "
    "bombeo de freno); adelantamiento a transporte pesado con semirremolque "
    "y senas de giro (guino izquierdo indica peligro, guino derecho "
    "habilita paso); pendientes estrechas y retroceso de vehiculos segun "
    "porte y sentido de circulacion. 5. Devuelve UNICAMENTE un JSON con: "
    "escenario, opcion_a, opcion_b, correcta ('A'|'B'), "
    "fundamento_tecnico (explicacion del articulo o principio fisico).")


def _dilema_ia(usados=()):
    """Genera un dilema tecnico con Gemini. None si falla.

    Camino principal: SDK google-generativeai (gemini-1.5-flash,
    temperature 0.9 para variedad total). Respaldo: REST directo.
    """
    if not GEMINI_API_KEY:
        return None
    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Genera un dilema técnico vial real y difícil según la Ley "
            "Nacional 24.449 de Argentina. Las dos opciones deben parecer "
            "correctas a un conductor común pero solo una se ajusta a la "
            "normativa técnica. Responde ÚNICAMENTE un JSON con: "
            '{"escenario": "...", "opcion_a": "...", "opcion_b": "...", '
            '"correcta": "A", "fundamento_tecnico": "..."}')
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.9,
                               "max_output_tokens": 700})
        clean_text = (response.text.replace("```json", "")
                      .replace("```", "").strip())
        d = json.loads(clean_text)
        if (isinstance(d, dict) and d.get("escenario")
                and d.get("opcion_a") and d.get("opcion_b")):
            return d
    except Exception:
        import traceback
        traceback.print_exc()  # diagnostico exacto en consola
    for modelo in GEMINI_MODELOS:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{modelo}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={
                    "system_instruction": {
                        "parts": [{"text": _DILEMA_IA_SISTEMA}]},
                    "contents": [{"role": "user", "parts": [{"text":
                        "Genera el dilema vial tecnico en JSON."}]}],
                    "generationConfig": {
                        "maxOutputTokens": 700, "temperature": 0.85,
                        "thinkingConfig": {"thinkingBudget": 0}},
                },
                timeout=25)
            if r.status_code != 200:
                continue
            texto = "".join(
                p.get("text", "")
                for c in r.json().get("candidates", [])
                for p in c.get("content", {}).get("parts", []))
            texto_raw = texto.strip()
            texto_raw = re.sub(r"^```json\s*", "", texto_raw,
                               flags=re.MULTILINE)
            texto_raw = re.sub(r"```$", "", texto_raw,
                               flags=re.MULTILINE).strip()
            limpio = re.sub(r"^```\s*", "", texto_raw,
                            flags=re.MULTILINE).strip()
            d = json.loads(limpio)
            if (isinstance(d, dict) and d.get("escenario")
                    and d.get("opcion_a") and d.get("opcion_b")):
                return d
        except Exception:
            continue
    return None


def _dilema_a_tarjeta(raw):
    """Normaliza el JSON de Gemini al shape del juego."""
    correcta_a = str(raw.get("correcta", "")).strip().upper() == "A"
    fund = str(raw.get("fundamento_tecnico", "")).strip()
    return {
        "id": f"IA-{random.randint(1000, 99999)}",
        "titulo": "Dilema técnico generado por IA",
        "lugar": "Peritaje vial · Ley 24.449",
        # Claves crudas del contrato IA + shape del juego.
        "escenario": str(raw.get("escenario", "")).strip(),
        "opcion_a": str(raw.get("opcion_a", "")).strip(),
        "opcion_b": str(raw.get("opcion_b", "")).strip(),
        "correcta": str(raw.get("correcta", "")).strip().upper(),
        "situacion": str(raw.get("escenario", "")).strip(),
        "dificultad": 3,
        "opciones": [
            {"texto": str(raw.get("opcion_a", "")).strip(),
             "correcta": correcta_a, "es_correcta": correcta_a,
             "explicacion": fund, "comentario": fund},
            {"texto": str(raw.get("opcion_b", "")).strip(),
             "correcta": not correcta_a, "es_correcta": not correcta_a,
             "explicacion": fund, "comentario": fund},
        ],
        "explicacion": fund, "stats": {}, "fuente": "ia",
    }


# Banco de reserva TECNICO (Ley 24.449): solo se usa si Gemini esta
# caido o sin cuota. El juego jamas devuelve 500 ni "Gemini no
# respondio" al usuario.
_DILEMAS_RESERVA = [
    {"escenario": "Vas a ingresar a una rotonda y otro auto ya circula "
     "por el anillo giratorio. ¿Quién pasa primero?",
     "opcion_a": "Vos, por prioridad reglamentaria de la derecha.",
     "opcion_b": "El que ya circula dentro del anillo giratorio.",
     "correcta": "B",
     "fundamento_tecnico": "Art. 43 Ley 24.449: en rotondas el circulante "
     "dentro del anillo tiene prioridad sobre el que ingresa."},
    {"escenario": "Vas en auto por avenida y adelante va un ciclista. "
     "¿Cómo lo sobrepasás en forma segura y reglamentaria?",
     "opcion_a": "Tocás bocina corta y lo rebasás con 0,5 m en el mismo "
     "carril.",
     "opcion_b": "Cambiás de carril dejando margen lateral mínimo de "
     "1,5 m, sin bocina sorpresiva.",
     "correcta": "B",
     "fundamento_tecnico": "Art. 42 bis Ley 24.449: adelantamiento a "
     "ciclistas exige distancia lateral mínima de 1,5 metros."},
    {"escenario": "Con lluvia fuerte sentís que la dirección 'flota' y "
     "el auto pierde contacto con el asfalto (aquaplaning). ¿Qué "
     "maniobra corresponde?",
     "opcion_a": "Clavar el freno a fondo para detener el vehículo "
     "antes de perder la trayectoria.",
     "opcion_b": "Soltar suavemente el acelerador, sin frenar brusco ni "
     "girar el volante, hasta recuperar adherencia.",
     "correcta": "B",
     "fundamento_tecnico": "En hidroplaneo, frenar bloquea las ruedas "
     "sobre la película de agua y agrava la pérdida de control; la "
     "desaceleración progresiva recupera el grip."},
    {"escenario": "Llegás a un cruce de avenidas sin semáforo y otro "
     "vehículo se aproxima por tu derecha. ¿Quién tiene prioridad?",
     "opcion_a": "El que va por la avenida de mayor porte, "
     "independientemente del lado.",
     "opcion_b": "El vehículo que viene por la derecha (Art. 41 "
     "Ley 24.449).",
     "correcta": "B",
     "fundamento_tecnico": "Art. 41 Ley 24.449: sin señalización la "
     "prioridad es siempre del que viene por la derecha."},
    {"escenario": "En una calle angosta con fuerte pendiente donde no "
     "entran dos vehículos, te encontrás de frente con otro. ¿Quién "
     "retrocede?",
     "opcion_a": "El que sube, porque tiene mejor visibilidad.",
     "opcion_b": "El que desciende, salvo que lleve acoplado.",
     "correcta": "B",
     "fundamento_tecnico": "En pendientes el vehículo descendente debe "
     "retroceder porque puede controlar la reversa con el freno; el "
     "ascendente perdería tracción."},
]


@app.post("/api/generar-dilema-ia")
def api_generar_dilema_ia():
    """Dilema vial tecnico generado por IA en tiempo real.

    Si Gemini demora o falla por cuota/red se devuelve un dilema
    tecnico de reserva: NUNCA un 500 ni 'Gemini no respondio'.
    """
    payload = request.get_json(silent=True) or {}
    usados = {str(u) for u in (payload.get("usados") or [])}
    try:
        raw = _dilema_ia(usados) or _dilema_ia(usados)  # reintento
    except Exception:
        raw = None
    if raw:
        d = _dilema_a_tarjeta(raw)
    else:
        banco = [r for r in _DILEMAS_RESERVA
                 if r["escenario"][:40] not in usados] or _DILEMAS_RESERVA
        d = _dilema_a_tarjeta(random.choice(banco))
        d["fuente"] = "reserva"
    return jsonify({"status": "ok", "dilema": d})


@app.post("/api/desafio-decision")
def api_desafio_decision():
    """Dilema vial tecnico en modo supervivencia infinito.

    modo=UNIVERSAL mezcla todos los bancos por vehiculo + el banco
    tecnico (Ley 24.449, fisica vial, zonas ciegas). Cuando el banco se
    agota intenta generar uno nuevo con IA; si no hay IA reinicia el
    ciclo — nunca devuelve 'agotado': la partida solo termina al
    perder las 3 vidas.
    """
    payload = request.get_json(silent=True) or {}
    modo = normalizar(payload.get("modo"))
    if modo == "BICI":
        modo = "BICICLETA"

    edad_raw = payload.get("edad")
    try:
        edad = (min(max(int(edad_raw), 10), 95)
                if edad_raw is not None else None)
    except (TypeError, ValueError):
        edad = None
    usados = payload.get("usados") or []
    try:
        nivel = min(max(int(payload.get("nivel") or 1), 1), 3)
    except (TypeError, ValueError):
        nivel = 1

    # Pool: UNIVERSAL mezcla todos los modos + el banco tecnico.
    if modo == "UNIVERSAL" or modo not in SITUACIONES:
        pool = (
            [(f"{m}-{i}", s.get("modo") or m, s)
             for m, banco in SITUACIONES.items()
             for i, s in enumerate(banco)]
            + [(f"U-{i}", s.get("modo") or "AUTO", s)
               for i, s in enumerate(SITUACIONES_UNIVERSALES)])
    else:
        pool = [(f"{modo}-{i}", modo, s)
                for i, s in enumerate(SITUACIONES[modo])]
    disponibles = [x for x in pool if x[0] not in usados]

    if not disponibles:
        if GEMINI_API_KEY:
            gen = _desafio_gemini(usados, nivel)
            if gen:
                modo_g = (normalizar(gen.get("modo"))
                          if normalizar(gen.get("modo"))
                          in MODOS_VALIDOS else "AUTO")
                return jsonify({
                    "titulo": str(gen.get("titulo") or "Dilema vial"),
                    "lugar": str(gen.get("lugar") or "Corredor jujeño"),
                    "situacion": str(gen["situacion"]),
                    "dificultad": nivel,
                    "modo": modo_g,
                    "opciones": [
                        {"texto": str(o.get("texto") or ""),
                         "segura": bool(o.get("segura")),
                         "impacto": str(o.get("impacto") or ""),
                         "explicacion": str(o.get("explicacion") or "")}
                        for o in gen["opciones"]
                    ],
                    "id": f"g-{random.randrange(10**6)}",
                    "stats": _stats_dilema(modo_g, edad, gen),
                    "fuente": "gemini",
                })
        disponibles = pool  # sin IA: reinicia el ciclo (modo infinito)

    # Racha alta -> dilemas de mayor dificultad tecnica; degrada si agota.
    ideal = [x for x in disponibles if x[2].get("dificultad", 1) == nivel]
    candidatos = (ideal
                  or [x for x in disponibles
                      if x[2].get("dificultad", 1) <= nivel]
                  or disponibles)
    sid, modo_real, sit = random.choice(candidatos)

    return jsonify({
        **sit,
        "id": sid,
        "stats": _stats_dilema(modo_real, edad, sit),
    })


# ================== MODULO EDA (Analisis Exploratorio de Datos) ==================

MESES_NOMBRES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

RANGOS_ETARIOS_EDA = [
    ("<18", 0, 17), ("18-29", 18, 29), ("30-45", 30, 45),
    ("46-60", 46, 60), (">60", 61, 200),
]

MODOS_EDA = ["MOTO", "AUTO", "CAMIONETA", "BICICLETA", "PEATON"]
GRAVEDADES_EDA = ["SOLO DAÑOS", "LEVE", "GRAVE"]

COLS_CORR = [
    "hora_num", "edad_victima", "total_victimas",
    "fin_semana", "es_dia_feria", "target_same",
]
ETIQUETAS_CORR = {
    "hora_num": "Hora del día",
    "edad_victima": "Edad víctima",
    "total_victimas": "Total víctimas",
    "fin_semana": "Fin de semana",
    "es_dia_feria": "Alta concurrencia",
    "target_same": "Asistencia SAME",
}

_EDA_CACHE = {"key": None, "payload": None}


def _subset_eda(jurisdiccion, tipo_via):
    """Subconjunto de SINIESTROS segun el filtro del dashboard EDA.

    jurisdiccion: PERICO | MONTERRICO | EL CARMEN | RUTAS (vias de acceso)
    tipo_via:     prefijo de tipo_de_via (p.ej. RUTA, URBANA)
    """
    rows = SINIESTROS
    if tipo_via:
        rows = [s for s in rows
                if normalizar(s.get("tipo_de_via")).startswith(tipo_via)]
    if jurisdiccion == "RUTAS":
        rows = [s for s in rows
                if normalizar(s.get("tipo_de_via")).startswith("RUTA")
                or "RUTA" in normalizar(s.get("calle_1"))]
    elif jurisdiccion and jurisdiccion in CENTROS_MUNICIPIOS:
        rows = [s for s in rows if s["_jur"] == jurisdiccion]
    return rows


def _factores_eda(df):
    """Patrones interpretables para el publico general.

    Reemplazan en la UI a la matriz de correlacion cruda: cada tarjeta
    lleva icono, cifra destacada e intensidad (0-100) calculada del dataset.
    """
    factores = []
    total = len(df)
    if total < 10:
        return factores

    # 1) Mas involucrados -> mas demanda del SAME
    s_multi = df.loc[df["vic_n"] > 1, "es_same"]
    s_una = df.loc[df["vic_n"] == 1, "es_same"]
    if len(s_multi) and len(s_una) and s_una.mean():
        p_m, p_u = 100 * s_multi.mean(), 100 * s_una.mean()
        lift = (p_m / p_u - 1) * 100
        factores.append({
            "icono": "🚨",
            "titulo": "Múltiples víctimas → Mayor demanda SAME",
            "cifra": f"+{lift:.0f}%",
            "descripcion": (
                f"Con 2 o más personas involucradas la asistencia del SAME "
                f"sube de {p_u:.0f}% a {p_m:.0f}% de los casos: cada siniestro "
                f"exige más recursos de traslado."),
            "intensidad": min(100, max(0, round(lift))),
        })

    # 2) Pico horario en dias de alta concurrencia
    sub_conc = df[df["feria_n"] == 1]
    if len(sub_conc):
        vc = sub_conc["_hora"].value_counts()
        pico, n_pico = int(vc.idxmax()), int(vc.max())
        sobre = (n_pico / vc.mean() - 1) * 100 if len(vc) else 0
        factores.append({
            "icono": "🛒",
            "titulo": f"Días concurridos → Pico a las {pico:02d}:00 h",
            "cifra": f"{n_pico} casos",
            "descripcion": (
                f"En días de alta concurrencia comercial el máximo se da a "
                f"las {pico} h ({n_pico} siniestros, +{sobre:.0f}% sobre el "
                f"promedio horario): salida laboral + cierre comercial."),
            "intensidad": min(100, max(0, round(sobre))),
        })

    # 3) Rutas/accesos -> gravedad disparada
    rut = df["tipo_n"].str.startswith("RUTA")
    urb = df["tipo_n"] == "URBANA"
    if rut.sum() >= 10 and urb.sum() >= 10:
        g_r = 100 * df.loc[rut, "es_grave"].mean()
        g_u = 100 * df.loc[urb, "es_grave"].mean()
        veces = g_r / g_u if g_u else 0
        factores.append({
            "icono": "🛣️",
            "titulo": "Rutas y accesos → Gravedad disparada",
            "cifra": f"{g_r:.0f}% graves",
            "descripcion": (
                f"En rutas y accesos el {g_r:.0f}% de los siniestros son "
                f"graves vs {g_u:.0f}% en el casco urbano "
                f"(~{veces:.1f} veces más): la velocidad manda."),
            "intensidad": min(100, max(0, round(g_r * 2))),
        })

    # 4) Concentracion etaria 18-29
    joven = (df["edad"] >= 18) & (df["edad"] <= 29)
    pct_j = 100 * joven.sum() / total
    if pct_j >= 20:
        factores.append({
            "icono": "🎂",
            "titulo": "Franja 18–29 → Mayoría de las víctimas",
            "cifra": f"{pct_j:.0f}%",
            "descripcion": (
                f"El {pct_j:.0f}% de las víctimas tiene entre 18 y 29 años "
                f"(mediana {df['edad'].median():.0f}): conductores y "
                f"motociclistas primerizos concentran el riesgo."),
            "intensidad": min(100, round(pct_j)),
        })

    # 5) Semaforizacion: efecto medido honestamente
    sem1 = df.loc[df["sem_n"] == 1, "es_grave"]
    sem0 = df.loc[df["sem_n"] == 0, "es_grave"]
    if len(sem1) >= 20 and len(sem0) >= 20:
        g1, g0 = 100 * sem1.mean(), 100 * sem0.mean()
        dif = g1 - g0
        if abs(dif) < 5:
            titulo = "Semaforización → Sin cambio medible en gravedad"
            desc = (f"Cruces con semáforo: {g1:.0f}% graves · sin semáforo: "
                    f"{g0:.0f}%. La severidad depende del modo y la "
                    f"velocidad, no del dispositivo de cruce.")
            cifra, intens = "≈ 0 pp", 8
        elif dif < 0:
            titulo = "Semaforización → Menos impactos graves"
            desc = (f"Los cruces semaforizados registran {g1:.0f}% de "
                    f"gravedad vs {g0:.0f}% en cruces sin señalizar "
                    f"({abs(dif):.1f} pp menos).")
            cifra, intens = f"{dif:.0f} pp", min(100, round(abs(dif) * 4))
        else:
            titulo = "Semaforización → Más casos graves (más tránsito)"
            desc = (f"Las esquinas semaforizadas concentran {g1:.0f}% de "
                    f"gravedad vs {g0:.0f}% (+{dif:.1f} pp): marcan los "
                    f"cruces de mayor volumen.")
            cifra, intens = f"+{dif:.0f} pp", min(100, round(dif * 4))
        factores.append({
            "icono": "🚦", "titulo": titulo, "cifra": cifra,
            "descripcion": desc, "intensidad": intens,
        })
    return factores


def _metricas_eda(jurisdiccion=None, tipo_via=None):
    """Agregaciones EDA sobre SINIESTROS (o un subconjunto filtrado).

    Se cachea y solo se recalcula si el dataset cambia (reportes nuevos).
    """
    clave = (len(SINIESTROS), jurisdiccion, tipo_via)
    if _EDA_CACHE["key"] == clave:
        return _EDA_CACHE["payload"]

    contexto = {
        "PERICO": "Perico (casco urbano)",
        "MONTERRICO": "Monterrico",
        "EL CARMEN": "El Carmen",
        "RUTAS": "Rutas y accesos",
    }.get(jurisdiccion, "Todos los Valles (región completa)")

    rows = _subset_eda(jurisdiccion, tipo_via)
    if not rows:
        payload = {
            "generado": datetime.now().isoformat(timespec="seconds"),
            "contexto": contexto,
            "kpis_comparativos": {
                "total": 0, "siniestros_por_anio": {},
                "pct_same_por_anio": {}, "pct_same_global": 0,
                "pct_moto_por_anio": {}, "pct_moto_global": 0,
                "edad": {"media": 0, "mediana": 0, "min": 0, "max": 0},
                "sexo": {"MASCULINO": 0, "FEMENINO": 0},
            },
            "serie_mensual_interanual": {"meses": MESES_NOMBRES},
            "curva_horaria": {"horas": list(range(24)),
                              "concurrencia": [0] * 24,
                              "estandar": [0] * 24},
            "modo_vs_severidad": {"modos": [],
                                  **{g: [] for g in GRAVEDADES_EDA}},
            "top_arterias_interanual": {"calles": []},
            "distribucion_etaria": {
                "rangos": [r[0] for r in RANGOS_ETARIOS_EDA],
                "MASCULINO": [0] * len(RANGOS_ETARIOS_EDA),
                "FEMENINO": [0] * len(RANGOS_ETARIOS_EDA)},
            "matriz_correlacion": {
                "variables": COLS_CORR,
                "etiquetas": [ETIQUETAS_CORR[c] for c in COLS_CORR],
                "matriz": [[0.0] * len(COLS_CORR)] * len(COLS_CORR)},
            "factores_clave": [],
        }
        _EDA_CACHE["key"] = clave
        _EDA_CACHE["payload"] = payload
        return payload

    df = pd.DataFrame(rows)
    df["anio"] = pd.to_numeric(df["anio_siniestro"], errors="coerce")
    df["mes"] = pd.to_numeric(df["mes_siniestro"], errors="coerce")
    df["edad"] = pd.to_numeric(df["edad_victima"], errors="coerce")
    df["feria_n"] = pd.to_numeric(df["es_dia_feria"], errors="coerce")
    df["vic_n"] = pd.to_numeric(df["total_victimas"], errors="coerce")
    df["sem_n"] = pd.to_numeric(df["es_post_semaforos"], errors="coerce")
    df["es_same"] = df["asistencia_same"].map(normalizar) == "SI"
    df["modo_n"] = df["modo_desplazamiento_victima"].map(normalizar)
    df["grav_n"] = df["gravedad_siniestro"].map(
        lambda g: "SOLO DAÑOS" if normalizar(g) == "SOLO DANOS"
        else normalizar(g))
    df["es_grave"] = df["grav_n"] == "GRAVE"
    df["tipo_n"] = df["tipo_de_via"].map(normalizar)
    df["calle_n"] = df["calle_1"].fillna("").str.strip().str.upper()
    df["sexo_n"] = df["sexo_victima"].map(normalizar)

    anios = sorted(a for a in df["anio"].dropna().astype(int).unique())
    anios = [int(a) for a in anios]

    def pct_por_anio(mask):
        res = {}
        for a in anios:
            g = df[df["anio"] == a]
            res[str(a)] = round(100 * mask.loc[g.index].sum()
                                / len(g), 1) if len(g) else 0
        return res

    sin_anio = df["anio"].value_counts()
    edades = df["edad"].dropna()
    edades = edades[(edades >= 3) & (edades <= 100)]
    edad_stats = (
        {"media": round(float(edades.mean()), 1),
         "mediana": float(edades.median()),
         "min": int(edades.min()), "max": int(edades.max())}
        if len(edades)
        else {"media": 0, "mediana": 0, "min": 0, "max": 0})
    sexo_vc = df["sexo_n"].value_counts()
    n_masc, n_fem = int(sexo_vc.get("MASCULINO", 0)), int(sexo_vc.get("FEMENINO", 0))
    sexo_tot = n_masc + n_fem
    total = len(df)

    kpis = {
        "total": total,
        "siniestros_por_anio": {str(a): int(sin_anio.get(a, 0)) for a in anios},
        "pct_same_por_anio": pct_por_anio(df["es_same"]),
        "pct_same_global": round(100 * df["es_same"].sum() / total, 1),
        "pct_moto_por_anio": pct_por_anio(df["modo_n"] == "MOTO"),
        "pct_moto_global": round(
            100 * (df["modo_n"] == "MOTO").sum() / total, 1),
        "edad": edad_stats,
        "sexo": {
            "MASCULINO": round(100 * n_masc / sexo_tot, 1) if sexo_tot else 0,
            "FEMENINO": round(100 * n_fem / sexo_tot, 1) if sexo_tot else 0,
        },
    }

    # Serie mensual interanual
    mensual = {str(a): [0] * 12 for a in anios}
    for (a, m), n in df.groupby(["anio", "mes"]).size().items():
        if not (pd.isna(a) or pd.isna(m)) and 1 <= int(m) <= 12:
            mensual[str(int(a))][int(m) - 1] = int(n)

    # Curva horaria: dias de alta concurrencia (es_dia_feria=1) vs estandar
    horas = list(range(24))
    curva = {}
    for nombre, mask in (("concurrencia", df["feria_n"] == 1),
                         ("estandar", df["feria_n"] == 0)):
        vc = df.loc[mask, "_hora"].value_counts()
        curva[nombre] = [int(vc.get(h, 0)) for h in horas]

    # Severidad por modo de desplazamiento
    modos = [m for m in MODOS_EDA if m in set(df["modo_n"])]
    modo_vs = {
        g: [int(((df["modo_n"] == m) & (df["grav_n"] == g)).sum())
            for m in modos]
        for g in GRAVEDADES_EDA
    }

    # Top 5 arterias con desglose interanual
    top5 = df[df["calle_n"].ne("")]["calle_n"].value_counts().head(5)
    calles = top5.index.tolist()
    art = {"calles": calles}
    for a in anios:
        vc = df[df["anio"] == a]["calle_n"].value_counts()
        art[str(a)] = [int(vc.get(c, 0)) for c in calles]

    # Distribucion etaria cruzada por sexo
    rangos = [r[0] for r in RANGOS_ETARIOS_EDA]
    dist = {"rangos": rangos,
            "MASCULINO": [0] * len(rangos), "FEMENINO": [0] * len(rangos)}
    sub = df.dropna(subset=["edad"])
    sub = sub[sub["sexo_n"].isin(("MASCULINO", "FEMENINO"))]
    for sexo in ("MASCULINO", "FEMENINO"):
        for i, (_, lo, hi) in enumerate(RANGOS_ETARIOS_EDA):
            dist[sexo][i] = int(
                ((sub["sexo_n"] == sexo)
                 & sub["edad"].between(lo, hi)).sum())

    # Matriz de correlacion de Pearson
    for c in COLS_CORR:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    corr = df[COLS_CORR].corr()
    matriz = [
        [0.0 if pd.isna(v) else round(float(v), 3) for v in fila]
        for fila in corr.values
    ]

    payload = {
        "generado": datetime.now().isoformat(timespec="seconds"),
        "contexto": contexto,
        "kpis_comparativos": kpis,
        "serie_mensual_interanual": {
            "meses": MESES_NOMBRES,
            **{str(a): mensual[str(a)] for a in anios},
        },
        "curva_horaria": {"horas": horas, **curva},
        "modo_vs_severidad": {"modos": modos, **modo_vs},
        "top_arterias_interanual": art,
        "distribucion_etaria": dist,
        "matriz_correlacion": {
            "variables": COLS_CORR,
            "etiquetas": [ETIQUETAS_CORR[c] for c in COLS_CORR],
            "matriz": matriz,
        },
        "factores_clave": _factores_eda(df),
    }
    _EDA_CACHE["key"] = clave
    _EDA_CACHE["payload"] = payload
    return payload


@app.get("/api/eda-metricas")
def api_eda_metricas():
    """Dataset agregado para el dashboard EDA (IES N6 - Prof. Castro).

    Query params opcionales:
        jurisdiccion  PERICO | MONTERRICO | EL CARMEN | RUTAS
        tipo_via      prefijo de tipo_de_via (RUTA, URBANA, ...)
    """
    jur = normalizar(request.args.get("jurisdiccion") or "") or None
    tipo = normalizar(request.args.get("tipo_via") or "") or None
    return jsonify(_metricas_eda(jur, tipo))


# ============ MODULO RADAR: PRONOSTICO/PREVENCION HORARIA ============

RADAR_CENTROS = {
    "PERICO": (-24.377, -65.117, 13.8),
    "EL CARMEN": (-24.388, -65.259, 13.3),
    "MONTERRICO": (-24.438, -65.161, 13.8),
    "PALPALA": (-24.256, -65.212, 13.6),
    "SAN PEDRO DE JUJUY": (-24.233, -64.866, 13.6),
    "SAN SALVADOR DE JUJUY": (-24.185, -65.299, 12.6),
    "TODO JUJUY": (-24.335, -65.195, 11.4),
    # Corredor interurbano RN 66 / RN 9 / RP 42 / RP 47
    "RUTAS": (-24.29, -65.19, 11.6),
}
RADAR_RADIO_KM = {
    "PERICO": 5.0, "EL CARMEN": 6.0, "MONTERRICO": 5.0, "PALPALA": 5.0,
    "SAN PEDRO DE JUJUY": 5.0,
    "SAN SALVADOR DE JUJUY": 10.0, "TODO JUJUY": 65.0,
    "RUTAS": 65.0,
}
VIAS_PRINCIPALES = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary",
    "primary_link", "secondary", "secondary_link", "tertiary",
    "tertiary_link",
}
RADAR_MAX_VIAS = 1400
RADAR_MAX_PUNTOS = 80


def _dif_horaria(h1, h2):
    """Distancia circular en horas entre h1 y h2 (0-23)."""
    d = abs(h1 - h2) % 24
    return min(d, 24 - d)


def _subset_radar(jurisdiccion, dia, hora):
    """Siniestros del mismo dia de semana dentro de la ventana +-2 h."""
    jur_ok = jurisdiccion in CENTROS_MUNICIPIOS
    centro = RADAR_CENTROS[jurisdiccion]
    radio = RADAR_RADIO_KM[jurisdiccion]
    rows = []
    for s in SINIESTROS:
        if jurisdiccion == "RUTAS":
            # Solo incidentes sobre corredores viales provinciales o
            # nacionales (RN/RP/accesos/empalmes), en toda la region.
            if not _es_siniestro_ruta(s):
                continue
        elif jurisdiccion != "TODO JUJUY":
            if jur_ok and s["_jur"] != jurisdiccion:
                continue
            if not jur_ok and _haversine_km(
                    s["_lat"], s["_lon"], centro[0], centro[1]) > radio:
                continue
        if dia and s["_dia"] != dia and s["dia_semana_num"] != dia:
            continue
        if _dif_horaria(s["_hora"], hora) > 2:
            continue
        rows.append(s)
    return rows


def _agrupar_cruces(rows):
    """Agrupa la ventana por interseccion geografica (calle_1 + calle_2).

    Los registros sin calle se agrupan por grilla ~100 m; su nombre se
    resuelve luego contra la red vial. Clasificacion:
      rojo     -> hay siniestros graves o >= 2 casos en la ventana
      amarillo -> antecedentes leves / solo danios aislados
    """
    grupos = {}
    for s in rows:
        c1 = normalizar(s.get("calle_1") or "")
        c2 = normalizar(s.get("calle_2") or "")
        clave = (c1, c2) if c1 else (
            "GRID", round(s["_lat"], 3), round(s["_lon"], 3))
        grupos.setdefault(clave, []).append(s)

    puntos = []
    for (c1, c2), items in grupos.items():
        lat = sum(i["_lat"] for i in items) / len(items)
        lon = sum(i["_lon"] for i in items) / len(items)
        graves = sum(
            1 for i in items
            if normalizar(i["gravedad_siniestro"]) == "GRAVE")
        modos, causas = {}, {}
        for i in items:
            modos[i["_modo"]] = modos.get(i["_modo"], 0) + 1
            causa = (i.get("causa_probable") or "").strip().upper()
            if causa and causa != "NO ESPECIFICADA":
                causas[causa] = causas.get(causa, 0) + 1
        if c1 == "GRID":
            cruce = _cruce_en_punto(lat, lon)
            c1 = normalizar(cruce["calle_1"]) or "PUNTO SIN NOMBRE"
            c2 = normalizar(cruce["calle_2"]) or ""
        arteria = (c1 + (" y " + c2 if c2 else "")).title()
        puntos.append({
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "nivel": "rojo" if graves or len(items) >= 2 else "amarillo",
            "arteria": arteria,
            "causa": (max(causas.items(), key=lambda kv: kv[1])[0].title()
                      if causas else "No especificada"),
            "modo": (max(modos.items(), key=lambda kv: kv[1])[0].title()
                     if modos else "No especificado"),
            "n": len(items),
            "graves": graves,
        })
    puntos.sort(key=lambda p: (p["nivel"] != "rojo", -p["n"]))
    return puntos[:RADAR_MAX_PUNTOS]


_RED_SSJ = None


def _red_ssj():
    """Vias principales de San Salvador de Jujuy (fuera del bbox local).

    Descarga perezosa y cacheada via Overpass; devuelve [] si la red no
    responde (el radar igual centra la camara y muestra los puntos).
    """
    global _RED_SSJ
    if _RED_SSJ is not None:
        return _RED_SSJ
    _RED_SSJ = []
    lat, lon, _ = RADAR_CENTROS["SAN SALVADOR DE JUJUY"]
    query = (
        "[out:json][timeout:40];"
        'way["highway"~"motorway|trunk|primary|secondary|tertiary"]'
        f"(around:10000,{lat},{lon});out geom;")
    for endpoint in ("https://overpass-api.de/api/interpreter",
                     "https://overpass.kumi.systems/api/interpreter"):
        try:
            r = requests.post(
                endpoint, data={"data": query}, timeout=45,
                headers={"User-Agent": "mineria-valles-jujuy/1.0 "
                                       "(trabajo academico)"})
            r.raise_for_status()
            for el in r.json().get("elements", []):
                if el.get("type") != "way" or not el.get("geometry"):
                    continue
                coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
                if len(coords) >= 2:
                    _RED_SSJ.append({
                        "type": "Feature",
                        "geometry": {"type": "LineString",
                                     "coordinates": coords},
                        "properties": el.get("tags", {}),
                    })
            break
        except requests.RequestException:
            continue
    if not _RED_SSJ:
        # Ningun espejo respondio: no cachear el fallo, reintentar en el
        # proximo llamado (Overpass suele rechazar el primer intento).
        _RED_SSJ = None
        return []
    return _RED_SSJ


_RE_REF_RUTA = re.compile(r"^(RN|RP|RUTA)\s*\d", re.IGNORECASE)


def _es_via_ruta(props):
    """Feature de la red vial que pertenece a un corredor provincial o
    nacional: ref RN*/RP* o nombre con RUTA/ACCESO/EMPALME."""
    ref = normalizar(props.get("ref") or "")
    nombre = normalizar(props.get("name") or "")
    return (bool(_RE_REF_RUTA.match(ref))
            or bool(re.search(r"\b(RUTA|ACCESO|EMPALME)\b", nombre)))


def _vias_radar_geojson(jurisdiccion):
    """Vias principales del area como FeatureCollection (tramos fluidos)."""
    centro = RADAR_CENTROS[jurisdiccion]
    radio = RADAR_RADIO_KM[jurisdiccion]
    if jurisdiccion == "SAN SALVADOR DE JUJUY":
        red = _red_ssj()          # el geojson local no cubre la capital
    elif jurisdiccion in ("TODO JUJUY", "RUTAS"):
        red = RED_VIAL + _red_ssj()
    else:
        red = RED_VIAL
    feats = []
    for f in red:
        props = f["properties"] or {}
        hw = props.get("highway")
        if hw not in VIAS_PRINCIPALES:
            continue
        if jurisdiccion == "RUTAS" and not _es_via_ruta(props):
            continue  # solo corredores viales provinciales/nacionales
        coords = f["geometry"]["coordinates"]
        if jurisdiccion not in ("TODO JUJUY", "RUTAS") and not any(
                _haversine_km(lat, lon, centro[0], centro[1]) <= radio
                for lon, lat in coords):
            continue
        feats.append({
            "type": "Feature",
            "geometry": f["geometry"],
            "properties": {
                "name": props.get("name") or props.get("ref") or "",
                "highway": hw,
            },
        })
        if len(feats) >= RADAR_MAX_VIAS:
            break
    return {"type": "FeatureCollection", "features": feats}


@app.post("/api/pronostico-radar")
def api_pronostico_radar():
    """Radar preventivo: cruces de riesgo y tramos fluidos para un dia/hora.

    Body JSON: {jurisdiccion, dia_semana, hora_num}. Filtra el historial en
    una ventana de +-2 h para ese dia y zona, agrupa por interseccion y
    clasifica el riesgo (rojo = graves/reiterados, amarillo = leves).
    """
    payload = request.get_json(silent=True) or {}
    jur = normalizar(payload.get("jurisdiccion") or "PERICO")
    if jur == "SAN SALVADOR":
        jur = "SAN SALVADOR DE JUJUY"
    if jur in ("TODOS", "TODAS", "TODA LA REGION", "JUJUY"):
        jur = "TODO JUJUY"
    if jur in ("SOLO RUTAS", "RUTAS Y ACCESOS"):
        jur = "RUTAS"
    if jur not in RADAR_CENTROS:
        jur = "PERICO"

    dia = normalizar(payload.get("dia_semana") or "")
    if dia in ("TODOS", "TODAS"):
        dia = ""
    if dia.isdigit() and not 0 <= int(dia) <= 6:
        dia = ""

    try:
        hora = int(payload.get("hora_num"))
        if not 0 <= hora <= 23:
            hora = 20
    except (TypeError, ValueError):
        hora = 20

    rows = _subset_radar(jur, dia, hora)
    puntos = _agrupar_cruces(rows)
    lat, lon, zoom = RADAR_CENTROS[jur]
    return jsonify({
        "jurisdiccion": jur,
        "centro": {"lat": lat, "lon": lon, "zoom": zoom},
        "vias": _vias_radar_geojson(jur),
        "puntos": puntos,
        "resumen": {
            "dia": dia,
            "hora": hora,
            "ventana": f"{(hora - 2) % 24:02d}:00–{(hora + 2) % 24:02d}:59",
            "total_ventana": len(rows),
            "n_criticos": sum(1 for p in puntos if p["nivel"] == "rojo"),
            "n_precaucion": sum(1 for p in puntos if p["nivel"] == "amarillo"),
        },
    })


# ============ MODULO NOTICIAS: INGESTA IA + DEDUPLICACION ============

CAMPOS_NOTICIA = [
    "fecha_aprox", "hora_aprox", "ciudad", "calle_1", "calle_2",
    "modo_desplazamiento_victima", "contraparte_siniestro",
    "gravedad_siniestro", "edad_victima", "resumen",
]

MESES_TXT = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5,
    "JUNIO": 6, "JULIO": 7, "AGOSTO": 8, "SEPTIEMBRE": 9,
    "SETIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
}
DIAS_TXT = ["LUNES", "MARTES", "MIERCOLES", "JUEVES",
            "VIERNES", "SABADO", "DOMINGO"]

MAP_MODO = [
    (r"MOTO|MOTOCICLET|MOTOCICLIST", "MOTO"),
    (r"BICICLET|CICLIST", "BICICLETA"),
    (r"PEATON|PEATONA|A PIE|CAMINANDO|CAMINABA", "PEATON"),
    (r"CAMIONETA|PICK[\s-]?UP", "CAMIONETA"),
    (r"COLECTIVO|OMNIBUS|\bMICRO\b", "COLECTIVO"),
    (r"CAMION(?!ETA)", "CAMION"),
    (r"AUTO|AUTOMOVIL|VEHICULO|COCHE", "AUTO"),
]
MAP_CONTRAPARTE = [
    *MAP_MODO,
    (r"POSTE|COLUMNA|ARBOL|PARED|BARDA|ALAMBRADO|SEMAFORO|OBJETO FIJO",
     "OBJETO FIJO / COLUMNA"),
    (r"NINGUN|DESPIST|VOLCO|PERDIO EL CONTROL", "NINGUNO"),
]

_RE_VIA_NOTICIA = re.compile(
    r"\b(AVENIDA|AVDA?\.?|CALLE|BOULEVARD|BV\.?|PASAJE|DIAGONAL|ACCESO|"
    r"RUTA NACIONAL|RUTA PROVINCIAL|RUTA|RN|RP)\s*\.?\s*"
    r"([A-Z0-9Ñ][A-Z0-9Ñ .]{0,45}?)"
    r"(?=\s+(?:Y|E|ESQ\.?|ESQUINA|CRUCE|ENTRE|ALTURA|KM|C/)\b|[.,;:\n]|$)")
_RE_ESQUINA = re.compile(
    r"(?:ESQUINA|CRUCE|INTERSECCION)\s+(?:DE\s+LA\s+|DE\s+|LA\s+)?"
    r"([A-Z0-9Ñ][A-Z0-9Ñ ]{1,35}?)\s+(?:Y|E|CON)\s+"
    r"([A-Z0-9Ñ][A-Z0-9Ñ ]{1,35}?)(?=[.,;:\n]|$)")
_RE_CONTRAPARTE = re.compile(
    r"(?:CONTRA|CHOCO\s+CON|COLISIONO?\s+CON|IMPACTO\s+CONTRA?|"
    r"EMBISTIO\s+A|ATROPELLO?\s+A)\s+(?:UN[AO]?S?|EL|LA|LOS|LAS|OTR[AO])\s+"
    r"([A-Z0-9Ñ ]{2,25})")


def _norm_modo(valor):
    n = normalizar(valor or "")
    if not n or "NO ESPECIFICADO" in n:
        return ""
    for pat, modo in MAP_MODO:
        if re.search(pat, n):
            return modo
    return n if n in MODOS_VALIDOS else ""


def _norm_contraparte(valor):
    n = normalizar(valor or "")
    if not n or "NO ESPECIFICADO" in n:
        return ""
    for pat, modo in MAP_CONTRAPARTE:
        if re.search(pat, n):
            return modo
    return n if n in CONTRAPARTES_VALIDAS else ""


def _norm_gravedad(valor):
    n = normalizar(valor or "")
    if not n or "NO ESPECIFICADO" in n:
        return ""
    if re.search(r"FALLEC|MURI|DECED|FATAL|SIN VIDA|MUERT|GRAVE", n):
        return "GRAVE"
    if re.search(r"ILESO|SIN HERIDOS|SOLO DANOS|DANOS MATERIALES", n):
        return "SOLO DAÑOS"
    if re.search(r"LEVE|HERID|LESION|HOSPITAL|TRASLAD|AMBULANCIA|SAME", n):
        return "LEVE"
    return ""


def _norm_ciudad(valor):
    n = normalizar(valor or "")
    if not n or "NO ESPECIFICADO" in n:
        return None
    if "PERICO" in n:
        return "Perico"
    if "MONTERRICO" in n:
        return "Monterrico"
    if "CARMEN" in n:
        return "El Carmen"
    if "PALPALA" in n:
        return "Palpalá"
    if "SAN PEDRO" in n:
        return "San Pedro de Jujuy"
    # Quebrada de Humahuaca y otras zonas fuera de la region Valles.
    if re.search(r"\b(MAIMARA|TILCARA|HUMAHUACA|PURMAMARCA|SUSQUES|"
                 r"TUMBAYA|VOLCAN|QUEBRADA)\b", n):
        return "Otra"
    if "SAN SALVADOR" in n or "SAN SALVADOR DE JUJUY" in n or n == "JUJUY":
        return "San Salvador de Jujuy"
    return None


def _fecha_de_texto(valor):
    """'03/11/2026', '3 de noviembre', 'ayer', 'el sabado'... -> date|None."""
    n = normalizar(valor or "")
    if not n or "NO ESPECIFICADO" in n:
        return None
    hoy = datetime.now().date()
    if "ANTEAYER" in n or "ANTES DE AYER" in n:
        return hoy - timedelta(days=2)
    if re.search(r"\bAYER\b|\bANOCHE\b", n):
        return hoy - timedelta(days=1)
    if re.search(r"\bHOY\b", n):
        return hoy
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", n)
    if m:
        d, mes = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else hoy.year
        if y < 100:
            y += 2000
        try:
            f = date(y, mes, d)
            if f > hoy and not m.group(3):
                f = date(y - 1, mes, d)
            return f
        except ValueError:
            pass
    m = re.search(
        r"\b(\d{1,2})\s+DE\s+(" + "|".join(MESES_TXT) + r")"
        r"(?:\s+DE\s+(\d{4}))?", n)
    if m:
        try:
            f = date(int(m.group(3)) if m.group(3) else hoy.year,
                     MESES_TXT[m.group(2)], int(m.group(1)))
            if f > hoy and not m.group(3):
                f = date(f.year - 1, f.month, f.day)
            return f
        except ValueError:
            pass
    # 'el lunes' / 'el sabado pasado' -> ocurrencia mas reciente
    for i, dn in enumerate(DIAS_TXT):
        if re.search(rf"\b{dn}\b", n):
            delta = (hoy.weekday() - i) % 7 or 7
            return hoy - timedelta(days=delta)
    return None


def _hora_de_texto(valor):
    """'21:30', '21 horas', 'a las 21' -> 'HH:MM' | None."""
    n = normalizar(valor or "")
    if not n or "NO ESPECIFICADO" in n:
        return None
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", n)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"\b(\d{1,2})\s*(?:HORAS|HS\.?|H\.?)\b", n)
    if m and 0 <= int(m.group(1)) <= 23:
        return f"{int(m.group(1)):02d}:00"
    m = re.search(r"\bA LAS (\d{1,2})\b", n)
    if m and 0 <= int(m.group(1)) <= 23:
        return f"{int(m.group(1)):02d}:00"
    return None


def _modo_en_texto(n):
    """Primer modo de desplazamiento mencionado en el texto."""
    mejor, pos = "", len(n) + 1
    for pat, modo in MAP_MODO:
        m = re.search(pat, n)
        if m and m.start() < pos:
            mejor, pos = modo, m.start()
    return mejor


def _contraparte_en_texto(n):
    """Vehiculo/objeto contra el que impacto, segun verbos de choque."""
    for m in _RE_CONTRAPARTE.finditer(n):
        frag = m.group(1)
        for pat, modo in MAP_CONTRAPARTE:
            if re.search(pat, frag):
                return modo
    if re.search(r"\bDESPIST|\bVOLCO|PERDIO EL CONTROL|\bSE CAYO\b", n):
        return "NINGUNO"
    return ""


def _extraer_calles_noticia(n):
    """(calle_1, calle_2) desde el texto normalizado; '' si no figuran."""
    matches = list(_RE_VIA_NOTICIA.finditer(n))
    if matches:
        calle_1 = matches[0].group(0).strip(" .")
        calle_2 = matches[1].group(0).strip(" .") if len(matches) > 1 else ""
        if not calle_2:
            post = n[matches[0].end():]
            mc = re.match(
                r"\s+(?:Y|E|ESQ\.?|ESQUINA|CRUCE|C/)\s+"
                r"([A-Z0-9Ñ][A-Z0-9Ñ ]{1,40}?)"
                r"(?=[.,;:\n]|\s+(?:ALTURA|KM|ENTRE|DE LA|DEL|EN LA|A LA)\b|$)",
                post)
            if mc:
                calle_2 = mc.group(1).strip(" .")
        return calle_1.upper(), calle_2.upper()
    mq = _RE_ESQUINA.search(n)
    if mq:
        return mq.group(1).strip(), mq.group(2).strip()
    return "", ""


def _extraer_noticia_reglas(texto):
    """Fallback sin IA: extraccion por regex/NLP del texto de la noticia."""
    n = normalizar(texto)
    calle_1, calle_2 = _extraer_calles_noticia(n)
    m_edad = re.search(r"\b(\d{1,3})\s*A[ÑN]OS\b", n)
    edad = int(m_edad.group(1)) if m_edad and 1 <= int(m_edad.group(1)) <= 110 else None
    frases = re.split(r"(?<=[.!?])\s+", texto.strip())
    return {
        "fecha_aprox": texto,          # se parsea entero en _canon_noticia
        "hora_aprox": texto,
        "ciudad": _norm_ciudad(n) or "NO ESPECIFICADO",
        "calle_1": calle_1 or "NO ESPECIFICADO",
        "calle_2": calle_2 or "NO ESPECIFICADO",
        "modo_desplazamiento_victima": _modo_en_texto(n) or "NO ESPECIFICADO",
        "contraparte_siniestro": _contraparte_en_texto(n) or "NO ESPECIFICADO",
        "gravedad_siniestro": _norm_gravedad(n) or "NO ESPECIFICADO",
        "edad_victima": edad,
        "resumen": " ".join(frases[:2])[:260] or "NO ESPECIFICADO",
    }


def _extraer_noticia_gemini(texto):
    """Pide a Gemini la extraccion JSON estricta. None si falla."""
    # Etapa 1 previa: la jurisdiccion determinista se inyecta al prompt
    # para que la IA no invente la localidad del hecho.
    jur_firme = clasificar_jurisdiccion(texto)
    pista = (" JURISDICCION YA DETERMINADA POR CLASIFICADOR: "
             f"'{jur_firme}'. Usala tal cual en 'jurisdiccion'."
             if jur_firme and jur_firme != "OTRA" else "")
    sistema = pista + (
        "Sos un extractor geoespacial estricto de cronicas policiales de "
        "Jujuy, Argentina, sobre siniestros viales. Respondé SOLO con un "
        "objeto JSON valido, sin markdown ni texto extra. REGLAS "
        "INQUEBRANTABLES: 1) arteria_principal: SOLO el nombre formal y "
        "limpio de la via ('Avenida Brasil', 'Ruta 9', 'RP 42', 'Av. "
        "Belgrano'). NUNCA incluyas verbos ni texto del relato. 2) "
        "arteria_secundaria: SOLO si es una calle real transversal que "
        "corta a la principal; si la nota NO menciona una segunda calle "
        "explicita, devolve null. PROHIBIDO extraer fragmentos de "
        "oraciones como calles (ej. 'Pasajero a otro hombre', 'El "
        "conductor no pudo', 'Conecta las ciudades'). 3) jurisdiccion: "
        "exactamente una de ['Perico', 'Monterrico', 'El Carmen', "
        "'Palpala', 'San Pedro de Jujuy', 'San Salvador de Jujuy', "
        "'Otra'] — la localidad REAL "
        "del hecho; si ocurrio en Ruta 52 (Purmamarca/Susques/Puna) "
        "marca 'Otra' para no contaminar El Carmen o los Valles. 4) "
        "referencia_paraje: barrio, paraje o hito (ej. 'El Ceibal', 'San "
        "Antonio', '12 de Octubre', o null). Claves restantes: "
        "fecha_aprox (dd/mm/aaaa o null), hora_aprox (HH:MM o null), "
        "ciudad_exacta (localidad textual del texto), "
        "calle_principal y calle_secundaria (alias de arteria_* por "
        "compatibilidad), "
        "vehiculos_involucrados (lista breve), "
        "modo_desplazamiento_victima (MOTO | AUTO | CAMIONETA | BICICLETA | "
        "PEATON | COLECTIVO | CAMION), contraparte_siniestro (AUTO | MOTO | "
        "CAMIONETA | CAMION | COLECTIVO | BICICLETA | PEATON | "
        "OBJETO FIJO / COLUMNA | NINGUNO), gravedad (LEVE | "
        "GRAVE | SOLO DAÑOS), edad_victima (entero o null), resumen (una "
        "oracion). CATALOGO DE VIAS JUJENAS CONOCIDAS (reconocerlas con "
        "o sin prefijo de via): Canónigo Gorriti, Avenida 19 de Abril, "
        "Avenida Fascio, Avenida Éxodo, Avenida Belgrano, Avenida San "
        "Martín, Leandro N. Alem, Avenida Bolivia, Avenida Congreso, "
        "Avenida Villafañe, Avenida Centenario, Avenida José Hernández, "
        "Ruta Nacional 9, Ruta Nacional 66, Ruta Provincial 42, Ruta "
        "Provincial 43, Ruta Provincial 47, Acceso Dique La Ciénaga. Si "
        "el texto menciona \"Canónigo Gorriti\" o cualquier otra calle o "
        "ruta local del catálogo (o verificable como arteria jujeña), "
        "extraela en calle_principal con su nombre propio completo; NO "
        "marques \"Ubicación no detectada\" ni \"NO ESPECIFICADO\" "
        "mientras haya una via local identificable. REGLA ESTRICTA: si "
        "un dato no figura explicitamente en el texto, asigná exactamente "
        "\"NO ESPECIFICADO\" (o null si es numerico). No inventes ni "
        "deduzcas valores. Claves adicionales OBLIGATORIAS: "
        "esquina_exacta, jurisdiccion_confirmada ('Perico' | 'San "
        "Pedro de Jujuy' | 'San Salvador de Jujuy' | 'El Carmen' | "
        "'Monterrico' | 'Palpala' | 'Otra') y "
        "coordenadas_validas (booleano). PRECISION ESPACIAL: esquina_"
        "exacta es la interseccion o punto kilometrico preciso del hecho "
        "(ej. 'Gorriti y 19 de Abril', 'RN 66 km 1284'); si la esquina NO "
        "se menciona con precision suficiente para ubicarla a menos de "
        "50 metros, respondé exactamente 'REQUIERE_CONFIRMACION_MANUAL'. "
        "JURISDICCION ESTRICTA: la localidad real del hecho se decide "
        "cruzando la via detectada con las arterias oficiales de cada "
        "jurisdiccion — la calle manda sobre la sede del medio o la "
        "mencion generica de 'Jujuy'. ANCLAJES: Avenida Savio, Canónigo "
        "Gorriti, Avenida Fascio, Avenida 19 de Abril, Alvear, Balbín, "
        "Alberdi, Güemes, Senador Pérez y los barrios General Arias, "
        "Martiarena, Bajo La Viña, Alto Comedero, Ciudad de Nieva, "
        "Mariano Moreno, Lozano, Chijra y 13 de Octubre pertenecen a "
        "San Salvador de Jujuy; Avenida Belgrano, Avenida Éxodo, Leandro "
        "N. Alem, Avenida José Hernández, El Bananal y la Terminal de "
        "Ómnibus a Perico; Avenida Centenario, Empalme RN 9 y Acceso "
        "Dique La Ciénaga a El Carmen; el corredor RP 42/RP 43 y zona "
        "El Labrador a Monterrico. coordenadas_validas: true SOLO si la "
        "via detectada permite ubicar el punto con certeza espacial "
        "dentro de la jurisdiccion_confirmada; ante duda razonable "
        "asigná false. PROHIBIDO inventar coordenadas por defecto o "
        "hacer fallback a avenidas de Perico si el choque ocurrio en "
        "otra ciudad.")
    bruto = _preguntar_gemini(
        texto[:4500], sistema=sistema, max_tokens=600, temperatura=0.1)
    limpio = re.sub(r"^```(?:json)?|```$", "", bruto.strip(),
                    flags=re.M).strip()
    datos = json.loads(limpio)
    return datos if isinstance(datos, dict) else None


def _dato(datos, *claves):
    """Primer valor no vacio entre las claves alias (nuevas y viejas)."""
    for c in claves:
        v = datos.get(c)
        if v is not None and str(v).strip():
            return v
    return ""


# Sanitizador post-IA: palabras que demuestran que la "calle" extraida es
# en realidad un fragmento del relato ("Pasajero a otro hombre",
# "Conecta las ciudades"), no una via real.
_PROHIBIDAS_ARTERIA = {
    "CONDUCTOR", "PASAJERO", "HOMBRE", "MUJER", "EMBISTIO", "EVITAR",
    "CONECTA", "TERMINARON", "HOSPITAL", "TRAS", "CHOQUE", "CERCANIAS",
    "HACIA", "ENTRE",
}


def _sanear_arteria(calle, original=""):
    """'' si la cadena no es un nombre de via valido; la calle si lo es."""
    n = normalizar(calle or "")
    if not n or "NO ESPECIFICADO" in n:
        return ""
    toks = n.split()
    if any(t in _PROHIBIDAS_ARTERIA for t in toks):
        return ""
    # Mas de 3 palabras sin ser nombre propio -> fragmento de oracion.
    # (reglas emite TODO EN MAYUSCULAS: no es nombre propio pero es via
    # legitima — solo se descarta texto mixto que no es Title Case).
    s = str(original).strip()
    if len(toks) > 3 and not s.isupper() and s != s.title():
        return ""
    # Cola geografica del extractor regex: 'AV. MARTIJENA EN PALPALA'
    # -> 'AVENIDA MARTIJENA' (la ciudad va aparte).
    n = re.sub(r"\s+EN\s+(?:LA\s+|EL\s+|LOS\s+|LAS\s+)?"
               r"[A-ZÑ]{3,}(?:\s+[A-ZÑ]{3,})*\s*$", "", n).strip()
    # Ruido de medios y menciones de ciudad pegadas al nombre de via.
    n = re.sub(
        r"\s*(?:EN\s+)?(?:SAN PEDRO(?:\s+DE\s+JUJUY)?|PERICO|EL CARMEN|"
        r"MONTERRICO|JUJUY)\s*$", "", n)
    n = re.sub(r"\s*(?:RUTA\s+PROVINCIAL\s+)?EL\s+TRIBUNO(?:\s+DE\s+JUJUY)?",
               "", n)
    n = re.sub(r"\s*(?:SOMOS\s+JUJUY|JUJUY\s+AL\s+DIA|TODO\s+JUJUY|"
               r"SINIESTRO\s+VIAL|VUELCO|CHOQUE)\s*$", "", n)
    # Canon de rutas: 'RUTAS PROVINCIALES 1' -> 'RP 1', 'RUTA NACIONAL 9'
    # -> 'RN 9'.
    n = re.sub(r"^(?:RUTAS?|RUTA)\s+PROVINCIAL(?:ES)?\s*\.?\s*(\d+)$",
               r"RP \1", n)
    n = re.sub(r"^(?:RUTAS?|RUTA)\s+NACIONAL(?:ES)?\s*\.?\s*(\d+)$",
               r"RN \1", n)
    n = " ".join(n.split())
    # Solo quedo el tipo de via (sin nombre) -> no es una arteria.
    if n and not limpiar_calle(n):
        return ""
    return n


def _norm_jurisdiccion_valor(valor):
    """'Palpala'/'palpalá'/'EL CARMEN'/'Rutas' -> canonico del selector."""
    n = normalizar(valor or "")
    if n == "OTRA":
        return "Otra"
    if n in ("RUTAS", "RUTA", "CORREDOR VIAL"):
        return "Rutas"
    return _norm_ciudad(n)


def _canon_noticia(datos):
    """Normaliza la salida (Gemini o reglas) a los campos canonicos.

    Acepta las claves del prompt estricto (calle_principal, ciudad_exacta,
    gravedad) y las historicas (calle_1, ciudad, gravedad_siniestro).
    Si la IA marca REQUIERE_CONFIRMACION_MANUAL, la esquina queda como
    imprecisa y jamas se inventan coordenadas."""
    ext = {}
    ext["fecha_aprox"] = "NO ESPECIFICADO"
    f = _fecha_de_texto(str(datos.get("fecha_aprox") or ""))
    if f:
        ext["fecha_aprox"] = f.isoformat()
    ext["hora_aprox"] = _hora_de_texto(
        str(datos.get("hora_aprox") or "")) or "NO ESPECIFICADO"
    ext["ciudad"] = (_norm_ciudad(_dato(datos, "ciudad_exacta", "ciudad"))
                     or "NO ESPECIFICADO")
    for c, alias in (("calle_1", ("arteria_principal", "calle_principal",
                                  "calle_1")),
                     ("calle_2", ("arteria_secundaria", "calle_secundaria",
                                  "calle_2"))):
        v = str(_dato(datos, *alias)).strip()
        # Sanitizador post-IA: fragmentos del relato no son arterias.
        v = _sanear_arteria(v, v)
        ext[c] = v if v else "NO ESPECIFICADO"
    # Desduplicacion: 'Avenidas Brasil y Avenidas Brasil' -> una sola.
    if (ext["calle_2"] != "NO ESPECIFICADO"
            and limpiar_calle(ext["calle_2"]) == limpiar_calle(ext["calle_1"])):
        ext["calle_2"] = "NO ESPECIFICADO"
    ext["modo_desplazamiento_victima"] = (
        _norm_modo(datos.get("modo_desplazamiento_victima"))
        or _modo_en_texto(normalizar(
            str(datos.get("vehiculos_involucrados") or "")))
        or "NO ESPECIFICADO")
    ext["contraparte_siniestro"] = (
        _norm_contraparte(datos.get("contraparte_siniestro"))
        or "NO ESPECIFICADO")
    ext["gravedad_siniestro"] = (
        _norm_gravedad(_dato(datos, "gravedad", "gravedad_siniestro"))
        or "NO ESPECIFICADO")
    try:
        e = int(datos.get("edad_victima"))
        ext["edad_victima"] = e if 1 <= e <= 110 else None
    except (TypeError, ValueError):
        ext["edad_victima"] = None
    res = str(datos.get("resumen") or "").strip()
    ext["resumen"] = (res[:400]
                      if res and "NO ESPECIFICADO" not in normalizar(res)
                      else "NO ESPECIFICADO")
    # Jurisdiccion estricta declarada por la IA (o derivada de ciudad/calle).
    jc = _norm_jurisdiccion_valor(
        _dato(datos, "jurisdiccion", "jurisdiccion_confirmada"))
    ext["jurisdiccion_confirmada"] = jc or "NO ESPECIFICADO"
    # Referencia territorial auxiliar (barrio/paraje/hito citado en la nota).
    ext["referencia_paraje"] = (
        " ".join(str(_dato(datos, "referencia_paraje")).split())
        or "NO ESPECIFICADO")
    # Esquina imprecisa: la IA pidio confirmacion manual -> prohibido
    # georreferenciar de oficio; el punto lo marca el usuario en el mapa.
    marcas = " ".join(str(_dato(datos, "esquina_exacta", "esquina",
                              "calle_principal", "calle_1",
                              "calle_secundaria", "calle_2")))
    ext["esquina_imprecisa"] = "REQUIERE_CONFIRMACION_MANUAL" in normalizar(
        marcas)
    cv = datos.get("coordenadas_validas")
    ext["coordenadas_validas"] = (
        False if ext["esquina_imprecisa"]
        else (cv if isinstance(cv, bool) else None))
    return ext


# Arterias inconfundibles por jurisdiccion: un "Belgrano" o "San Martin"
# existe en todos los pueblos, pero Savio o Gorriti solo estan en la
# capital. La calle detectada define la localidad real del hecho.
VIAS_POR_JURISDICCION = {
    "SAN SALVADOR DE JUJUY": (
        "SAVIO", "GORRITI", "CANONIGO", "FASCIO", "19 DE ABRIL",
        "ALVEAR", "BALBIN", "ALBERDI", "GUEMES", "SENADOR PEREZ",
        "NECOCHEA", "CAFRUNE",
    ),
    "PERICO": (
        "JOSE HERNANDEZ", "LEANDRO N ALEM", "LEANDRO ALEM",
        "TERMINAL DE OMNIBUS", "ROTONDA BELGRANO",
    ),
    "EL CARMEN": (
        "DIQUE LA CIENAGA", "LA CIENAGA", "EMPALME",
    ),
    "MONTERRICO": (
        "EL LABRADOR", "ACCESO SAN ANTONIO",
    ),
    "PALPALA": (
        "MARTIJENA", "RIO DE LA PLATA", "CATALANO",
        "JUAN JOSE PASO", "AVENIDA LIBERTAD",
    ),
    "SAN PEDRO DE JUJUY": (
        "BRASIL", "SIRIA", "GOLETA", "PROLONGACION",
    ),
}


def _jurisdiccion_por_calle(calle):
    """Jurisdiccion deducida de una arteria inconfundible, o ''."""
    n = normalizar(calle or "")
    if not n:
        return ""
    for jur, claves in VIAS_POR_JURISDICCION.items():
        if any(re.search(rf"\b{re.escape(k)}\b", n) for k in claves):
            return jur
    return ""


# Barrios y zonas inconfundibles por jurisdiccion: el cronista suele citar
# el barrio antes que la calle. Un hecho en "Martiarena" es de San
# Salvador aunque el filtro activo diga Perico.
BARRIOS_POR_JURISDICCION = {
    "SAN SALVADOR DE JUJUY": (
        "GENERAL ARIAS", "GRAL ARIAS", "MARTIARENA", "BAJO LA VINA",
        "ALTO COMEDERO", "CIUDAD DE NIEVA", "MARIANO MORENO",
        "LOZANO", "CHIJRA", "13 DE OCTUBRE", "SAN PEDRITO", "CUYAYA",
        "PUESTO VIEJO", "BARRIO CUYAYA", "LOTEO CUYAYA",
        "REYES", "RIO LEON", "ARIAS", "GUAYAQUIL",
    ),
    "PERICO": (
        "EL BANANAL", "BARRIO NORTE", "TERMINAL DE OMNIBUS",
    ),
    "EL CARMEN": (
        "PAMPAS BLANCAS", "EL QUIMILAR",
    ),
    "MONTERRICO": (
        "EL LABRADOR",
    ),
    "PALPALA": (
        "12 DE OCTUBRE", "EL CEIBAL", "SAN ANTONIO", "PALPALA",
    ),
    "SAN PEDRO DE JUJUY": (
        "SAN PEDRO", "BARRIO CANAL", "LA POSTA",
    ),
    # Fuera de la region operativa: Quebrada de Humahuaca y Puna.
    "OTRA": (
        "RUTA 52", "MAIMARA", "TILCARA", "HUMAHUACA", "PURMAMARCA",
        "SUSQUES", "TUMBAYA", "VOLCAN", "QUEBRADA",
    ),
}

# ================= ETAPA 1: CLASIFICADOR TERRITORIAL =================
# Diccionario deterministico de entidades geograficas provinciales con
# su Bounding Box hermetico y centroide urbano de referencia.
ENTIDADES_GEO = {
    "SAN PEDRO DE JUJUY": {
        "claves": ("SAN PEDRO", "LA POSTA", "INGENIO LA ESPERANZA",
                   "BARRIO CANAL", "PATERSON"),
        "bbox": (-64.91, -24.28, -64.82, -24.19),
        "centro": (-24.233, -64.866),
    },
    "SAN SALVADOR DE JUJUY": {
        "claves": ("SAN SALVADOR", "CAPITAL", "GORRITI", "ARIAS",
                   "CORONEL ARIAS", "SAVIO", "MARTIARENA", "REYES",
                   "BAJO LA VINA", "CHIJRA", "RIO LEON",
                   "CIUDAD DE NIEVA", "GUAYAQUIL"),
        "bbox": (-65.37, -24.25, -65.23, -24.13),
        "centro": (-24.185, -65.299),
    },
    "PERICO": {
        "claves": ("PERICO", "CIUDAD PERICO", "AEROPUERTO", "EL PONGO",
                   "BARRIO SAN JOSE", "BARRIO OBRERO"),
        "bbox": (-65.16, -24.42, -65.07, -24.34),
        "centro": (-24.377, -65.117),
    },
    "MONTERRICO": {
        "claves": ("MONTERRICO", "12 DE OCTUBRE", "LA OVEJERIA"),
        "bbox": (-65.20, -24.47, -65.12, -24.40),
        "centro": (-24.438, -65.161),
    },
    "EL CARMEN": {
        "claves": ("EL CARMEN", "DIQUES", "LA CIENAGA", "LAS MADERAS",
                   "SAN ANTONIO", "EL CEIBAL"),
        "bbox": (-65.32, -24.43, -65.22, -24.34),
        "centro": (-24.388, -65.258),
    },
    "PALPALA": {
        "claves": ("PALPALA", "ALTOS HORNOS ZAPLA", "MARTIJENA",
                   "RIO BLANCO"),
        "bbox": (-65.26, -24.30, -65.17, -24.22),
        "centro": (-24.256, -65.212),
    },
}
FUERA_ZONA = (
    "RUTA 52", "PURMAMARCA", "TILCARA", "HUMAHUACA", "LA QUIACA",
    "SUSQUES", "LEDEZMA", "LIBERTADOR", "MAIMARA", "TUMBAYA",
    "VOLCAN", "QUEBRADA",
)


def clasificar_jurisdiccion(texto):
    """Etapa 1: clasifica el texto completo en una jurisdiccion firme.

    Devuelve 'OTRA' si el hecho es fuera de la region operativa
    (Quebrada/Puna -> descartable), la clave de la jurisdiccion con la
    mencion territorial mas fuerte, o '' si nada determina la zona.
    """
    n = normalizar(texto or "")
    if any(re.search(rf"\b{re.escape(k)}\b", n) for k in FUERA_ZONA):
        return "OTRA"
    mejor, mejor_score = "", 0
    for jur, ent in ENTIDADES_GEO.items():
        score = sum(1 for k in ent["claves"]
                    if re.search(rf"\b{re.escape(k)}\b", n))
        # Las claves compuestas pesan mas (mencion inconfundible).
        if score > mejor_score:
            mejor, mejor_score = jur, score
    return mejor


def _en_bbox_jur(punto, jur):
    """Etapa 3: el punto debe caer dentro del bbox hermetico."""
    bb = BBOX_JURISDICCION.get(jur)
    if not bb:
        return True
    return (bb[1] <= punto["lat"] <= bb[3]
            and bb[0] <= punto["lon"] <= bb[2])

_RE_MARCADOR_RUTA = re.compile(
    r"\b(RUTA|RN\s*\d+|RP\s*\d+|NACIONAL|PROVINCIAL|ACCESO|EMPALME|"
    r"AUTOPISTA|KM\s*\d+)\b")


def _jurisdiccion_en_texto(texto):
    """Jurisdiccion que revela el texto crudo (barrios/vias unicas), o ''."""
    n = normalizar(texto or "")
    if not n:
        return ""
    for jur, claves in BARRIOS_POR_JURISDICCION.items():
        if any(re.search(rf"\b{re.escape(k)}\b", n) for k in claves):
            return jur
    return _jurisdiccion_por_calle(texto)


def _noticia_en_ruta(ext, texto=""):
    """True si el hecho ocurrio sobre un corredor vial provincial o
    nacional (RN 66, RN 9, RP 42, RP 47, accesos, empalmes)."""
    partes = " ".join(filter(None, [
        ext.get("calle_1") or "", ext.get("calle_2") or "", texto or ""]))
    return bool(_RE_MARCADOR_RUTA.search(normalizar(partes)))


def _jurisdiccion_detectada(ext):
    """Jurisdiccion mas confiable del hecho: confirmada por IA > ciudad
    explicita > arteria inconfundible. 'OTRA' = fuera del corredor."""
    jc = ext.get("jurisdiccion_confirmada") or ""
    if normalizar(jc) == "OTRA":
        return "OTRA"
    return (normalizar(_norm_ciudad(jc) or "")
            or normalizar(_norm_ciudad(ext.get("ciudad")) or "")
            or _jurisdiccion_por_calle(ext.get("calle_1"))
            or _jurisdiccion_por_calle(ext.get("calle_2")))


def _geo_valida(geo, ext):
    """Certeza espacial del punto.

    Exige que el geocoder haya encontrado una via real (no el centro
    generico), que la IA no haya marcado coordenadas invalidas y que el
    punto caiga dentro de la jurisdiccion detectada: si "Gorriti" matchea
    en Perico pero el hecho es de San Salvador, el punto es espurio y se
    marca invalido en vez de inventarlo."""
    if ext.get("esquina_imprecisa"):
        return False  # la IA pidio confirmacion manual: no inventar punto
    if not (geo and geo.get("confianza") != "baja"
            and ext.get("coordenadas_validas") is not False):
        return False
    jur = _jurisdiccion_detectada(ext)
    if jur == "OTRA":
        return False
    centro = CENTROS_MUNICIPIOS.get(jur)
    if centro and _haversine_km(geo["lat"], geo["lon"], *centro) > (
            RADAR_RADIO_KM.get(jur, RADIO_JURISDICCION_KM)):
        return False  # calle homonima de otro pueblo
    return True


def _geocodificar_noticia(ciudad, calle_1, calle_2, usar_nominatim=True,
                          jur_detectada=""):
    """Estima lat/lon cruzando calles con la red vial local (y Nominatim).

    La jurisdiccion detectada (ciudad/calle del hecho, no la elegida en el
    filtro) decide el contexto geografico: si la noticia es de San
    Salvador se consulta Nominatim con esa ciudad y el fallback centra en
    (-24.185, -65.299) — nunca se fuerza el punto a Perico.
    """
    jur = (jur_detectada if jur_detectada in CENTROS_MUNICIPIOS
           else normalizar(_norm_ciudad(ciudad or "") or ""))
    ciudad_ctx = {
        "PERICO": "Perico", "MONTERRICO": "Monterrico",
        "EL CARMEN": "El Carmen", "PALPALA": "Palpalá",
        "SAN PEDRO DE JUJUY": "San Pedro de Jujuy",
        "SAN SALVADOR DE JUJUY": "San Salvador",
    }.get(jur, ciudad or "")

    consulta = (f"{calle_1} y {calle_2}" if calle_1 and calle_2
                else calle_1 or "")
    if consulta:
        # En Perico/Palpala el motor de interseccion local es el de mayor
        # precision: limpia prefijos/titulos (limpiar_calle) y cruza las
        # geometrias reales de las LineStrings del catalogo.
        if jur in ("PERICO", "PALPALA", "SAN PEDRO DE JUJUY",
                   "SAN SALVADOR DE JUJUY"):
            try:
                cruce = geo_cruzar(calle_1 or "", calle_2 or "", jur=jur)
                if cruce:
                    return cruce
            except Exception:
                pass
        # En San Salvador la red vial local no tiene cobertura: Nominatim
        # primero evita matchear calles homonimas de los Valles.
        if jur != "SAN SALVADOR DE JUJUY":
            try:
                res = geo_buscar(consulta)
                # Match local solo con confianza >= 85%: nada de resolver
                # la esquina en otra jurisdiccion por nombre parecido.
                if (res and res[0].get("score", 0) >= 0.85
                        and _en_bbox_jur(res[0], jur)):
                    return {
                        "lat": res[0]["lat"], "lon": res[0]["lon"],
                        "nombre": res[0]["nombre"],
                        "fuente": "red-vial", "confianza": "alta",
                    }
            except Exception:
                pass
        if usar_nominatim:
            try:
                res = geo_nominatim(
                    f"{consulta}, {ciudad_ctx}, Jujuy, Argentina",
                    limite=1,
                    viewbox=BBOX_JURISDICCION.get(
                        jur, BBOX_JURISDICCION["TODOS"]))
                if res:
                    return {
                        "lat": res[0]["lat"], "lon": res[0]["lon"],
                        "nombre": res[0]["nombre"],
                        "fuente": "nominatim", "confianza": "media",
                    }
            except requests.RequestException:
                pass
        if jur == "SAN SALVADOR DE JUJUY":
            try:
                res = geo_buscar(consulta)
                if (res and res[0].get("score", 0) >= 0.85
                        and _en_bbox_jur(res[0], jur)):
                    return {
                        "lat": res[0]["lat"], "lon": res[0]["lon"],
                        "nombre": res[0]["nombre"],
                        "fuente": "red-vial", "confianza": "alta",
                    }
            except Exception:
                pass
    # Etapa 4 - fallback seguro: con jurisdiccion firme se asigna el
    # centroide urbano propio; JAMAS un punto de otra jurisdiccion.
    sugerida = " y ".join(
        p.title() for p in (calle_1, calle_2) if p) or ciudad_ctx or ""
    if jur in CENTROS_MUNICIPIOS:
        lat_c, lon_c = CENTROS_MUNICIPIOS[jur]
        return {
            "lat": lat_c, "lon": lon_c,
            "coordenadas": [lon_c, lat_c],
            "calle_sugerida": sugerida,
            "nombre": sugerida or ciudad_ctx or "Ubicación no resuelta",
            "fuente": "centroide-jurisdiccion", "confianza": "media",
            "coordenadas_validas": True,
        }
    return {
        "lat": None, "lon": None, "coordenadas": None,
        "calle_sugerida": sugerida,
        "nombre": sugerida or "Ubicación no resuelta",
        "fuente": "sin-coincidencia", "confianza": "baja",
        "coordenadas_validas": False,
    }


def geocodificar_noticia(calle_1, calle_2, ciudad):
    """API publica para el Monitor de Noticias: recibe las dos arterias
    textuales detectadas por la IA mas la ciudad, y devuelve lat/lon.

    1) En Perico cruza las geometrias reales del catalogo local
       (limpiar_calle + motor de interseccion): nunca queda vacio si la
       IA ya detecto las dos arterias.
    2) Si no, delega al pipeline general (red vial + Nominatim acotado
       al bbox estricto de la jurisdiccion detectada).
    """
    # Sanitiza las entradas crudas (titulares con colas de medio/ciudad).
    calle_1 = _sanear_arteria(calle_1, calle_1)
    calle_2 = _sanear_arteria(calle_2, calle_2)
    jur = _jurisdiccion_query(ciudad)
    if jur in ("PERICO", "PALPALA", "SAN PEDRO DE JUJUY",
               "SAN SALVADOR DE JUJUY"):
        try:
            cruce = geo_cruzar(calle_1 or "", calle_2 or "", jur=jur)
            if cruce:
                return cruce
        except Exception:
            pass
    return _geocodificar_noticia(
        ciudad or "", calle_1 or "", calle_2 or "",
        usar_nominatim=True, jur_detectada=jur)


def _duplicado_noticia(ext, geo):
    """ID del registro probablemente duplicado, o None.

    Regla: misma fecha (+-24 h) y (misma ciudad + misma calle) o radio
    < 200 m del punto georreferenciado. Si la noticia no tiene fecha
    resoluble, se exige cercania geografica + misma calle en los ultimos
    7 dias (advertencia mas suave).
    """
    fecha = None
    if ext["fecha_aprox"] != "NO ESPECIFICADO":
        try:
            fecha = date.fromisoformat(ext["fecha_aprox"])
        except ValueError:
            fecha = None
    jur_ext = normalizar(ext["ciudad"]) if ext["ciudad"] != "NO ESPECIFICADO" else ""
    calle_ext = (normalizar(ext["calle_1"])
                 if ext["calle_1"] != "NO ESPECIFICADO" else "")
    raiz_ext = _raiz_nombre(calle_ext) if calle_ext else ""
    hoy = datetime.now().date()

    for s in SINIESTROS:
        try:
            f_s = date.fromisoformat(str(s.get("fecha_dt") or "")[:10])
        except ValueError:
            try:
                f_s = datetime.strptime(
                    s.get("fecha_siniestro") or "", "%d/%m/%Y").date()
            except ValueError:
                continue
        if fecha:
            if abs((f_s - fecha).days) > 1:
                continue
        elif abs((f_s - hoy).days) > 7:
            continue

        # La georreferencia 'centro-ciudad' es solo una aproximacion de
        # respaldo: no debe disparar el match por radio <200 m.
        cerca = (bool(geo) and geo.get("confianza") != "baja"
                 and _haversine_km(geo["lat"], geo["lon"],
                                   s["_lat"], s["_lon"]) <= 0.2)
        misma_ciudad = bool(jur_ext) and s["_jur"] == jur_ext
        raiz_s = _raiz_nombre(s.get("calle_1") or "")
        misma_calle = bool(raiz_ext) and bool(raiz_s) and (
            raiz_ext == raiz_s or raiz_ext in raiz_s or raiz_s in raiz_ext)

        if fecha:
            if cerca or (misma_ciudad and misma_calle):
                return s["id_siniestro"]
        elif cerca and misma_calle:
            return s["id_siniestro"]
    return None


def _html_a_texto(doc):
    """HTML crudo de un articulo -> texto plano legible."""
    t = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", doc)
    t = re.sub(r"(?is)<br[^>]*>|</p>|</div>|</h[1-6]>", ". ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return " ".join(html.unescape(t).split())


_HEADERS_WEB = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "es-AR,es;q=0.9",
}


def _resolver_url_gnews(url):
    """Resuelve enlaces /rss/articles/ de Google News a la URL del medio.

    Google News ya no hace redirect 30x: la pagina intermedia trae una
    firma (data-n-a-sg) y un timestamp (data-n-a-ts) que hay que mandar
    al RPC interno 'Fbv4je' de batchexecute para recuperar la URL real.
    """
    if "news.google.com" not in url:
        return url
    try:
        r = requests.get(url, headers=_HEADERS_WEB, timeout=15)
        if "news.google.com" not in r.url:
            return r.url
        m_id = re.search(r"/articles/([A-Za-z0-9_-]+)", url)
        m_ts = re.search(r'data-n-a-ts="(\d+)"', r.text)
        m_sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        if m_id and m_ts and m_sg:
            inner = (
                '["garturlreq",[["es-419","AR",["WEB_TEST_1_0_0"],'
                'null,null,1,1,"AR:es-419",null,180,null,null,null,'
                'null,null,0,null,null,[1608992183,723341000]],'
                '"es-419","AR",1,[2,3,4,8],1,0,"655000234",0,0,null,0]'
                f',"{m_id.group(1)}",{m_ts.group(1)},"{m_sg.group(1)}"]')
            payload = json.dumps([[
                ["Fbv4je", inner, None, "generic"]]])
            r2 = requests.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                data={"f.req": payload}, headers=_HEADERS_WEB,
                timeout=15)
            for linea in r2.text.splitlines():
                linea = linea.strip()
                if not linea.startswith("["):
                    continue
                try:
                    arr = json.loads(linea)
                    envoltura = json.loads(arr[0][2])
                    destino = envoltura[1]
                    if isinstance(destino, str) and destino.startswith("http"):
                        return destino
                except (json.JSONDecodeError, TypeError, IndexError,
                        KeyError):
                    continue
        # Ultimo recurso: primer enlace externo a Google en la pagina.
        m_href = re.search(
            r'href="(https?://(?!www\.google|news\.google|accounts\.google|'
            r'support\.google|policies\.google)[^"]+)"', r.text)
        if m_href:
            return html.unescape(m_href.group(1))
    except requests.RequestException:
        pass
    return url


def _extraer_cuerpo_html(doc):
    """Texto del articulo: trafilatura/bs4 si estan, si no regex simple."""
    try:  # trafilatura da el cuerpo limpio cuando esta instalada
        import trafilatura
        t = trafilatura.extract(doc, include_comments=False,
                                include_tables=False)
        if t and len(t) > 200:
            return " ".join(t.split())
    except ImportError:
        pass
    try:  # BeautifulSoup: <article> o la suma de parrafos largos
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(doc, "html.parser")
        nodo = soup.find("article") or soup.find("main") or soup
        parrafos = [p.get_text(" ", strip=True)
                    for p in nodo.find_all("p")]
        texto = " ".join(p for p in parrafos if len(p) > 40)
        if len(texto) > 200:
            return " ".join(texto.split())
    except ImportError:
        pass
    return _html_a_texto(doc)


def _descargar_cuerpo_noticia(url):
    """Descarga el cuerpo real de la nota: resuelve el redirect de Google
    News y extrae el articulo completo. Devuelve (texto, url_final)."""
    url_final = _resolver_url_gnews(url)
    r = requests.get(url_final, headers=_HEADERS_WEB, timeout=15)
    r.raise_for_status()
    return _extraer_cuerpo_html(r.text), url_final


@app.post("/api/procesar-noticia")
def api_procesar_noticia():
    """Extrae el siniestro desde una cronica periodistica y detecta duplicados.

    Body JSON: {texto, url?}. Si viene URL (y no texto) descarga el cuerpo
    REAL de la nota — resolviendo el redirect de Google News — y analiza el
    articulo completo, no el titular truncado. Con GEMINI_API_KEY usa
    extraccion IA; si no, regex/NLP local. Georreferencia por calle+cruce
    dentro de la jurisdiccion detectada y compara contra el CSV.
    """
    payload = request.get_json(silent=True) or {}
    texto = str(payload.get("texto") or "").strip()
    url = str(payload.get("url") or "").strip()
    url_final = url
    cuerpo_completo = bool(texto)

    if url:
        try:
            descargado, url_final = _descargar_cuerpo_noticia(url)
            if len(descargado) >= 120:
                texto = descargado
                cuerpo_completo = True
        except requests.RequestException:
            pass
    if not texto and url:
        return jsonify({
            "status": "error",
            "mensaje": ("No se pudo descargar el cuerpo de la noticia; "
                        "pegá el texto de la crónica directamente."),
        }), 400

    if len(texto) < 60:
        return jsonify({
            "status": "error",
            "mensaje": "Pegá la crónica completa (mínimo ~60 caracteres).",
        }), 400

    datos, fuente = None, "reglas"
    if GEMINI_API_KEY:
        try:
            datos = _extraer_noticia_gemini(texto)
            if datos:
                fuente = "gemini"
        except (RuntimeError, json.JSONDecodeError, AttributeError,
                TypeError, ValueError):
            datos = None
    if not datos:
        datos = _extraer_noticia_reglas(texto)
    ext = _canon_noticia(datos)

    geo = _geocodificar_noticia(
        None if ext["ciudad"] == "NO ESPECIFICADO" else ext["ciudad"],
        None if ext["calle_1"] == "NO ESPECIFICADO" else ext["calle_1"],
        None if ext["calle_2"] == "NO ESPECIFICADO" else ext["calle_2"],
        jur_detectada=_jurisdiccion_detectada(ext))
    geo["coordenadas_validas"] = _geo_valida(geo, ext)
    dup_id = _duplicado_noticia(ext, geo)

    advertencia = (
        f"⚠️ Posible duplicado del registro ID {dup_id}. Se sugiere "
        "fusionar información en lugar de crear una fila nueva"
        if dup_id else "")
    return jsonify({
        "status": "ok",
        "fuente": fuente,
        "extraido": ext,
        "geo": geo,
        "es_duplicado": bool(dup_id),
        "duplicado_id": dup_id,
        "advertencia": advertencia,
        "url_final": url_final,
        "cuerpo_completo": cuerpo_completo,
        "cuerpo_len": len(texto),
    })


# ====== MONITOR DE PRENSA: escaneo automatico de Google News ======

GNEWS_PERIODOS = {
    "24H": "when:1d", "ULTIMAS 24 HORAS": "when:1d",
    "48H": "when:2d", "ULTIMAS 48 HORAS": "when:2d",
    "SEMANA": "when:7d", "ESTA SEMANA": "when:7d",
    "MES": "when:30d", "ESTE MES": "when:30d",
}
GNEWS_MAX_ITEMS = 15
GNEWS_GEMINI_ITEMS = 8   # solo los primeros N titulares van a la IA

# Descarte previo (antes de Gemini): terminos necrologicos / aereos y, por
# el otro lado, exigencia minima de vocabulario vial en el titular.
_GNEWS_NEGATIVOS = re.compile(
    r"Q\.?\s*E\.?\s*P\.?\s*D|FALLECIMIENTO|OBITUARIO|NECROLOG|"
    r"AVION|AEROPUERTO|AEROLINEA|VUELO|HELICOPTERO|"
    r"FUTBOL|COPA\s+(AMERICA|LIBERTADORES|ARGENTINA)|SELECCION\s+ARGENTINA")
_GNEWS_VIALES = re.compile(
    r"CHOQUE|COLISION|SINIESTRO|ACCIDENTE|ATROPELL|EMBISTI|VOLCAD|\bVOLCO|"
    r"DESPIST|\bMOTO|AUTOMOVIL|\bAUTO\b|VEHICULO|CAMIONETA|\bCAMION\b|"
    r"COLECTIVO|OMNIBUS|BICICLETA|CICLISTA|PEATON|TRANSITO|TRAFICO|"
    r"MOTORISTA|CONDUCTOR|CHOFER|\bRUTA\b|AVENIDA")


def _titular_parece_vial(titulo):
    """True si el titular amerita ir a la IA: sin necrologicas/aereas y
    con al menos un termino de vehiculo o transito."""
    n = normalizar(titulo)
    if _GNEWS_NEGATIVOS.search(n):
        return False
    return bool(_GNEWS_VIALES.search(n))


def _extraer_noticia_gemini_safe(texto):
    """_extraer_noticia_gemini sin propagar errores (uso en lotes)."""
    try:
        return _extraer_noticia_gemini(texto)
    except (RuntimeError, json.JSONDecodeError, AttributeError,
            TypeError, ValueError):
        return None


@app.get("/api/buscar-noticias-google")
def api_buscar_noticias_google():
    """Monitor de prensa: titulares de Google News -> extraccion + dedup.

    Params: zona (default Jujuy), periodo (48h | semana | mes). Consulta el
    feed RSS abierto de Google News en es-419/AR, corre la extraccion de
    campos (Gemini en paralelo sobre los primeros titulares, regex para el
    resto) y marca duplicados contra el CSV.
    """
    zona = str(request.args.get("zona") or "Jujuy").strip() or "Jujuy"
    when = GNEWS_PERIODOS.get(
        normalizar(request.args.get("periodo") or "SEMANA"), "when:7d")

    # Filtro estricto de jurisdiccion: si la zona elegida es una localidad
    # del corredor, se descartan noticias confirmadas en otra localidad
    # ("Jujuy"/"Todo Jujuy"/"Toda la Region" no filtra). "RUTAS" es un
    # ambito especial: solo incidentes sobre corredores viales.
    zona_n = normalizar(zona)
    solo_rutas = zona_n in ("RUTAS", "SOLO RUTAS", "RUTAS Y ACCESOS")
    jur_filtro = ("" if solo_rutas or zona_n in (
        "", "JUJUY", "TODO JUJUY", "TODOS", "TODA LA REGION")
        else normalizar(_norm_ciudad(zona) or ""))

    # Consulta estricta de transito jujeño: exige terminos de siniestralidad
    # vial + la provincia + la zona elegida (evita notas de otros paises y
    # falsos positivos como personas apodadas "Perico").
    if solo_rutas:
        q = (f"(choque OR siniestro vial OR colision) Jujuy "
             f"(ruta OR \"RN 66\" OR \"RN 9\" OR \"RP 42\" OR \"RP 47\") "
             f"{when}")
    else:
        q = f"(choque OR siniestro vial OR colision) Jujuy ({zona}) {when}"
    try:
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": q, "hl": "es-419", "gl": "AR",
                    "ceid": "AR:es-419"},
            headers={"User-Agent": "mineria-valles-jujuy/1.0 "
                                   "(trabajo academico)"},
            timeout=20)
        r.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({
            "status": "error",
            "mensaje": f"No se pudo consultar Google News ({exc}).",
        }), 502

    try:
        raiz = ET.fromstring(r.content)
    except ET.ParseError:
        return jsonify({"status": "error",
                        "mensaje": "Respuesta inválida de Google News."}), 502

    items = []
    for it in raiz.findall(".//item")[:GNEWS_MAX_ITEMS]:
        titulo = (it.findtext("title") or "").strip()
        medio = ""
        src = it.find("source")
        if src is not None and src.text:
            medio = src.text.strip()
        sufijo = f" - {medio}"
        if medio and titulo.endswith(sufijo):
            titulo = titulo[: -len(sufijo)].strip()
        fecha_pub = None
        try:
            fecha_pub = parsedate_to_datetime(
                (it.findtext("pubDate") or "").strip())
        except (TypeError, ValueError):
            pass
        items.append({
            "titulo": titulo,
            "medio": medio,
            "url": (it.findtext("link") or "").strip(),
            "fecha_pub": fecha_pub.isoformat() if fecha_pub else "",
            "fecha_d": fecha_pub.date() if fecha_pub else None,
            "texto": _html_a_texto(
                f"{titulo}. {it.findtext('description') or ''}"),
        })

    # Descarte previo en Python: necrologicas, aereas o titulares sin un
    # solo termino de vehiculo/transito jamas llegan a Gemini.
    total_rss = len(items)
    items = [it for it in items if _titular_parece_vial(it["titulo"])]
    descartados = total_rss - len(items)

    # Enriquecimiento IA en paralelo (primeros N titulares)
    datos = [None] * len(items)
    if GEMINI_API_KEY and items:
        n_ia = min(GNEWS_GEMINI_ITEMS, len(items))
        with ThreadPoolExecutor(max_workers=4) as pool:
            resultados = list(pool.map(
                lambda i: _extraer_noticia_gemini_safe(items[i]["texto"]),
                range(n_ia)))
        datos[:n_ia] = resultados

    noticias = []
    descartadas_jur = 0
    for i, it in enumerate(items):
        ext = _canon_noticia(datos[i] or _extraer_noticia_reglas(it["texto"]))
        if ext["fecha_aprox"] == "NO ESPECIFICADO" and it["fecha_d"]:
            ext["fecha_aprox"] = it["fecha_d"].isoformat()
        # Filtro estricto de ciudad: con jurisdiccion elegida se descarta
        # toda noticia confirmada en otra localidad — por la via detectada
        # o por mencion clara a un barrio ajeno (ej. eligiendo Perico se
        # descarta lo que mencione Gorriti, Martiarena, Bajo La Vina...).
        jur_det = _jurisdiccion_detectada(ext)
        jur_txt = clasificar_jurisdiccion(it["texto"])
        # San Pedro: exige mencion propia y descarta hitos de la capital.
        if jur_filtro == "SAN PEDRO DE JUJUY":
            nt = normalizar(it["texto"])
            if not re.search(
                    r"\b(SAN PEDRO|LA ESPERANZA|LA POSTA|PATERSON)\b", nt):
                descartadas_jur += 1
                continue
            if re.search(
                    r"\b(HOSPITAL SAN ROQUE|HOSPITAL PABLO SORIA|GORRITI|"
                    r"CORONEL ARIAS|SAN SALVADOR|AVENIDA SAVIO|"
                    r"MARTIARENA)\b", nt):
                descartadas_jur += 1
                continue
        if jur_filtro and ((jur_det and jur_det != jur_filtro)
                           or (jur_txt and jur_txt != jur_filtro)):
            descartadas_jur += 1
            continue
        # "Solo Rutas": exclusivamente incidentes sobre corredores viales
        # provinciales o nacionales (RN 66, RN 9, RP 42, RP 47, accesos).
        if solo_rutas and not _noticia_en_ruta(ext, it["texto"]):
            descartadas_jur += 1
            continue
        geo = _geocodificar_noticia(
            None if ext["ciudad"] == "NO ESPECIFICADO" else ext["ciudad"],
            None if ext["calle_1"] == "NO ESPECIFICADO" else ext["calle_1"],
            None if ext["calle_2"] == "NO ESPECIFICADO" else ext["calle_2"],
            usar_nominatim=False,
            jur_detectada=jur_txt or jur_det)  # el texto manda sobre la IA
        geo["coordenadas_validas"] = _geo_valida(geo, ext)
        # La calle manda: si el punto georreferenciado cae fuera del radio
        # de la jurisdiccion filtrada, el hecho es de otra localidad.
        if (jur_filtro and geo["confianza"] != "baja"
                and _haversine_km(geo["lat"], geo["lon"],
                                  *CENTROS_MUNICIPIOS[jur_filtro])
                > RADAR_RADIO_KM.get(jur_filtro, RADIO_JURISDICCION_KM)):
            descartadas_jur += 1
            continue
        dup_id = _duplicado_noticia(ext, geo)
        noticias.append({
            "id": f"n-{i}",
            "titulo": it["titulo"],
            "medio": it["medio"],
            "url": it["url"],
            "fecha_pub": it["fecha_pub"],
            "texto": it["texto"],
            "extraido": ext,
            "jurisdiccion_detectada": jur_det or None,
            "geo": geo,
            "es_duplicado": bool(dup_id),
            "duplicado_id": dup_id,
        })

    return jsonify({
        "status": "ok", "zona": zona, "periodo": when,
        "total": len(noticias), "noticias": noticias,
        "descartados": descartados,
        "descartados_jurisdiccion": descartadas_jur,
        "jurisdiccion_filtro": ("RUTAS" if solo_rutas else jur_filtro or None),
        "fuente": "gemini" if GEMINI_API_KEY else "reglas",
    })


@app.post("/api/analizar-url")
def api_analizar_url():
    """Descarga server-side la nota del escaner y devuelve la extraccion.

    Resuelve el redirect de Google News, descarga el HTML con UA de
    navegador (timeout 10 s para que el boton jamas quede trabado),
    extrae parrafos + fecha de publicacion de los metadatos y pasa todo
    por Gemini. Si la descarga falla, procesa el titular de la tarjeta.
    """
    if not request.is_json:
        return jsonify({"status": "error",
                        "mensaje": "Body JSON requerido"}), 400
    datos = request.get_json(silent=True) or {}
    url = str(datos.get("url") or "").strip()
    titulo = str(datos.get("titulo") or "").strip()
    if not (url.lower().startswith("http://")
            or url.lower().startswith("https://")):
        return jsonify({"status": "error", "mensaje": "URL inválida."}), 400

    cuerpo, fecha_pub, url_final = "", None, url
    try:
        try:
            url = _resolver_url_gnews(url) or url
        except Exception:
            pass
        r = requests.get(url, headers=_UA_DESCARGA, timeout=10)
        r.raise_for_status()
        url_final = r.url
        cuerpo = _extraer_cuerpo_html(r.text)
        fecha_pub = _extraer_fecha_html(r.text)
    except Exception:
        cuerpo = ""   # paywall / gnews bloqueado: cae al titular

    texto_ia = (cuerpo or titulo).strip()
    if len(texto_ia) < 40:
        return jsonify({"status": "error",
                        "mensaje": "No se pudo obtener contenido de la nota."}), 502
    if fecha_pub:
        texto_ia = (f"[Fecha de publicación del artículo: "
                    f"{fecha_pub.isoformat()}]\n\n" + texto_ia)
    try:
        ext = _armar_extraccion_nota(texto_ia)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"status": "error",
                        "mensaje": f"Gemini no respondió: {exc}"}), 502
    extraido = (ext or {}).get("extraido") or {}
    if not extraido.get("fecha_siniestro") and fecha_pub:
        extraido["fecha_siniestro"] = fecha_pub.isoformat()
    extraido["fuente"] = "gemini"
    return jsonify({"status": "ok", "extraido": extraido,
                    "url_final": url_final}), 200


@app.get("/api/buscar-direccion")
def api_buscar_direccion():
    """Buscador del mapa -> Google Maps Geocoding (prioridad absoluta)."""
    from src.geocoder import geo_google
    q = str(request.args.get("q") or "").strip()
    jur = str(request.args.get("jurisdiccion") or "").strip()
    if len(q) < 2:
        return jsonify({"status": "ZERO_RESULTS"})
    if normalizar(jur) in ("", "TODOS", "TODA LA REGION", "JUJUY"):
        jur = ""
    res = geo_google(q, None, jur)
    if res:
        return jsonify({"status": "OK", "lat": res["lat"],
                        "lon": res["lon"], "nombre": res["nombre"]})
    return jsonify({"status": "ZERO_RESULTS"})


GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")


@app.get("/api/places-autocomplete")
def api_places_autocomplete():
    """Sugerencias en vivo: Google Places Autocomplete -> fallback
    Geocoding (ambos son Google; Places esta deshabilitada en la key)."""
    q = str(request.args.get("q") or "").strip()
    if len(q) < 2 or not GOOGLE_MAPS_KEY:
        return jsonify([])
    out = []
    try:  # Places API (New): preferida cuando esta habilitada
        rn = requests.post(
            "https://places.googleapis.com/v1/places:autocomplete",
            headers={"X-Goog-Api-Key": GOOGLE_MAPS_KEY,
                     "Content-Type": "application/json"},
            json={"input": q, "languageCode": "es",
                  "includedRegionCodes": ["ar"],
                  "locationBias": {"circle": {
                      "center": {"latitude": -24.25,
                                 "longitude": -65.10},
                      "radius": 50000.0}}},
            timeout=5).json()
        for sug in rn.get("suggestions", []):
            pp = sug.get("placePrediction") or {}
            txt = (pp.get("text") or {}).get("text") or ""
            sf = pp.get("structuredFormat") or {}
            if pp.get("placeId") and txt:
                out.append({
                    "titulo": (sf.get("mainText") or {}).get("text", txt),
                    "subtitulo": (sf.get("secondaryText") or {}).get(
                        "text", ""),
                    "descripcion_completa": txt,
                    "place_id": pp["placeId"], "fuente": "Google Maps"})
    except Exception as exc:
        print(f"Places New: {exc}")
    try:
        if out:
            raise StopIteration
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/autocomplete/json",
            params={"input": q, "components": "country:ar",
                    "locationbias": "circle:80000@-24.25,-65.10",
                    "language": "es", "key": GOOGLE_MAPS_KEY},
            timeout=5).json()
        for it in r.get("predictions", []):
            sf = it.get("structured_formatting") or {}
            out.append({"titulo": sf.get("main_text", it.get("description")),
                        "subtitulo": sf.get("secondary_text", ""),
                        "descripcion_completa": it.get("description"),
                        "place_id": it.get("place_id"),
                        "fuente": "Google Maps"})
    except StopIteration:
        pass
    except Exception as exc:
        print(f"Error en Google Autocomplete: {exc}")
    if not out:  # Places deshabilitada -> Geocoding como respaldo
        try:
            g = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": f"{q}, Jujuy, Argentina",
                        "components": "country:AR",
                        "key": GOOGLE_MAPS_KEY}, timeout=5).json()
            for it in g.get("results", [])[:5]:
                partes = it.get("formatted_address", "").split(",")
                out.append({
                    "titulo": partes[0].strip(),
                    "subtitulo": ",".join(partes[1:]).strip(),
                    "descripcion_completa": it.get("formatted_address", ""),
                    "place_id": "",
                    "lat": it["geometry"]["location"]["lat"],
                    "lon": it["geometry"]["location"]["lng"],
                    "fuente": "Google Maps"})
        except Exception:
            pass
    return jsonify([o for o in out if o["titulo"]])


def _geo_por_address(address):
    """Geocoding de Google por texto libre -> dict status OK o None."""
    if not address or not GOOGLE_MAPS_KEY:
        return None
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": GOOGLE_MAPS_KEY},
            timeout=5).json()
        if r.get("status") == "OK" and r.get("results"):
            loc = r["results"][0]["geometry"]["location"]
            return {"status": "OK", "lat": loc["lat"], "lon": loc["lng"],
                    "nombre": r["results"][0].get("formatted_address", "")}
    except Exception:
        pass
    return None


@app.get("/api/place-coordenadas")
@app.get("/api/place-details")
def api_place_coordenadas():
    """Resuelve place_id (Place Details) o address (Geocoding) a lat/lon."""
    place_id = str(request.args.get("place_id") or "").strip()
    address = str(request.args.get("address") or "").strip()
    if place_id and GOOGLE_MAPS_KEY:
        for url_places in (
            f"https://places.googleapis.com/v1/places/{place_id}",
            "https://maps.googleapis.com/maps/api/place/details/json"):
            try:
                if "v1/places" in url_places:
                    r = requests.get(url_places, headers={
                        "X-Goog-Api-Key": GOOGLE_MAPS_KEY,
                        "X-Goog-FieldMask": "location,formattedAddress"},
                        timeout=5).json()
                    loc = r.get("location") or {}
                    if "latitude" in loc:
                        return jsonify({"status": "OK",
                                        "lat": loc["latitude"],
                                        "lon": loc["longitude"],
                                        "nombre": r.get("formattedAddress", "")})
                else:
                    r = requests.get(url_places, params={
                        "place_id": place_id,
                        "fields": "geometry,formatted_address",
                        "key": GOOGLE_MAPS_KEY}, timeout=5).json()
                    if r.get("status") == "OK":
                        loc = r["result"]["geometry"]["location"]
                        return jsonify({"status": "OK", "lat": loc["lat"],
                                        "lon": loc["lng"],
                                        "nombre": r["result"].get(
                                            "formatted_address", "")})
            except Exception:
                pass
    if address:
        res = _geo_por_address(
            address if "jujuy" in address.lower()
            else f"{address}, Jujuy, Argentina")
        if res:
            return jsonify(res)
    return jsonify({"status": "ZERO_RESULTS"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Servidor MODULO 3 en http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
