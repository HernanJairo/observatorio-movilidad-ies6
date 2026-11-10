"""Arranque del MODULO 2: levanta el servidor y abre el navegador.

Uso:
    python run.py            -> http://localhost:8000
    python run.py --puerto 5000
"""

import argparse
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--puerto", type=int, default=8000)
    parser.add_argument("--sin-navegador", action="store_true")
    args = parser.parse_args()

    url = f"http://localhost:{args.puerto}"
    if not args.sin_navegador:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    from servidor import app

    print("=" * 56)
    print("  SINIESTRALIDAD VIAL - VALLES DE JUJUY (MODULO 3)")
    print(f"  Sirviendo en {url}  (Ctrl+C para detener)")
    print("=" * 56)
    app.run(host="127.0.0.1", port=args.puerto, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
