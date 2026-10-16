"""
Monitoreo beta - Etapa 2: recolector de Google News
Trae notas del RSS de Google News, filtra por ventana de tiempo,
quita duplicados y guarda el resultado en JSON.

Uso:
    python recolector_gnews.py
Sin dependencias externas (solo librería estándar).
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

# ---------- CONFIGURACIÓN (ficha de la beta) ----------
BUSQUEDAS = ['"Clara Brugada"', '"Gobierno de la CDMX"']
EXCLUIR = []                 # palabras que descartan una nota (se llena después)
VENTANA_HORAS = 24           # lo publicado desde el corte anterior
UMBRAL_DUPLICADO = 0.85      # 0-1: qué tan parecidos deben ser dos títulos
CARPETA_SALIDA = Path("salida")
# -------------------------------------------------------

RSS_BASE = "https://news.google.com/rss/search?q={q}&hl=es-419&gl=MX&ceid=MX:es-419"


def construir_url(busqueda: str) -> str:
    # when:1d le pide a Google solo lo del último día
    return RSS_BASE.format(q=urllib.parse.quote(f"{busqueda} when:1d"))


def descargar(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def parsear_rss(xml_bytes: bytes, busqueda: str) -> list[dict]:
    notas = []
    raiz = ET.fromstring(xml_bytes)
    for item in raiz.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        fuente_el = item.find("source")
        fuente = fuente_el.text.strip() if fuente_el is not None and fuente_el.text else ""
        # Google agrega " - Fuente" al final del título; se lo quitamos
        if fuente and titulo.endswith(f" - {fuente}"):
            titulo = titulo[: -len(f" - {fuente}")]
        try:
            fecha = parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            fecha = None
        notas.append({
            "titulo": titulo,
            "fuente": fuente,
            "fecha": fecha,
            "link": (item.findtext("link") or "").strip(),
            "busqueda": busqueda,
        })
    return notas


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", texto).strip()


def filtrar_ventana(notas: list[dict], horas: int) -> list[dict]:
    limite = datetime.now(timezone.utc) - timedelta(hours=horas)
    return [n for n in notas if n["fecha"] and n["fecha"] >= limite]


def filtrar_excluidas(notas: list[dict], excluir: list[str]) -> list[dict]:
    if not excluir:
        return notas
    pal = [normalizar(p) for p in excluir]
    return [n for n in notas if not any(p in normalizar(n["titulo"]) for p in pal)]


def quitar_duplicados(notas: list[dict], umbral: float) -> list[dict]:
    """Agrupa notas con títulos casi iguales. Conserva la más antigua
    y anota en 'tambien_en' los otros medios que la publicaron."""
    unicas = []
    for nota in sorted(notas, key=lambda n: n["fecha"]):
        clave = normalizar(nota["titulo"])
        for u in unicas:
            if SequenceMatcher(None, clave, u["_clave"]).ratio() >= umbral:
                if nota["fuente"] and nota["fuente"] not in u["tambien_en"] + [u["fuente"]]:
                    u["tambien_en"].append(nota["fuente"])
                break
        else:
            nota["_clave"] = clave
            nota["tambien_en"] = []
            unicas.append(nota)
    for u in unicas:
        del u["_clave"]
    return unicas


def recolectar() -> list[dict]:
    todas = []
    for b in BUSQUEDAS:
        try:
            todas += parsear_rss(descargar(construir_url(b)), b)
        except Exception as e:  # una búsqueda caída no tumba las demás
            print(f"[error] {b}: {e}")
    total = len(todas)
    todas = filtrar_ventana(todas, VENTANA_HORAS)
    en_ventana = len(todas)
    todas = filtrar_excluidas(todas, EXCLUIR)
    todas = quitar_duplicados(todas, UMBRAL_DUPLICADO)
    print(f"Descargadas: {total} | En ventana: {en_ventana} | Únicas: {len(todas)}")
    return todas


def guardar(notas: list[dict]) -> Path:
    CARPETA_SALIDA.mkdir(exist_ok=True)
    ruta = CARPETA_SALIDA / f"notas_{datetime.now():%Y-%m-%d_%H%M}.json"
    salida = [{**n, "fecha": n["fecha"].isoformat()} for n in notas]
    ruta.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    return ruta


if __name__ == "__main__":
    notas = recolectar()
    for n in notas[:15]:
        extra = f" (+{len(n['tambien_en'])} medios)" if n["tambien_en"] else ""
        print(f"- [{n['fuente']}] {n['titulo']}{extra}")
    print(f"\nGuardado en: {guardar(notas)}")
