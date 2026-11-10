# -*- coding: utf-8 -*-
"""Verificacion de inconsistencias geograficas en prensa.

Casos:
  1) 'Avenida Brasil y Avenida Paterson En San Pedro De Jujuy'
     -> San Pedro de Jujuy (NO Perico), cruce en ~(-24.234, -64.868).
  2) 'Rutas Provinciales 1 y Ruta Provincial El Tribuno De Jujuy'
     -> arteria limpia 'RP 1', ciudad San Pedro de Jujuy.
  3) 'Siniestro vial en barrio Gral. Arias, calle Guayaquil'
     -> San Salvador de Jujuy (descartable si el filtro es Perico).
  4) 'Desbarranco en Ruta 52' -> 'Otra' (descartada de El Carmen).
"""
import sys
from servidor import (  # noqa: E402
    _canon_noticia, _jurisdiccion_en_texto, _norm_ciudad,
    _sanear_arteria, geocodificar_noticia,
)

fallas = 0


def check(nombre, ok):
    print(("OK   " if ok else "FALLO") + " " + nombre)
    return bool(ok)


# --- Caso 1: cruce Brasil x Paterson debe ir a SAN PEDRO, no a Perico ---
r = geocodificar_noticia(
    "Avenida Brasil", "Avenida Paterson En San Pedro De Jujuy",
    "San Pedro de Jujuy")
print("C1 ->", r)
fallas += not check(
    "Brasil x Paterson -> San Pedro (lat/lon en San Pedro, nunca Perico)",
    r["lat"] is not None and -24.25 <= r["lat"] <= -24.22
    and -64.88 <= r["lon"] <= -64.85)

# --- Caso 2: 'RP 1' limpia; 'El Tribuno De Jujuy' no es calle ---
c = _canon_noticia({
    "arteria_principal": "Rutas Provinciales 1",
    "arteria_secundaria": "Ruta Provincial El Tribuno De Jujuy",
    "ciudad_exacta": "San Pedro de Jujuy"})
print("C2 ->", c["calle_1"], "|", c["calle_2"], "|", c["ciudad"])
fallas += not check(
    "arteria limpia == 'RP 1' y secundaria None",
    c["calle_1"] == "RP 1" and c["calle_2"] == "NO ESPECIFICADO")
fallas += not check(
    "ciudad == San Pedro de Jujuy",
    c["ciudad"] == "San Pedro de Jujuy")
r2 = geocodificar_noticia("Rutas Provinciales 1", "", "San Pedro de Jujuy")
print("C2 geo ->", r2)
fallas += not check(
    "RP 1 -> tramo de acceso a San Pedro",
    r2["lat"] is not None and -64.88 <= r2["lon"] <= -64.82)

# --- Caso 3: Gral. Arias + Guayaquil -> San Salvador de Jujuy ---
t3 = "Siniestro vial en barrio Gral. Arias, calle Guayaquil"
j3 = _jurisdiccion_en_texto(t3)
print("C3 ->", j3)
fallas += not check(
    "Gral. Arias/Guayaquil -> San Salvador de Jujuy",
    j3 == "SAN SALVADOR DE JUJUY")

# --- Caso 4: Ruta 52 -> 'Otra', descartable de El Carmen ---
j4 = _jurisdiccion_en_texto("Desbarranco en Ruta 52")
print("C4 ->", j4)
fallas += not check("Ruta 52 -> 'OTRA' (fuera de region)", j4 == "OTRA")
fallas += not check("Maimara -> 'Otra'", _norm_ciudad("Maimará") == "Otra")

# Extra: sanitizador de la cola 'En San Pedro De Jujuy'
print("extra ->", _sanear_arteria(
    "Avenida Paterson En San Pedro De Jujuy",
    "Avenida Paterson En San Pedro De Jujuy"))
fallas += not check(
    "cola de ciudad removida -> 'AVENIDA PATERSON'",
    _sanear_arteria("Avenida Paterson En San Pedro De Jujuy",
                    "Avenida Paterson En San Pedro De Jujuy")
    == "AVENIDA PATERSON")

total = 8
print(f"\n{total - fallas}/{total} checks OK" if not fallas
      else f"\n{fallas} FALLAS")
sys.exit(1 if fallas else 0)
