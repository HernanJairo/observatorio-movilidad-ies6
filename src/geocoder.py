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


_PREFIJOS_VIA = {
    "AVENIDA", "AVENIDAS", "AV", "AVDA", "CALLE", "CALLES", "RUTA",
    "PROVINCIAL", "NACIONAL", "RN", "RP", "PASAJE", "PJE", "BOULEVARD",
    "BV", "DIAGONAL", "ACCESO", "COLECTORA", "CAMINO", "ESQ", "C/",
}
_RE_PREFIJOS = re.compile(
    r"^(?:" + "|".join(sorted(_PREFIJOS_VIA, key=len, reverse=True))
    + r")\.?\s+")


def raiz_via(texto: str) -> str:
    """Raiz del nombre de via sin el prefijo de tipo:
    'Avenida Malvinas Argentinas' -> 'MALVINAS ARGENTINAS',
    'Calle General Güemes' -> 'GENERAL GUEMES'."""
    n = normalizar(texto).replace(".", " ")
    n = " ".join(n.split())
    prev = None
    while prev != n:
        prev = n
        n = _RE_PREFIJOS.sub("", n).strip()
    return n


def _score_via(objetivo: str, nombre_via: str) -> float:
    """Similitud consulta-vs-via combinando nombre completo y raiz:
    max(contencion, fuzzy sobre nombre, fuzzy sobre raiz)."""
    nom = normalizar(nombre_via)
    raiz_obj, raiz_nom = raiz_via(objetivo), raiz_via(nombre_via)
    if objetivo and (objetivo in nom or nom in objetivo):
        return 1.0
    if raiz_obj and raiz_obj == raiz_nom:
        return 1.0
    if raiz_obj and raiz_nom and (
            raiz_obj in raiz_nom or raiz_nom in raiz_obj):
        return 0.95
    return max(_similitud(objetivo, nom), _similitud(raiz_obj, raiz_nom))


# Titulos y conectores que se eliminan del nucleo de la arteria:
# 'Calle General Guemes' -> 'guemes', 'J. J. de Urquiza' -> 'j j urquiza'.
_PALABRAS_ACCESORIAS = {
    "general", "gral", "coronel", "cnel", "teniente", "tte", "sargento",
    "sgto", "capitan", "cap", "comodoro", "brigadier", "doctor", "dr",
    "dra", "ingeniero", "ing", "profesor", "padre", "monsenor",
    "de", "del", "la", "las", "el", "los", "y", "e",
}


def limpiar_calle(texto: str) -> str:
    """Normalizacion agresiva para matching de arterias (minusculas, sin
    acentos, sin prefijos de via ni titulos): devuelve el nucleo limpio.
    'Avenida Malvinas Argentinas' -> 'malvinas argentinas',
    'Calle General Guemes' -> 'guemes', 'Avenidas Urquiza' -> 'urquiza'."""
    n = normalizar(texto)  # mayusculas sin acentos: 'AVENIDA X' -> regex ok
    prev = None
    while prev != n:
        prev = n
        n = _RE_PREFIJOS.sub("", n).strip()
    toks = [t.lower() for t in re.split(r"[^A-Z0-9Ñ]+", n)
            if t and t.lower() not in _PALABRAS_ACCESORIAS]
    return " ".join(toks)


# ------------------- Catalogo de arterias de Perico -------------------

RUTA_PERICO = Path(__file__).resolve().parent.parent / "data" / "calles_perico_geometria.json"
BBOX_PERICO = (-65.19, -24.43, -65.04, -24.32)  # min_lon,min_lat,max_lon,max_lat
_CALLES_PERICO = None  # cache: [{norm, nombre, segs:[[(lon,lat),..],..]}]


def cargar_perico() -> list[dict]:
    """Arterias de Perico agrupadas por nucleo limpio: 'guemes' junta
    'General Guemes', 'Pasaje Guemes' y la variante con encoding roto.
    Si el JSON de Perico no existe, se deriva de la red vial completa."""
    global _CALLES_PERICO
    if _CALLES_PERICO is not None:
        return _CALLES_PERICO
    vias = {}
    if RUTA_PERICO.exists():
        crudo = json.loads(RUTA_PERICO.read_text(encoding="utf-8"))
    else:  # fallback: recortar la red vial al bbox de Perico
        crudo = []
        for f in cargar_red():
            nombre = (f["properties"] or {}).get("name")
            if not nombre or f["geometry"]["type"] != "LineString":
                continue
            c = f["geometry"]["coordinates"]
            mid = c[len(c) // 2]
            if (BBOX_PERICO[0] <= mid[0] <= BBOX_PERICO[2]
                    and BBOX_PERICO[1] <= mid[1] <= BBOX_PERICO[3]):
                crudo.append({"nombre": nombre, "coords": c})
    for v in crudo:
        norm = limpiar_calle(v["nombre"])
        if not norm:
            continue
        item = vias.setdefault(norm, {"norm": norm, "nombre": v["nombre"],
                                      "segs": []})
        item["segs"].append(v["coords"])
    _CALLES_PERICO = list(vias.values())
    return _CALLES_PERICO


def _dist_m(p1, p2):
    """Distancia aproximada en metros entre (lat,lon) — suficiente a ~24°S."""
    dy = (p2[0] - p1[0]) * 111320
    dx = (p2[1] - p1[1]) * 101000
    return (dx * dx + dy * dy) ** 0.5


def _punto_en_segmento(lat, lon, a, b):
    """Proyeccion del punto (lat,lon) sobre el segmento a-b=(lon,lat)."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return ax, ay
    t = max(0.0, min(1.0, ((lon - ax) * dx + (lat - ay) * dy) / l2))
    return ax + t * dx, ay + t * dy


def _cruce_segs(segs_a, segs_b, tol_m=25.0):
    """Mejor punto de encuentro entre dos listas de LineStrings: nodos
    compartidos o vertices de una a < tol_m de un segmento de la otra."""
    mejor, dm = None, tol_m
    for ca in segs_a:
        for x, y in ca:
            for cb in segs_b:
                for i in range(len(cb) - 1):
                    px, py = _punto_en_segmento(y, x, cb[i], cb[i + 1])
                    d = _dist_m((y, x), (py, px))
                    if d < dm:
                        dm, mejor = d, ((py + y) / 2, (px + x) / 2)
    return mejor  # (lat, lon) del cruce real


def _match_catalogo(calle, catalogo, umbral=0.55):
    """Mejor via del catalogo por subcadena o similitud del nucleo."""
    raiz = limpiar_calle(calle)
    mejor, best = None, 0.0
    for item in catalogo:
        nom = item["norm"]
        if not raiz or not nom:
            continue
        if raiz == nom:
            score = 1.0
        elif raiz in nom or nom in raiz:
            score = 0.9
        else:
            score = _similitud(raiz, nom)
        if score > best:
            best, mejor = score, item
    return (mejor if mejor and best >= umbral else None)


def _punto_calle(item):
    """Punto medio de una arteria: vertice central de su segmento medio."""
    seg = item["segs"][len(item["segs"]) // 2]
    lon, lat = seg[len(seg) // 2]
    return {"lat": lat, "lon": lon, "nombre": item["nombre"],
            "fuente": "perico-calle", "confianza": "media",
            "coordenadas_validas": True}


# ------------- Catalogo de arterias troncales de Palpalá -------------
# Centro del casco urbano: (-24.256, -65.212). La red vial local no
# cubre Palpala, asi que sus arterias troncales van como segmentos
# cortos centrados en su traza aproximada (lon, lat).
CENTRO_PALPALA = (-24.256, -65.212)

def _seg_palpala(lat, lon, dl=0.0, dn=0.0018):
    """Segmento corto (~400 m) centrado en la traza, con vertices
    intermedios para que el motor de intersecciones cruce las trazas."""
    return [[lon + (dl * i / 4 - dl), lat + (dn * i / 4 - dn)]
            for i in range(9)]


ARTERIAS_PALPALA = {
    # RN 66: tramo sobre la calzada, acceso sur / derivador Palpala.
    "ruta nacional 66": _seg_palpala(-24.2655, -65.2012, dn=0.0035),
    "rn 66": _seg_palpala(-24.2655, -65.2012, dn=0.0035),
    "ruta 66": _seg_palpala(-24.2655, -65.2012, dn=0.0035),
    "martijena": _seg_palpala(-24.2568, -65.2115, dl=0.0028, dn=0.0002),
    "rio de la plata": _seg_palpala(-24.2618, -65.2168, dl=0.0024, dn=0.0002),
    "catalano": _seg_palpala(-24.2515, -65.2090, dl=0.0020, dn=0.0002),
    "libertad": _seg_palpala(-24.2595, -65.2190, dl=0.0020, dn=0.0002),
    "juan jose paso": _seg_palpala(-24.2490, -65.2125, dl=0.0016, dn=0.0002),
}
_PALPALA_CACHE = None


def cargar_palpala() -> list[dict]:
    """Catalogo de arterias troncales de Palpala (nucleo limpio -> segs)."""
    global _PALPALA_CACHE
    if _PALPALA_CACHE is None:
        _PALPALA_CACHE = [
            {"norm": norm, "nombre": nombre.title(), "segs": [seg]}
            for norm, seg, nombre in (
                (k, v, k) for k, v in ARTERIAS_PALPALA.items())
        ]
    return _PALPALA_CACHE


# ------------ Catalogo de arterias troncales de San Pedro ------------
# Centro del casco urbano: (-24.233, -64.866), al este del corredor de
# los Valles. Arterias clave como segmentos cortos (lon, lat).
CENTRO_SAN_PEDRO = (-24.233, -64.866)
ARTERIAS_SANPEDRO = {
    "brasil": _seg_palpala(-24.2307, -64.8665, dn=0.0038),
    "siria": _seg_palpala(-24.2310, -64.8690, dl=0.0020, dn=0.0002),
    "san martin": _seg_palpala(-24.2350, -64.8635, dl=0.0020, dn=0.0002),
    "9 de julio": _seg_palpala(-24.2370, -64.8680, dl=0.0016, dn=0.0002),
    "prolongacion goleta": _seg_palpala(-24.2280, -64.8720, dn=0.0024),
    "goleta": _seg_palpala(-24.2280, -64.8720, dn=0.0024),
    "ruta nacional 34": _seg_palpala(-24.2400, -64.8600, dn=0.0040),
    "ruta provincial 56": _seg_palpala(-24.2260, -64.8580, dl=0.0024, dn=0.0002),
    # Cruce con Av. Brasil: extremo oeste en (-24.2345, -64.8665).
    "paterson": _seg_palpala(-24.2345, -64.86825, dl=0.00175, dn=0.0),
    "rp 1": _seg_palpala(-24.2420, -64.8730, dn=0.0040),
    "ruta provincial 1": _seg_palpala(-24.2420, -64.8730, dn=0.0040),
}
_SANPEDRO_CACHE = None


def cargar_sanpedro() -> list[dict]:
    """Catalogo de arterias troncales de San Pedro (nucleo -> segs)."""
    global _SANPEDRO_CACHE
    if _SANPEDRO_CACHE is None:
        _SANPEDRO_CACHE = [{"norm": k, "nombre": k.title(), "segs": [v]}
                           for k, v in ARTERIAS_SANPEDRO.items()]
    return _SANPEDRO_CACHE


# ------- Catalogo minimo de arterias de San Salvador de Jujuy -------
# Centroide (-24.185, -65.299). Anclajes de barrios citados en prensa.
ARTERIAS_SSJ = {
    "guayaquil": _seg_palpala(-24.203, -65.292, dl=0.0020, dn=0.0002),
    # Av. General Savio: Acceso Sur / Barrio Coronel Arias, no el centro.
    "savio": _seg_palpala(-24.1985, -65.2920, dn=0.0035),
    "general savio": _seg_palpala(-24.1985, -65.2920, dn=0.0035),
    "avenida savio": _seg_palpala(-24.1985, -65.2920, dn=0.0035),
    "martiarena": _seg_palpala(-24.214, -65.285, dl=0.0024, dn=0.0002),
    "gorriti": _seg_palpala(-24.184, -65.308, dn=0.0024),
    "19 de abril": _seg_palpala(-24.186, -65.290, dl=0.0024, dn=0.0002),
    "canonigo gorriti": _seg_palpala(-24.184, -65.308, dn=0.0024),
}
_SSJ_CACHE = None


def cargar_ssj() -> list[dict]:
    """Catalogo minimo de arterias de San Salvador (nucleo -> segs)."""
    global _SSJ_CACHE
    if _SSJ_CACHE is None:
        _SSJ_CACHE = [{"norm": k, "nombre": k.title(), "segs": [v]}
                      for k, v in ARTERIAS_SSJ.items()]
    return _SSJ_CACHE


# ------- Catalogo de corredores interurbanos (rutas provinciales y
# nacionales): conectan los municipios del corredor y existen en TODAS
# las jurisdicciones. Las trazas son segmentos cortos sobre la calzada.
RUTAS_VALLES = {
    # RP 46: Perico -> Santo Domingo -> Monterrico. JAMAS mapear a RP 41.
    "rp 46": _seg_palpala(-24.4020, -65.1580, dl=0.006, dn=-0.003),
    "ruta 46": _seg_palpala(-24.4020, -65.1580, dl=0.006, dn=-0.003),
    "ruta provincial 46": _seg_palpala(-24.4020, -65.1580, dl=0.006, dn=-0.003),
    "46": _seg_palpala(-24.4020, -65.1580, dl=0.006, dn=-0.003),
    # RP 41: Lobaton -> empalme RP 1 en Rodeito (OSM: -24.3936, -64.8488).
    "rp 41": _seg_palpala(-24.39366, -64.84887, dl=0.004, dn=0.004),
    "ruta 41": _seg_palpala(-24.39366, -64.84887, dl=0.004, dn=0.004),
    "ruta provincial 41": _seg_palpala(-24.39366, -64.84887, dl=0.004, dn=0.004),
    "41": _seg_palpala(-24.39366, -64.84887, dl=0.004, dn=0.004),
    # RP 42: tramo Monterrico - El Carmen.
    "rp 42": _seg_palpala(-24.4310, -65.1850, dl=-0.003, dn=0.004),
    "ruta 42": _seg_palpala(-24.4310, -65.1850, dl=-0.003, dn=0.004),
    "ruta provincial 42": _seg_palpala(-24.4310, -65.1850, dl=-0.003, dn=0.004),
    # RN 9: acceso norte / tramo Yala - San Salvador.
    "rn 9": _seg_palpala(-24.1500, -65.3350, dl=0.002, dn=0.004),
    "ruta 9": _seg_palpala(-24.1500, -65.3350, dl=0.002, dn=0.004),
    "ruta nacional 9": _seg_palpala(-24.1500, -65.3350, dl=0.002, dn=0.004),
    # RN 66: traza autopista Perico - Palpala.
    "rn 66": _seg_palpala(-24.2850, -65.1800, dl=0.003, dn=0.004),
    "ruta 66": _seg_palpala(-24.2850, -65.1800, dl=0.003, dn=0.004),
    "ruta nacional 66": _seg_palpala(-24.2850, -65.1800, dl=0.003, dn=0.004),
    # RP 1: traza San Salvador - Palpala - San Pedro.
    "rp 1": _seg_palpala(-24.2600, -64.9800, dl=0.006, dn=0.002),
    "ruta 1": _seg_palpala(-24.2600, -64.9800, dl=0.006, dn=0.002),
    "ruta provincial 1": _seg_palpala(-24.2600, -64.9800, dl=0.006, dn=0.002),
    # RN 34: tramo San Pedro / Pampa Blanca.
    "rn 34": _seg_palpala(-24.2400, -64.8600, dl=0.004, dn=0.002),
    "ruta 34": _seg_palpala(-24.2400, -64.8600, dl=0.004, dn=0.002),
    "ruta nacional 34": _seg_palpala(-24.2400, -64.8600, dl=0.004, dn=0.002),
}
_RUTAS_CACHE = None


def cargar_rutas() -> list[dict]:
    """Catalogo de rutas interurbanas del corredor (nucleo -> segs)."""
    global _RUTAS_CACHE
    if _RUTAS_CACHE is None:
        _RUTAS_CACHE = [{"norm": k, "nombre": k.title(), "segs": [v]}
                        for k, v in RUTAS_VALLES.items()]
    return _RUTAS_CACHE


def catalogo_de(jur: str | None) -> list[dict]:
    """Catalogo local de la jurisdiccion + corredores interurbanos."""
    n = normalizar(jur)
    if n == "PALPALA":
        base = cargar_palpala()
    elif n == "SAN PEDRO DE JUJUY":
        base = cargar_sanpedro()
    elif n == "SAN SALVADOR DE JUJUY":
        base = cargar_ssj()
    else:
        base = cargar_perico()
    # Las rutas RP/RN existen en toda la region: las suma cualquier
    # jurisdiccion (un choque en "RP 46" geocodifica aunque sea de Perico).
    return base + cargar_rutas()


def sugerir_palpala(texto: str) -> list[dict]:
    """Sugerencias del catalogo de Palpala por subcadena/similitud."""
    return _sugerir_catalogo(texto, cargar_palpala())


def sugerir_sanpedro(texto: str) -> list[dict]:
    """Sugerencias del catalogo de San Pedro por subcadena/similitud."""
    return _sugerir_catalogo(texto, cargar_sanpedro())


def _sugerir_catalogo(texto, catalogo):
    raiz = limpiar_calle(texto)
    if len(raiz) < 2:
        return []
    out = []
    for item in catalogo:
        nom = item["norm"]
        score = 1.0 if raiz == nom else (
            0.9 if raiz in nom or nom in raiz else _similitud(raiz, nom))
        if score >= 0.6:
            c = _punto_calle(item)
            c.update({"score": round(score, 3), "fuente": "local"})
            out.append(c)
    return sorted(out, key=lambda r: r["score"], reverse=True)


def _norm_ruta(nombre: str | None) -> str:
    """Canoniza variantes de rutas: 'RP N° 1', 'RP1', 'Ruta Prov 1',
    'Ruta Provincial numero 1' -> 'RUTA PROVINCIAL 1'; idem nacionales."""
    t = normalizar(nombre or "")
    t = re.sub(r"\bRUTA\s+PROV(?:INCIAL|INCIA)?\.?\s*(?:N[°Oº.]?\s*|NUMERO\s+)?(\d+)",
               r"RUTA PROVINCIAL \1", t)
    t = re.sub(r"\bRP\s*[°N.]?\s*(\d+)", r"RUTA PROVINCIAL \1", t)
    t = re.sub(r"\bRUTA\s+NACIONAL\.?\s*(?:N[°Oº.]?\s*|NUMERO\s+)?(\d+)",
               r"RUTA NACIONAL \1", t)
    t = re.sub(r"\bRN\s*[°N.]?\s*(\d+)", r"RUTA NACIONAL \1", t)
    t = re.sub(r"\bRUTA\s+(\d+)", r"RUTA PROVINCIAL \1", t)
    return t


# Cruces de corredores con coordenada exacta conocida: la interseccion
# manda sobre cualquier centroide urbano.
CRUCES_RUTAS = {
    # Empalme real sobre calzada (OSM): Rodeito / ejido San Pedro.
    frozenset(("RUTA PROVINCIAL 1", "RUTA PROVINCIAL 41")):
        (-24.39366, -64.84887, "Empalme RP 1 y RP 41 (Rodeito / San Pedro)"),
}


def geo_google(arteria_1, arteria_2=None, jurisdiccion="Perico"):
    """Geocodificacion principal via Google Maps Geocoding API.
    Devuelve dict con lat/lon/nombre o None si ZERO_RESULTS o sin key."""
    import os, urllib.parse
    import requests as _req
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        try:  # fallback: leer .env directo
            for linea in open(".env", encoding="utf-8"):
                if linea.startswith("GOOGLE_MAPS_API_KEY="):
                    key = linea.split("=", 1)[1].strip()
        except OSError:
            pass
    if not key:
        return None
    query = str(arteria_1 or "").strip()
    if arteria_2 and str(arteria_2).lower() not in (
            "none", "null", "ninguno", ""):
        query += f" y {str(arteria_2).strip()}"
    jur_map = {"SAN PEDRO DE JUJUY": "San Pedro de Jujuy",
               "PERICO": "Perico", "MONTERRICO": "Monterrico",
               "EL CARMEN": "El Carmen", "PALPALA": "Palpala",
               "SAN SALVADOR DE JUJUY": "San Salvador de Jujuy"}
    jur_txt = (jur_map.get(normalizar(jurisdiccion),
                           str(jurisdiccion or "").strip()) or "")
    query += f", {jur_txt + ', ' if jur_txt else ''}Jujuy, Argentina"
    try:
        r = _req.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": key}, timeout=8)
        resp = r.json()
    except Exception:
        return None
    if resp.get("status") == "OK" and resp.get("results"):
        loc = resp["results"][0]["geometry"]["location"]
        return {"lat": loc["lat"], "lon": loc["lng"],
                "nombre": resp["results"][0]["formatted_address"],
                "fuente": "google-maps", "confianza": "alta",
                "coordenadas_validas": True}
    return None


def cruzar_calles(calle_1: str, calle_2: str,
                  jur: str = "PERICO") -> dict | None:
    """Interseccion real entre dos arterias con tolerancia difusa: ambas
    se limpian con limpiar_calle() y se buscan por subcadena/similitud
    en el catalogo de la jurisdiccion (Perico o Palpala).

    - Las dos existen -> punto geometrico de interseccion de LineStrings.
    - Solo existe una -> punto medio de esa calle.
    - Ninguna -> None (el llamador decide el fallback).
    """
    n1, n2 = _norm_ruta(calle_1), _norm_ruta(calle_2)
    calle_1, calle_2 = n1 or calle_1, n2 or calle_2
    es_par_rutas = bool(
        re.match(r"^RUTA (PROVINCIAL|NACIONAL) \d+$", n1 or "")
        and re.match(r"^RUTA (PROVINCIAL|NACIONAL) \d+$", n2 or ""))
    # Empalmes de rutas provinciales curados (Google los desvia o falla).
    if es_par_rutas:
        par = CRUCES_RUTAS.get(frozenset((n1, n2)))
        if par:
            return {"lat": par[0], "lon": par[1], "nombre": par[2],
                    "fuente": "rutas-interseccion", "confianza": "alta",
                    "coordenadas_validas": True}
    # PASO 1: Google Maps Geocoding API, prioridad absoluta para
    # esquinas urbanas (Brasil y Haiti -> esquina real, no la plaza).
    g = geo_google(calle_1, calle_2, jur)
    if g:
        return g
    catalogo = catalogo_de(jur)
    tag = {"PALPALA": "palpala", "SAN PEDRO DE JUJUY": "san-pedro",
           "SAN SALVADOR DE JUJUY": "ssj"}.get(normalizar(jur), "perico")
    m1 = _match_catalogo(calle_1, catalogo)
    m2 = _match_catalogo(calle_2, catalogo)

    if m1 and m2 and m1["norm"] == m2["norm"]:
        m2 = None  # mismo nombre: es una sola arteria, no un cruce
    if m1 and m2:
        cruce = _cruce_segs(m1["segs"], m2["segs"])
        if cruce:
            return {"lat": cruce[0], "lon": cruce[1],
                    "nombre": f'{m1["nombre"]} y {m2["nombre"]}',
                    "fuente": f"{tag}-interseccion", "confianza": "alta",
                    "coordenadas_validas": True}
        # Las dos existen pero no se tocan fisicamente: la primera.
        r = _punto_calle(m1)
        r["fuente"] = f"{tag}-calle"
        return r
    r = _punto_calle(m1 or m2) if (m1 or m2) else None
    if r:
        r["fuente"] = f"{tag}-calle"
    return r


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
    """Busca vias por nombre en la red local con fuzzy matching sobre el
    nombre completo y su raiz ('Av. Malvinas Argentinas' == 'Malvinas
    Argentinas'). Devuelve coincidencias ordenadas por similitud."""
    objetivo = normalizar(texto)
    resultados = []
    for feature in cargar_red():
        nombre = feature["properties"].get("name")
        if not nombre:
            continue
        score = _score_via(objetivo, nombre)
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
            and _score_via(objetivo, f["properties"]["name"]) >= umbral
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
        n1, n2 = _norm_ruta(partes[0]), _norm_ruta(partes[1])
        par = CRUCES_RUTAS.get(frozenset((n1, n2)))
        if par:
            return [{"lat": par[0], "lon": par[1], "nombre": par[2],
                     "fuente": "rutas-interseccion", "score": 1.0}]
        return buscar_interseccion(partes[0], partes[1])
    return buscar_calle(texto)


def buscar_nominatim(texto: str, limite: int = 5,
                     viewbox: tuple | None = None) -> list[dict]:
    """Busca en Nominatim acotado a un bounding box estricto.

    viewbox: (min_lon, min_lat, max_lon, max_lat) — por defecto el corredor
    de los Valles. 'bounded=1' impide que una calle de Perico resuelva en
    otra jurisdiccion o fuera de zona.
    """
    params = {
        "q": texto,
        "format": "jsonv2",
        "limit": limite,
        "countrycodes": "ar",
        "viewbox": ",".join(map(str, viewbox or VIEWBOX_JUJUY)),
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
