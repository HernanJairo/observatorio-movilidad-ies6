# -*- coding: utf-8 -*-
"""Suite exhaustiva: filtro San Pedro, geocodificacion hermetica y
dilemas dinamicos."""
import sys
sys.path.insert(0, "src")
from servidor import (  # noqa: E402
    clasificar_jurisdiccion, geocodificar_noticia, _dilema_ia,
    _dilema_a_tarjeta,
)

fallas = 0


def check(nombre, ok):
    print(("OK   " if ok else "FALLO") + " " + nombre)
    return bool(ok)


# 1) Noticias de la capital NO entran en la bandeja de San Pedro.
t1 = clasificar_jurisdiccion(
    "Herido en Hospital San Roque tras choque en Gorriti")
fallas += not check("Hospital San Roque/Gorriti -> no San Pedro",
                    t1 in ("SAN SALVADOR DE JUJUY", "OTRA"))
t1b = clasificar_jurisdiccion("Siniestro en Av. Savio, Martiarena")
fallas += not check("Savio/Martiarena -> SSJ", t1b == "SAN SALVADOR DE JUJUY")

# 2) Avenida Brasil -> San Pedro, nunca Perico.
r = geocodificar_noticia("Avenida Brasil", None, "San Pedro de Jujuy")
print("T2 ->", r["fuente"], r["lat"], r["lon"])
fallas += not check("Av. Brasil en San Pedro (-24.23, -64.86)",
                    r["lat"] is not None and -64.88 <= r["lon"] <= -64.85)

# 3) RP 1 -> acceso de San Pedro (-24.242, -64.873).
r3 = geocodificar_noticia("RP 1", None, "San Pedro de Jujuy")
print("T3 ->", r3["fuente"], r3["lat"], r3["lon"])
fallas += not check("RP 1 sobre traza de acceso a San Pedro",
                    r3["lat"] is not None
                    and abs(r3["lat"] - (-24.242)) < 0.01
                    and abs(r3["lon"] - (-64.873)) < 0.02)

# 4) POST /api/generar-dilema-ia -> 200 + claves del contrato.
from servidor import app  # noqa: E402
cli = app.test_client()
r = cli.post("/api/generar-dilema-ia", json={"usados": []})
dj = r.get_json() or {}
dil = dj.get("dilema", {})
print("T4 ->", r.status_code, dil.get("fuente"))
fallas += not check(
    "endpoint 200 + escenario/opcion_a/opcion_b/correcta",
    r.status_code == 200
    and all(dil.get(k) for k in
            ("escenario", "opcion_a", "opcion_b", "correcta")))
# Dos llamadas: contenido distinto o al menos valido siempre.
r2 = cli.post("/api/generar-dilema-ia", json={"usados": []})
fallas += not check("segunda llamada tambien 200",
                    r2.status_code == 200
                    and (r2.get_json() or {}).get("dilema", {}).get(
                        "escenario"))

# 5) Avenida Savio -> -24.1985 (Acceso Sur), NO -24.180 centro.
r5 = geocodificar_noticia("Avenida Savio", None, "San Salvador de Jujuy")
print("T5 ->", r5["fuente"], r5["lat"], r5["lon"])
fallas += not check(
    "Savio ~-24.198 (nunca -24.180 Salta/Balcarce)",
    r5["lat"] is not None and abs(r5["lat"] - (-24.1985)) < 0.01)

# 6) Brasil x Paterson -> jurisdiccion San Pedro + dentro de su bbox.
j6 = clasificar_jurisdiccion(
    "Choque en Avenida Brasil y Avenida Paterson, San Pedro")
r6 = geocodificar_noticia("Avenida Brasil", "Avenida Paterson",
                          "San Pedro de Jujuy")
print("T6 ->", j6, r6["fuente"], r6["lat"], r6["lon"])
fallas += not check("jur == SAN PEDRO DE JUJUY",
                    j6 == "SAN PEDRO DE JUJUY")
fallas += not check(
    "cruce dentro del bbox de San Pedro",
    r6["lat"] is not None and -64.91 <= r6["lon"] <= -64.82
    and -24.28 <= r6["lat"] <= -24.19)

total = 9
print(f"\n{total - fallas}/{total} checks OK" if not fallas
      else f"\n{fallas} FALLAS")
sys.exit(1 if fallas else 0)
