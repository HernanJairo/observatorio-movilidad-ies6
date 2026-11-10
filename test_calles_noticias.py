"""Pruebas del geocodificador de noticias (motor de interseccion local).

Verifica que 'Avenida Malvinas Argentinas y Calle General Guemes'
resuelva el cruce REAL de Perico sin tipeo manual, y que una calle
duplicada devuelva un punto sobre la arteria en Perico.

Uso:  venv/Scripts/python test_calles_noticias.py
"""
import sys

sys.path.insert(0, "src")
from servidor import geocodificar_noticia  # noqa: E402


def check(nombre, cond):
    print(("OK   " if cond else "FALLA") + " " + nombre)
    return cond


fallas = 0

# Prueba 1: cruce real Malvinas Argentinas x General Guemes -> ~(-24.376,-65.118)
r1 = geocodificar_noticia("Avenida Malvinas Argentinas",
                          "Calle General Guemes", "Perico")
print("P1 ->", r1)
fallas += not check(
    "cruce Malvinas Argentinas y Guemes -> coordenadas del cruce real",
    r1["lat"] is not None and abs(r1["lat"] - (-24.376)) < 0.004
    and abs(r1["lon"] - (-65.118)) < 0.008 and r1["confianza"] == "alta")

# Prueba 2: misma calle dos veces -> punto sobre Urquiza en Perico
r2 = geocodificar_noticia("Avenidas Urquiza", "Avenidas Urquiza", "Perico")
print("P2 ->", r2)
fallas += not check(
    "Urquiza + Urquiza (duplicado) -> punto sobre Urquiza en Perico",
    r2["lat"] is not None and -24.43 <= r2["lat"] <= -24.32
    and -65.19 <= r2["lon"] <= -65.04)

# Prueba 3: 'San Pedro: choque en Avenidas Brasil' -> San Pedro de Jujuy.
r3 = geocodificar_noticia("Avenidas Brasil", "Avenidas Brasil",
                          "San Pedro de Jujuy")
print("P3 ->", r3)
fallas += not check(
    "Avenidas Brasil (dup) -> punto sobre Av. Brasil en San Pedro",
    r3["lat"] is not None and abs(r3["lat"] - (-24.233)) < 0.01
    and abs(r3["lon"] - (-64.866)) < 0.01)

# Prueba 4: canon + sanitizador: 'Avenida Savio Somos Jujuy' -> 'Avenida Savio'.
from servidor import _canon_noticia  # noqa: E402
c4 = _canon_noticia({"calle_principal": "Avenida Savio Somos Jujuy",
                     "ciudad_exacta": "San Salvador de Jujuy"})
print("P4 ->", c4["calle_1"], "|", c4["calle_2"])
fallas += not check(
    "sufijo 'Somos Jujuy' removido -> 'AVENIDA SAVIO'",
    c4["calle_1"] == "AVENIDA SAVIO" and c4["calle_2"] == "NO ESPECIFICADO")

# Prueba 5: dedup 'Avenidas Brasil y Avenidas Brasil' -> una sola arteria.
c5 = _canon_noticia({"arteria_principal": "Avenidas Brasil",
                     "arteria_secundaria": "Avenidas Brasil",
                     "ciudad_exacta": "San Pedro de Jujuy"})
print("P5 ->", c5["calle_1"], "|", c5["calle_2"])
fallas += not check(
    "dedup -> solo 'AVENIDAS BRASIL'",
    c5["calle_1"] == "AVENIDAS BRASIL" and c5["calle_2"] == "NO ESPECIFICADO")

# Prueba 6: 'Tragedia en Maimara' -> Otra (fuera de la region Valles).
from servidor import _norm_ciudad  # noqa: E402
print("P6 -> Maimara =>", _norm_ciudad("Maimará"))
fallas += not check("Maimara -> 'Otra'", _norm_ciudad("Maimará") == "Otra")

total = 6
print(f"\n{total - fallas}/{total} pruebas OK" if not fallas
      else f"\n{fallas} FALLAS")
sys.exit(1 if fallas else 0)
