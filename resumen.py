"""
Monitoreo - Resumen v2 (Marca / Adversarios / Ciudad / Alertas)

1. Toma el JSON más reciente de salida/ (de recolector_gnews.py).
2. Guarda los conteos del día en historial.json y calcula los íconos:
     🔥 = 2x o más su promedio   🆕 = reaparece tras 5+ días sin menciones
3. Gemini agrupa titulares en temas por sección y detecta alertas.
   El código cuenta medios, ordena y pone links (no dependen del LLM).
4. Envía el corte a Telegram.

Uso:
    export GEMINI_API_KEY="..."  TELEGRAM_BOT_TOKEN="..."  TELEGRAM_CHAT_ID="..."
    python3 resumen.py              # corte completo
    python3 resumen.py --reenviar   # reusa la última respuesta de Gemini
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CARPETA = Path("salida")
HISTORIAL = Path("historial.json")
MODELOS = ["gemini-flash-latest", "gemini-flash-lite-latest"]
REINTENTOS = 3
ESPERA_INICIAL = 10
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

CFG = json.loads(Path("config.json").read_text(encoding="utf-8"))
MARCA, ADVS, CIUDAD = CFG["marca"], CFG["adversarios"], CFG["ciudad"]
LIM, UMB = CFG["limites"], CFG["umbrales"]
SECCIONES = [MARCA] + ADVS + [CIUDAD]

PROMPT = """Eres analista de monitoreo de medios para un cliente político en {plaza}.
Marca (el cliente): {marca}.

Abajo hay titulares numerados, separados por SECCIÓN. Solo tienes los
titulares, no el texto de las notas.

Tareas:
1. En cada sección, agrupa en un mismo tema los titulares que hablan del
   MISMO evento o anuncio, aunque estén redactados distinto.
   - Cada titular va en UN SOLO tema.
   - Usa solo titulares de esa sección para sus temas.
   - Prefiere temas específicos sobre generales: si en un evento se
     anunciaron varias cosas, cada anuncio es su propio tema.
2. Descarta titulares que no correspondan a su sección:
   - En secciones de personas: si el titular no trata realmente de esa
     persona (homónimos, menciones de paso), descártalo.
   - En la sección ciudad: quédate solo con asuntos de interés público
     (seguridad, servicios, agua, transporte, gobierno, protestas,
     alcaldías). Descarta deportes, espectáculos, conciertos, farándula,
     clima rutinario y publicidad.
3. Para cada tema escribe un título corto (máximo 12 palabras).
4. Alertas: críticas o señalamientos que puedan afectar negativamente a
   {marca}, tomados de cualquier sección. Cada alerta es UN solo asunto.
   Si no hay, deja la lista vacía.

Reglas estrictas:
- Usa SOLO lo que dicen los titulares. No agregues cifras, fechas,
  nombres ni datos que no aparezcan en ellos.
- Tono neutral, sin adjetivos valorativos. Español de México.
- En "ids" pon los números de TODOS los titulares que usaste.

Responde solo con JSON con esta forma (las llaves de "temas" son los
identificadores de sección que aparecen entre corchetes):
{{"temas": {{"marca": [{{"titulo": "", "ids": [0]}}], "otra_seccion": []}},
 "alertas": [{{"texto": "", "ids": [0]}}]}}

{titulares}
"""


# ---------------- Datos ----------------

def cargar_notas() -> tuple[dict, Path]:
    archivos = sorted(CARPETA.glob("notas_*.json"))
    if not archivos:
        raise SystemExit("No hay notas en salida/. Corre primero recolector_gnews.py")
    ruta = archivos[-1]
    return json.loads(ruta.read_text(encoding="utf-8")), ruta


def actualizar_historial(fecha: str, conteos: dict) -> dict:
    hist = json.loads(HISTORIAL.read_text(encoding="utf-8")) if HISTORIAL.exists() else {}
    hist[fecha] = {k: v["notas"] for k, v in conteos.items()}
    HISTORIAL.write_text(json.dumps(dict(sorted(hist.items())), indent=2), encoding="utf-8")
    return hist


def n_notas(n: int) -> str:
    return f"{n} {'nota' if n == 1 else 'notas'}"


def estado(hist: dict, fecha: str, sec_id: str, conteo: dict) -> tuple[str, str]:
    """Devuelve (ícono, texto) para la línea de una persona o de la ciudad."""
    notas, tope = conteo["notas"], conteo["tope"]
    if tope:
        return "", f"{notas}+ notas (tope)"
    previos = sorted(d for d in hist if d < fecha)
    if len(previos) < UMB["dias_minimos_historial"]:
        return "", f"{n_notas(notas)} (sin historial)"

    racha = 0
    for d in reversed(previos):
        if hist[d].get(sec_id, 0) == 0:
            racha += 1
        else:
            break
    if notas > 0 and racha >= UMB["dias_sin_menciones_nuevo"]:
        return "🆕", f"{n_notas(notas)} (sin menciones en {racha} días)"

    ultimos = previos[-UMB["dias_promedio"]:]
    promedio = sum(hist[d].get(sec_id, 0) for d in ultimos) / len(ultimos)
    if promedio > 0 and notas >= UMB["notas_minimas_fuego"] \
       and notas / promedio >= UMB["veces_promedio_fuego"]:
        return "🔥", f"{n_notas(notas)} (≈{round(notas / promedio)}x su promedio)"
    return "", f"{n_notas(notas)} (normal)"


# ---------------- Gemini ----------------

def armar_titulares(notas: list[dict]) -> str:
    bloques = []
    for sec in SECCIONES:
        lineas = [f"[{i}] ({n['fuente']}) {n['titulo']}"
                  for i, n in enumerate(notas) if n["seccion"] == sec["id"]]
        if lineas:
            bloques.append(f"=== SECCIÓN [{sec['id']}] {sec['nombre']} ===\n" + "\n".join(lineas))
    return "\n\n".join(bloques)


def preguntar_gemini(prompt: str) -> dict:
    clave = os.environ.get("GEMINI_API_KEY")
    if not clave:
        raise SystemExit('Falta la clave. Corre: export GEMINI_API_KEY="tu_clave"')
    cuerpo = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }).encode()
    ultimo_error = ""
    for modelo in MODELOS:
        espera = ESPERA_INICIAL
        for intento in range(1, REINTENTOS + 1):
            req = urllib.request.Request(
                API_URL.format(m=modelo), data=cuerpo,
                headers={"Content-Type": "application/json", "x-goog-api-key": clave})
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    respuesta = json.loads(r.read())
                print(f"Modelo usado: {modelo}\n")
                return json.loads(respuesta["candidates"][0]["content"]["parts"][0]["text"])
            except urllib.error.HTTPError as e:
                ultimo_error = f"Error {e.code} de Gemini ({modelo}):\n{e.read().decode(errors='replace')}"
                if e.code in (429, 500, 503) and intento < REINTENTOS:
                    print(f"[{modelo}] saturado (error {e.code}), reintento {intento}/{REINTENTOS - 1} en {espera}s...")
                    time.sleep(espera); espera *= 2; continue
                if e.code in (404, 429, 500, 503):
                    print(f"[{modelo}] no disponible, probando el siguiente modelo...")
                    break
                raise SystemExit(ultimo_error)
            except (TimeoutError, urllib.error.URLError) as e:
                ultimo_error = f"Sin respuesta de Gemini ({modelo}): {e}"
                if intento < REINTENTOS:
                    print(f"[{modelo}] sin respuesta, reintento {intento}/{REINTENTOS - 1} en {espera}s...")
                    time.sleep(espera); espera *= 2; continue
                print(f"[{modelo}] no disponible, probando el siguiente modelo...")
                break
    raise SystemExit(f"Ningún modelo respondió. Último error:\n{ultimo_error}")


# ---------------- Armado ----------------

def medios_de(ids: list[int], notas: list[dict]) -> set[str]:
    medios = set()
    for i in ids:
        medios.add(notas[i]["fuente"])
        medios.update(notas[i].get("tambien_en", []))
    medios.discard("")
    return medios


def preparar(items: list[dict], notas: list[dict], seccion: str | None = None) -> list[dict]:
    """Limpia ids, cuenta medios y ordena por cobertura.
    Con 'seccion': solo acepta notas de esa sección y cada nota cuenta en
    un solo tema (el más específico, el de menos notas)."""
    listos, usados = [], set()
    if seccion:
        items = sorted(items, key=lambda it: len(it.get("ids", [])))
    for it in items:
        ids = [i for i in dict.fromkeys(it.get("ids", []))
               if isinstance(i, int) and 0 <= i < len(notas)]
        if seccion:
            ids = [i for i in ids if notas[i]["seccion"] == seccion and i not in usados]
            usados.update(ids)
        if ids:
            listos.append({**it, "ids": ids, "n_medios": len(medios_de(ids, notas))})
    return sorted(listos, key=lambda x: x["n_medios"], reverse=True)


def armar_corte(datos: dict, base: dict, hist: dict, tg: bool = False) -> str:
    notas, conteos, fecha = base["notas"], base["conteos"], base["fecha"]
    e = html.escape if tg else (lambda x: x)
    b = (lambda x: f"<b>{x}</b>") if tg else (lambda x: x)

    def link(i: int) -> str:
        url = notas[i]["link"]
        return f'<a href="{html.escape(url)}">Ver nota</a>' if tg else url

    def medios(n: int) -> str:
        return f"{n} {'medio' if n == 1 else 'medios'}"

    def lista_temas(sec_id: str, limite: int) -> list[str]:
        temas = preparar(datos.get("temas", {}).get(sec_id, []), notas, seccion=sec_id)[:limite]
        return [f"{k}. {e(t['titulo'])} ({medios(t['n_medios'])}) · {link(t['ids'][0])}"
                for k, t in enumerate(temas, 1)]

    def cabecera(sec: dict, con_nombre: bool = True) -> str:
        icono, texto = estado(hist, fecha, sec["id"], conteos[sec["id"]])
        nombre = f"{e(sec['nombre'])} · " if con_nombre else ""
        return f"{icono + ' ' if icono else ''}{nombre}{texto}"

    dia = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m")
    L = [b(f"📍 MONITOREO {e(CFG['plaza'])} · {dia} · corte {CFG['hora_corte']}"), ""]

    L += [b("🎯 MARCA"), cabecera(MARCA)] + lista_temas(MARCA["id"], LIM["temas_marca"]) + [""]

    L.append(b("⚔️ ADVERSARIOS"))
    con_notas = [a for a in ADVS if conteos[a["id"]]["notas"] > 0]
    orden_icono = {"🔥": 0, "🆕": 1, "": 2}
    con_notas.sort(key=lambda a: (orden_icono[estado(hist, fecha, a["id"], conteos[a["id"]])[0]],
                                  -conteos[a["id"]]["notas"]))
    for a in con_notas:
        L.append(cabecera(a))
        L += lista_temas(a["id"], LIM["temas_adversario"])
    sin = len(ADVS) - len(con_notas)
    if sin:
        L.append(f"Sin menciones: {sin} de {len(ADVS)}")
    L.append("")

    L += [b(f"🏙 {e(CIUDAD['nombre'])}"), cabecera(CIUDAD, con_nombre=False)]
    L += lista_temas(CIUDAD["id"], LIM["temas_ciudad"]) + [""]

    L.append(b("⚠️ ALERTAS"))
    alertas = preparar(datos.get("alertas", []), notas)[:LIM["alertas"]]
    if alertas:
        L += [f"• {e(a['texto'])} ({medios(a['n_medios'])}) · {link(a['ids'][0])}" for a in alertas]
    else:
        L.append("Sin alertas.")
    return "\n".join(L)


# ---------------- Telegram ----------------

def enviar_telegram(texto: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("\n(Telegram no configurado: solo se imprimió el corte)")
        return
    cuerpo = {"chat_id": chat, "text": texto, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}}
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=json.dumps(cuerpo).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
        print("\n✅ Enviado a Telegram")
    except urllib.error.HTTPError as err:
        print(f"\n❌ Telegram respondió {err.code}: {err.read().decode(errors='replace')}")


# ---------------- Principal ----------------

if __name__ == "__main__":
    base, ruta = cargar_notas()
    print(f"Usando {ruta} ({len(base['notas'])} notas)\n")
    hist = actualizar_historial(base["fecha"], base["conteos"])
    ruta_resp = CARPETA / f"gemini_{ruta.stem}.json"

    if "--reenviar" in sys.argv:
        if not ruta_resp.exists():
            raise SystemExit(f"No existe {ruta_resp}. Corre primero sin --reenviar.")
        datos = json.loads(ruta_resp.read_text(encoding="utf-8"))
    else:
        prompt = PROMPT.format(plaza=CFG["plaza"], marca=MARCA["nombre"],
                               titulares=armar_titulares(base["notas"]))
        try:
            datos = preguntar_gemini(prompt)
        except SystemExit as fallo:
            enviar_telegram(f"⚠️ El corte de {CFG['plaza']} de hoy no se generó: Gemini no respondió. "
                            f"Se recolectaron {len(base['notas'])} notas. Revisa GitHub Actions.")
            raise fallo
        ruta_resp.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")

    corte = armar_corte(datos, base, hist)
    print(corte)
    (CARPETA / f"corte_{datetime.now():%Y-%m-%d_%H%M}.txt").write_text(corte, encoding="utf-8")
    enviar_telegram(armar_corte(datos, base, hist, tg=True))
