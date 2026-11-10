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
import random
import re
import sys
import threading
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


def _jurisdiccion_param():
    """Jurisdiccion estricta: las 4 ciudades del corredor; defecto Perico."""
    jur = normalizar(request.args.get("jurisdiccion", ""))
    return jur if jur in CENTROS_MUNICIPIOS else "PERICO"


@app.get("/api/datos")
def api_datos():
    jurisdiccion = _jurisdiccion_param()
    modo = normalizar(request.args.get("modo", ""))
    dia = normalizar(request.args.get("dia_semana", ""))
    franja = _parsear_franja(request.args.get("franja_horaria", ""))

    filtrados = [
        s
        for s in SINIESTROS
        if s["_jur"] == jurisdiccion
        and (modo in ("", "TODOS", "TODAS") or s["_modo"] == modo)
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

    centro = CENTROS_MUNICIPIOS[_jurisdiccion_param()]
    sugerencias = []
    for r in geo_buscar(consulta):
        if _haversine_km(r["lat"], r["lon"], *centro) > RADIO_JURISDICCION_KM:
            continue
        sugerencias.append(
            {"nombre": r["nombre"], "lon": r["lon"], "lat": r["lat"]}
        )
        if len(sugerencias) >= 8:
            break
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
    os.environ.get("GEMINI_API_KEY") or _leer_env().get("GEMINI_API_KEY") or ""
)
# Modelos flash disponibles en la API (los 1.5/2.0 ya fueron retirados).
GEMINI_MODELOS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")

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
    return [
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


def _mito_gemini(usados):
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
    previas = "; ".join(usados[-4:]) if usados else "ninguna"
    prompt = f"Generá UN mito/afirmacion NUEVO (no repitas estos: {previas})."
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

    if GEMINI_API_KEY:
        datos = _mito_gemini(usados)
        if datos:
            return jsonify({
                "pregunta": str(datos["pregunta"]),
                "es_verdadero": bool(datos["es_verdadero"]),
                "explicacion": str(datos.get("explicacion") or ""),
                "fuente_dato": str(datos.get("fuente_dato") or "Gemini"),
                "id": f"g-{random.randrange(10**6)}",
                "fuente": "gemini",
            })

    banco = _banco_trivia()
    disponibles = [(i, b) for i, b in enumerate(banco)
                   if f"b{i}" not in usados]
    if not disponibles:
        return jsonify({"agotado": True,
                        "mensaje": "Completaste todo el banco de mitos."})
    i, item = random.choice(disponibles)
    return jsonify({**item, "id": f"b{i}", "fuente": "banco"})


# ------------- Simulador de decisiones: situaciones por modo -------------

def _sit(titulo, lugar, texto, op_a, op_b):
    """op_*: (texto, es_segura, impacto, explicacion)."""
    return {"titulo": titulo, "lugar": lugar, "situacion": texto,
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
    ],
}


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


@app.post("/api/desafio-decision")
def api_desafio_decision():
    """Situacion callejera con 2 opciones segun edad y modo del jugador."""
    payload = request.get_json(silent=True) or {}
    modo = normalizar(payload.get("modo"))
    if modo == "BICI":
        modo = "BICICLETA"
    if modo not in SITUACIONES:
        modo = "MOTO"
    try:
        edad = min(max(int(payload.get("edad") or 25), 10), 95)
    except (TypeError, ValueError):
        edad = 25

    usados = payload.get("usados") or []
    banco = SITUACIONES[modo]
    disponibles = [(i, s) for i, s in enumerate(banco)
                   if f"{modo}-{i}" not in usados]
    if not disponibles:
        disponibles = list(enumerate(banco))  # reinicia el ciclo
    i, sit = random.choice(disponibles)

    stats = {
        "modo": modo,
        **_stats_modo(modo),
        "edad_rango": _stats_edad(edad),
    }
    # Si el dilema ocurre de noche, el feedback se contextualiza solo con
    # datos nocturnos (19-06h): evita contradicciones tipo 'peor franja tarde'.
    hora_sit = _hora_texto(f"{sit['lugar']} {sit['situacion']}")
    if hora_sit is not None and (hora_sit >= 19 or hora_sit < 6):
        noche = _stats_modo_nocturno(modo)
        if noche:
            stats["nocturno"] = {"hora": hora_sit, **noche}

    return jsonify({
        **sit,
        "id": f"{modo}-{i}",
        "stats": stats,
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
    "SAN SALVADOR DE JUJUY": (-24.185, -65.299, 12.6),
    "TODO JUJUY": (-24.335, -65.195, 11.4),
}
RADAR_RADIO_KM = {
    "PERICO": 5.0, "EL CARMEN": 6.0, "MONTERRICO": 5.0,
    "SAN SALVADOR DE JUJUY": 10.0, "TODO JUJUY": 65.0,
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
        if jurisdiccion != "TODO JUJUY":
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


def _vias_radar_geojson(jurisdiccion):
    """Vias principales del area como FeatureCollection (tramos fluidos)."""
    centro = RADAR_CENTROS[jurisdiccion]
    radio = RADAR_RADIO_KM[jurisdiccion]
    if jurisdiccion == "SAN SALVADOR DE JUJUY":
        red = _red_ssj()          # el geojson local no cubre la capital
    elif jurisdiccion == "TODO JUJUY":
        red = RED_VIAL + _red_ssj()
    else:
        red = RED_VIAL
    feats = []
    for f in red:
        props = f["properties"] or {}
        hw = props.get("highway")
        if hw not in VIAS_PRINCIPALES:
            continue
        coords = f["geometry"]["coordinates"]
        if jurisdiccion != "TODO JUJUY" and not any(
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
    sistema = (
        "Sos un extractor estructurado de cronicas policiales de Jujuy, "
        "Argentina, sobre siniestros viales. Respondé SOLO con un objeto "
        "JSON valido, sin markdown ni texto extra, con estas claves "
        "exactas: fecha_aprox (dd/mm/aaaa o null), hora_aprox (HH:MM o "
        "null), ciudad (Perico | Monterrico | El Carmen | San Salvador de "
        "Jujuy | otra), calle_1, calle_2 (la via que cruza, si figura), "
        "modo_desplazamiento_victima (MOTO | AUTO | CAMIONETA | BICICLETA | "
        "PEATON | COLECTIVO | CAMION), contraparte_siniestro (AUTO | MOTO | "
        "CAMIONETA | CAMION | COLECTIVO | BICICLETA | PEATON | "
        "OBJETO FIJO / COLUMNA | NINGUNO), gravedad_siniestro (LEVE | "
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
        "extraela en calle_1 con su nombre propio completo; NO marques "
        "\"Ubicación no detectada\" ni \"NO ESPECIFICADO\" mientras haya "
        "una via local identificable. REGLA ESTRICTA: si un dato no "
        "figura explicitamente en el texto, asigná exactamente \"NO "
        "ESPECIFICADO\" (o null si es numerico). No inventes ni deduzcas "
        "valores.")
    bruto = _preguntar_gemini(
        texto[:1400], sistema=sistema, max_tokens=600, temperatura=0.1)
    limpio = re.sub(r"^```(?:json)?|```$", "", bruto.strip(),
                    flags=re.M).strip()
    datos = json.loads(limpio)
    return datos if isinstance(datos, dict) else None


def _canon_noticia(datos):
    """Normaliza la salida (Gemini o reglas) a los campos canonicos."""
    ext = {}
    ext["fecha_aprox"] = "NO ESPECIFICADO"
    f = _fecha_de_texto(str(datos.get("fecha_aprox") or ""))
    if f:
        ext["fecha_aprox"] = f.isoformat()
    ext["hora_aprox"] = _hora_de_texto(
        str(datos.get("hora_aprox") or "")) or "NO ESPECIFICADO"
    ext["ciudad"] = _norm_ciudad(datos.get("ciudad")) or "NO ESPECIFICADO"
    for c in ("calle_1", "calle_2"):
        v = str(datos.get(c) or "").strip()
        ext[c] = (" ".join(v.upper().split())
                  if v and normalizar(v) not in ("NO ESPECIFICADO",)
                  and "NO ESPECIFICADO" not in normalizar(v)
                  else "NO ESPECIFICADO")
    ext["modo_desplazamiento_victima"] = (
        _norm_modo(datos.get("modo_desplazamiento_victima"))
        or "NO ESPECIFICADO")
    ext["contraparte_siniestro"] = (
        _norm_contraparte(datos.get("contraparte_siniestro"))
        or "NO ESPECIFICADO")
    ext["gravedad_siniestro"] = (
        _norm_gravedad(datos.get("gravedad_siniestro"))
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
    return ext


def _geocodificar_noticia(ciudad, calle_1, calle_2, usar_nominatim=True):
    """Estima lat/lon cruzando calles con la red vial local (y Nominatim)."""
    consulta = (f"{calle_1} y {calle_2}" if calle_1 and calle_2
                else calle_1 or "")
    if consulta:
        try:
            res = geo_buscar(consulta)
            if res:
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
                    f"{consulta}, {ciudad or 'Perico'}, Jujuy, Argentina",
                    limite=1)
                if res:
                    return {
                        "lat": res[0]["lat"], "lon": res[0]["lon"],
                        "nombre": res[0]["nombre"],
                        "fuente": "nominatim", "confianza": "media",
                    }
            except requests.RequestException:
                pass
    jur = normalizar(ciudad or "PERICO")
    centro = (RADAR_CENTROS.get(jur) or RADAR_CENTROS["PERICO"])
    return {
        "lat": centro[0], "lon": centro[1],
        "nombre": ciudad or "Centro de la jurisdicción",
        "fuente": "centro-ciudad", "confianza": "baja",
    }


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


@app.post("/api/procesar-noticia")
def api_procesar_noticia():
    """Extrae el siniestro desde una cronica periodistica y detecta duplicados.

    Body JSON: {texto, url?}. Con GEMINI_API_KEY usa extraccion IA; si no,
    regex/NLP local. Georreferencia por calle+cruce y compara contra el CSV.
    """
    payload = request.get_json(silent=True) or {}
    texto = str(payload.get("texto") or "").strip()
    url = str(payload.get("url") or "").strip()

    if not texto and url:
        try:
            r = requests.get(
                url,
                headers={"User-Agent": "mineria-valles-jujuy/1.0 "
                                       "(trabajo academico)"},
                timeout=15)
            r.raise_for_status()
            texto = _html_a_texto(r.text)
        except requests.RequestException:
            return jsonify({
                "status": "error",
                "mensaje": ("No se pudo descargar el enlace; pegá el "
                            "texto de la noticia directamente."),
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
        None if ext["calle_2"] == "NO ESPECIFICADO" else ext["calle_2"])
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
    })


# ====== MONITOR DE PRENSA: escaneo automatico de Google News ======

GNEWS_PERIODOS = {
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

    # Consulta estricta de transito jujeño: exige terminos de siniestralidad
    # vial + la provincia + la zona elegida (evita notas de otros paises y
    # falsos positivos como personas apodadas "Perico").
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
    for i, it in enumerate(items):
        ext = _canon_noticia(datos[i] or _extraer_noticia_reglas(it["texto"]))
        if ext["fecha_aprox"] == "NO ESPECIFICADO" and it["fecha_d"]:
            ext["fecha_aprox"] = it["fecha_d"].isoformat()
        geo = _geocodificar_noticia(
            None if ext["ciudad"] == "NO ESPECIFICADO" else ext["ciudad"],
            None if ext["calle_1"] == "NO ESPECIFICADO" else ext["calle_1"],
            None if ext["calle_2"] == "NO ESPECIFICADO" else ext["calle_2"],
            usar_nominatim=False)
        dup_id = _duplicado_noticia(ext, geo)
        noticias.append({
            "id": f"n-{i}",
            "titulo": it["titulo"],
            "medio": it["medio"],
            "url": it["url"],
            "fecha_pub": it["fecha_pub"],
            "extraido": ext,
            "geo": geo,
            "es_duplicado": bool(dup_id),
            "duplicado_id": dup_id,
        })

    return jsonify({
        "status": "ok", "zona": zona, "periodo": when,
        "total": len(noticias), "noticias": noticias,
        "descartados": descartados,
        "fuente": "gemini" if GEMINI_API_KEY else "reglas",
    })


if __name__ == "__main__":
    print("Servidor MODULO 3 en http://localhost:8000")
    app.run(host="127.0.0.1", port=8000, debug=False)
