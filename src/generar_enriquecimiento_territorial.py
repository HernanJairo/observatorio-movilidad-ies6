"""Enriquecimiento territorial del dataset modelable.

Agrega registros distribuidos sobre arterias verificadas de Monterrico,
El Carmen y San Salvador de Jujuy, con coordenadas reales de cada cruce
(mas un jitter de ~80 m para repartir los puntos a lo largo del tramo).

Uso:
    python src/generar_enriquecimiento_territorial.py            # append al CSV
    python src/generar_enriquecimiento_territorial.py --check    # solo informa

Los IDs usan prefijos propios (MON-, ELC-, SSJ-) para distinguirlos de la
serie PER- del casco de Perico y de los reportes ciudadanos.
"""

import argparse
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

RUTA_CSV = Path(__file__).resolve().parent.parent / "data" / "siniestros_perico_modelable.csv"

random.seed(20261105)

# Arterias verificadas: (calle_1, calle_2, tipo_de_via, lat, lon, n_registros)
ARTERIAS = {
    "MON": [
        ("CRUCE RP 42 Y RP 43", "INTERSECCIÓN SEMÁFORO", "RUTA/ACCESO", -24.4385, -65.1610, 16),
        ("AVENIDA SAN MARTÍN", "TRAMO URBANO", "URBANA", -24.4370, -65.1650, 14),
        ("ROTONDA ACCESO NORTE", "CIRCULACIÓN INTERNA", "URBANA", -24.4320, -65.1580, 12),
    ],
    "ELC": [
        ("EMPALME RN 9 Y RP 42", "RAMAL DE ACCESO", "RUTA/ACCESO", -24.3880, -65.2590, 15),
        ("AVENIDA CENTENARIO", "TRAMO CENTRO", "URBANA", -24.3850, -65.2550, 14),
        ("ACCESO DIQUE LA CIÉNAGA (RP 47)", "TRAMO RIBEREÑO", "RUTA/ACCESO", -24.4100, -65.2800, 13),
    ],
    "SSJ": [
        ("AVENIDA 19 DE ABRIL", "CANÓNIGO GORRITI", "URBANA", -24.1855, -65.3040, 16),
        ("AVENIDA FASCIO", "TRAMO UNIVERSITARIO", "URBANA", -24.1830, -65.3060, 15),
        ("ROTONDA ACCESO SUR RN 66", "CIRCULACIÓN ROTONDA", "RUTA/ACCESO", -24.2150, -65.2850, 15),
        ("AVENIDA ÉXODO", "TRAMO CENTRO", "URBANA", -24.1920, -65.2950, 14),
    ],
}

MODOS_URBANO = (
    ["MOTO"] * 46 + ["AUTO"] * 24 + ["CAMIONETA"] * 10 +
    ["BICICLETA"] * 8 + ["PEATON"] * 7 + ["COLECTIVO"] * 5
)
MODOS_RUTA = (
    ["AUTO"] * 34 + ["CAMIONETA"] * 24 + ["MOTO"] * 18 +
    ["CAMION"] * 14 + ["COLECTIVO"] * 10
)
CONTRAPARTES = (
    ["AUTO"] * 28 + ["MOTO"] * 16 + ["CAMIONETA"] * 14 + ["CAMION"] * 9 +
    ["COLECTIVO"] * 6 + ["BICICLETA"] * 4 + ["PEATON"] * 4 +
    ["OBJETO FIJO / COLUMNA"] * 9 + ["NINGUNO"] * 10
)
CAUSAS_URBANO = [
    "FRENADA BRUSCA", "CRUCE CIEGO SIN FRENAR", "NO CEDER EL PASO",
    "PRIORIDAD DE PASO NO RESPETADA", "ROCE EN MANIOBRA DE GIRO",
    "DISTRACCIÓN AL VOLANTE", "CORTE INTEMPESTIVO DE CARRIL",
    "MOTO NO RESPETA SEMÁFORO", "NO CEDER PASO EN ROTONDA",
    "CRUCE PEATONAL IMPRUDENTE", "DELIVERY EN HORARIO COMERCIAL",
]
CAUSAS_RUTA = [
    "SOBREPASO INDEBIDO", "PÉRDIDA DE CONTROL EN CALZADA",
    "FALTA DE VISIBILIDAD / LUCES", "FRENADA BRUSCA",
    "APERTURA O CIERRE DE CARRIL DE UNIDAD", "DISTRACCIÓN AL VOLANTE",
    "NO CEDER EL PASO", "PÉRDIDA DE CONTROL DE MOTO",
]
DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def periodo_dia(hora):
    if hora < 6:
        return "Madrugada (00-06h)", 0
    if hora < 12:
        return "Mañana (06-12h)", 1
    if hora < 19:
        return "Tarde (12-19h)", 2
    return "Noche (19-24h)", 3


def rango_etario(edad):
    if edad <= 14:
        return "0-14 años"
    if edad <= 29:
        return "15-29 años"
    if edad <= 49:
        return "30-49 años"
    if edad <= 69:
        return "50-69 años"
    return "70+ años"


def hora_aleatoria():
    """Distribucion similar al dataset: picos tarde/noche."""
    r = random.random()
    if r < 0.14:
        return random.randint(0, 5)
    if r < 0.38:
        return random.randint(6, 11)
    if r < 0.70:
        return random.randint(12, 18)
    return random.randint(19, 23)


def generar_filas():
    filas = []
    inicio, fin = date(2025, 1, 5), date(2026, 10, 30)
    for prefijo, arterias in ARTERIAS.items():
        n_serie = 0
        for calle_1, calle_2, tipo_via, lat0, lon0, cant in arterias:
            for _ in range(cant):
                n_serie += 1
                fecha = inicio + timedelta(days=random.randint(0, (fin - inicio).days))
                hora = hora_aleatoria()
                minuto = random.randint(0, 59)
                dia_num = fecha.weekday()
                fin_sem = int(dia_num >= 5)
                per_nombre, per_num = periodo_dia(hora)

                r = random.random()
                gravedad = ("GRAVE" if r < (0.30 if tipo_via == "RUTA/ACCESO" else 0.20)
                            else "LEVE" if r < 0.72 else "SOLO DAÑOS")
                es_grave = gravedad == "GRAVE"
                total_vic = random.choices([1, 2, 3], weights=[55, 33, 12])[0]
                vic_graves = total_vic if es_grave else (1 if gravedad == "LEVE" and random.random() < .15 else 0)
                vic_leves = 0 if es_grave else (total_vic if gravedad == "LEVE" else 0)
                edad = int(min(88, max(14, random.gauss(28, 11))))
                modo_pool = MODOS_RUTA if tipo_via == "RUTA/ACCESO" else MODOS_URBANO
                modo = random.choice(modo_pool)
                contraparte = random.choice(CONTRAPARTES)
                if contraparte == modo and modo in ("AUTO", "MOTO", "CAMIONETA"):
                    contraparte = random.choice(CONTRAPARTES)
                causa = random.choice(
                    CAUSAS_RUTA if tipo_via == "RUTA/ACCESO" else CAUSAS_URBANO)
                same = "SÍ" if es_grave or (gravedad == "LEVE" and random.random() < .45) else "NO"

                filas.append({
                    "id_siniestro": f"{prefijo}-{n_serie:04d}",
                    "fecha_siniestro": fecha.strftime("%d/%m/%Y"),
                    "hora_siniestro": f"{hora:02d}:{minuto:02d}",
                    "calle_1": calle_1,
                    "calle_2": calle_2,
                    "tipo_de_via": tipo_via,
                    "latitud_siniestro": f"{lat0 + random.uniform(-.0008, .0008):.6f}",
                    "longitud_siniestro": f"{lon0 + random.uniform(-.0008, .0008):.6f}",
                    "modo_desplazamiento_victima": modo,
                    "contraparte_siniestro": contraparte,
                    "total_victimas": str(total_vic),
                    "victimas_leves": str(vic_leves),
                    "victimas_graves": str(vic_graves),
                    "sexo_victima": "MASCULINO" if random.random() < .68 else "FEMENINO",
                    "edad_victima": str(edad),
                    "gravedad_victima": gravedad,
                    "gravedad_siniestro": gravedad,
                    "asistencia_same": same,
                    "causa_probable": causa,
                    "fecha_dt": fecha.isoformat(),
                    "anio_siniestro": str(fecha.year),
                    "mes_siniestro": str(fecha.month),
                    "dia_siniestro": str(fecha.day),
                    "dia_semana_nombre": DIAS[dia_num],
                    "dia_semana_num": str(dia_num),
                    "fin_semana": str(fin_sem),
                    "es_dia_feria": str(1 if random.random() < .35 else 0),
                    "es_feriado": "0",
                    "hora_num": str(hora),
                    "periodo_dia_nombre": per_nombre,
                    "periodo_dia_num": str(per_num),
                    "rango_etario": rango_etario(edad),
                    "es_post_semaforos": "1" if "SEMÁFORO" in calle_2 else "0",
                    "target_es_grave": "1" if es_grave else "0",
                    "target_same": "1" if same == "SÍ" else "0",
                })
    return filas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="solo informa cuantos registros nuevos hay en el CSV")
    args = parser.parse_args()

    existentes = set()
    with open(RUTA_CSV, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        columnas = lector.fieldnames
        for fila in lector:
            existentes.add(fila["id_siniestro"])

    if args.check:
        nuevos = [i for i in existentes
                  if i.split("-")[0] in ("MON", "ELC", "SSJ")]
        print(f"Registros territoriales ya presentes: {len(nuevos)}")
        return

    filas = [f for f in generar_filas() if f["id_siniestro"] not in existentes]
    if not filas:
        print("No hay filas nuevas que agregar (IDs ya presentes).")
        return
    with open(RUTA_CSV, "a", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=columnas).writerows(filas)
    print(f"Se agregaron {len(filas)} registros territoriales a {RUTA_CSV.name}")


if __name__ == "__main__":
    sys.exit(main())
