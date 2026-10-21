"""
Monitoreo - Recolector de Google News (v2: Marca / Adversarios / Ciudad)

Lee config.json, hace las búsquedas de cada sección, cuenta notas por
persona, detecta cuando Google llega a su tope de ~100 resultados y
guarda todo en salida/notas_FECHA.json.

Uso:
    python3 recolector_gnews.py
"""

import json
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path

VENTANA_HORAS = 24
UMBRAL_DUPLICADO = 0.85
TOPE_GOOGLE = 90          # si una búsqueda trae 90+ resultados, asumimos que llegó al tope (~100)
CARPETA_SALIDA = Path("salida")
RSS_BASE = "https://news.google.com/rss/search?q={q}&hl=es-419&gl=MX&ceid=MX:es-419"


def cargar_config() -> dict:
    return json.loads(Path("config.json").read_text(encoding="utf-8"))


def descargar(busqueda: str) -> bytes:
    url = RSS_BASE.format(q=urllib.parse.quote(f"{busqueda} when:1d"))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def parsear_rss(xml_bytes: bytes) -> list[dict]:
    notas = []
    for item in ET.fromstring(xml_bytes).iter("item"):
        titulo = (item.findtext("title") or "").strip()
        fuente_el = item.find("source")
        fuente = fuente_el.text.strip() if fuente_el is not None and fuente_el.text else ""
        if fuente and titulo.endswith(f" - {fuente}"):
            titulo = titulo[: -len(f" - {fuente}")]
        try:
            fecha = parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            fecha = None
        notas.append({"titulo": titulo, "fuente": fuente, "fecha": fecha,
                      "link": (item.findtext("link") or "").strip()})
    return notas


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", texto).strip()


def secciones_config(cfg: dict) -> list[dict]:
    """Orden = prioridad para asignar cada nota a UNA sección:
    adversarios primero (son pocos y no queremos perderlos), luego marca, al final ciudad."""
    return cfg["adversarios"] + [cfg["marca"], cfg["ciudad"]]


def recolectar(cfg: dict) -> dict:
    limite = datetime.now(timezone.utc) - timedelta(hours=VENTANA_HORAS)
    por_link: dict[str, dict] = {}
    conteos = {}

    for sec in secciones_config(cfg):
        links_sec, tope = set(), False
        for b in sec["busquedas"]:
            try:
                crudas = parsear_rss(descargar(b))
            except Exception as e:
                print(f"[error] {b}: {e}")
                continue
            if len(crudas) >= TOPE_GOOGLE:
                tope = True
            for n in crudas:
                if not n["fecha"] or n["fecha"] < limite or not n["link"]:
                    continue
                links_sec.add(n["link"])
                nota = por_link.setdefault(n["link"], {**n, "secciones": []})
                if sec["id"] not in nota["secciones"]:
                    nota["secciones"].append(sec["id"])
        conteos[sec["id"]] = {"nombre": sec["nombre"], "notas": len(links_sec), "tope": tope}
        marca_tope = " (tope)" if tope else ""
        print(f"{sec['nombre']}: {len(links_sec)} notas{marca_tope}")

    # Cada nota se asigna a una sola sección, por prioridad
    prioridad = [s["id"] for s in secciones_config(cfg)]
    notas = list(por_link.values())
    for n in notas:
        n["seccion"] = min(n["secciones"], key=prioridad.index)

    return {"conteos": conteos, "notas": quitar_duplicados(notas)}


def quitar_duplicados(notas: list[dict]) -> list[dict]:
    """Junta titulares casi idénticos de la MISMA sección; anota en 'tambien_en'
    los otros medios que la publicaron."""
    unicas = []
    for nota in sorted(notas, key=lambda n: n["fecha"]):
        clave = normalizar(nota["titulo"])
        for u in unicas:
            if u["seccion"] == nota["seccion"] and \
               SequenceMatcher(None, clave, u["_clave"]).ratio() >= UMBRAL_DUPLICADO:
                if nota["fuente"] and nota["fuente"] not in u["tambien_en"] + [u["fuente"]]:
                    u["tambien_en"].append(nota["fuente"])
                break
        else:
            unicas.append({**nota, "_clave": clave, "tambien_en": []})
    for u in unicas:
        del u["_clave"]
    return unicas


def guardar(datos: dict) -> Path:
    CARPETA_SALIDA.mkdir(exist_ok=True)
    ruta = CARPETA_SALIDA / f"notas_{datetime.now():%Y-%m-%d_%H%M}.json"
    salida = {
        "fecha": datetime.now().strftime("%Y-%m-%d"),
        "conteos": datos["conteos"],
        "notas": [{**n, "fecha": n["fecha"].isoformat()} for n in datos["notas"]],
    }
    ruta.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    return ruta


if __name__ == "__main__":
    datos = recolectar(cargar_config())
    print(f"\nNotas únicas: {len(datos['notas'])}")
    print(f"Guardado en: {guardar(datos)}")
