# -*- coding: utf-8 -*-
"""Test de integracion del pipeline de noticias (4 etapas) en lote."""
import sys
sys.path.insert(0, "src")
from servidor import (  # noqa: E402
    _canon_noticia, _extraer_noticia_reglas, _geocodificar_noticia,
    clasificar_jurisdiccion, ENTIDADES_GEO,
)

CASOS = [
    # (texto, jur_esperada, bbox_check o 'OTRA')
    ("Avenida Brasil y Avenida Paterson en San Pedro de Jujuy",
     "SAN PEDRO DE JUJUY"),
    ("Siniestro vial en barrio Gral. Arias, calle Guayaquil",
     "SAN SALVADOR DE JUJUY"),
    ("Choque en Avenida Martijena, Palpala",
     "PALPALA"),
    ("Accidente en Malvinas Argentinas y General Guemes, Perico",
     "PERICO"),
    ("Motociclista caido en Ruta Provincial 56, Monterrico",
     "MONTERRICO"),
    ("Desbarranco en Ruta 52, cerca de Purmamarca", "OTRA"),
    ("Tragedia en Maimara sobre la ruta", "OTRA"),
]

fallas = 0


def check(nombre, ok):
    print(("OK   " if ok else "FALLO") + " " + nombre)
    return bool(ok)


for texto, jur_esp in CASOS:
    jur = clasificar_jurisdiccion(texto)
    ok = check(f"[{jur_esp}] '{texto[:45]}...' -> {jur or 'sin-jur'}",
               jur == jur_esp)
    fallas += not ok
    if jur == "OTRA" or not jur:
        continue
    # Etapa 2+3+4: extraccion y geocodificacion hermetica.
    ext = _canon_noticia(_extraer_noticia_reglas(texto))
    geo = _geocodificar_noticia(ext["ciudad"], ext["calle_1"],
                                ext["calle_2"], usar_nominatim=False,
                                jur_detectada=jur)
    bb = ENTIDADES_GEO[jur]["bbox"]
    dentro = (geo["lat"] is not None
              and bb[1] <= geo["lat"] <= bb[3]
              and bb[0] <= geo["lon"] <= bb[2])
    fallas += not check(
        f"   geo {geo['fuente']} ({geo['lat']},{geo['lon']}) dentro de "
        f"{jur}", dentro)
    fallas += not check(
        f"   sin fallback cruzado ({geo['fuente']})",
        geo["fuente"] in ("perico-interseccion", "perico-calle",
                          "palpala-interseccion", "palpala-calle",
                          "san-pedro-interseccion", "san-pedro-calle",
                          "red-vial", "nominatim",
                          "centroide-jurisdiccion"))

total = 14
print(f"\n{total - fallas}/{total} checks OK" if not fallas
      else f"\n{fallas} FALLAS")
sys.exit(1 if fallas else 0)
