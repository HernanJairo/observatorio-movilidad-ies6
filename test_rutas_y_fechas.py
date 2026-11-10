"""Suite de verificacion: empalme RP1xRP41 y parseo de fecha exacta."""
import sys
sys.path.insert(0, 'src')
sys.path.insert(0, '.')
import geocoder
from servidor import _parse_fecha_libre

# 1) Empalme real RP 1 x RP 41 (Rodeito / San Pedro) - coordenada OSM.
r = geocoder.buscar("Ruta Provincial 1 y Ruta Provincial 41")
punto = [r[0]["lat"], r[0]["lon"]]
assert punto == [-24.39366, -64.84887], punto
print("Test 1 OK - empalme RP1xRP41:", punto, "-", r[0]["nombre"])

# 2) Fecha estricta 'D de mes de aaaa' -> AAAA-MM-DD.
fecha = _parse_fecha_libre("Sábado, 24 de octubre de 2026 09:32")
assert fecha.isoformat() == "2026-10-24", fecha
print("Test 2 OK - fecha:", fecha.isoformat())

# 3) Variantes del empalme.
for q in ("RP 1 y RP 41", "rp1 y rp41", "Ruta 1 y Ruta 41"):
    r = geocoder.buscar(q)
    p = [r[0]["lat"], r[0]["lon"]]
    assert p == [-24.39366, -64.84887], (q, p)
    print("  variante OK:", q, "->", p)

print("TODOS LOS TESTS PASARON")
