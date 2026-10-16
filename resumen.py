"""
Monitoreo beta - Etapa 3: resumen con Gemini
Toma el JSON más reciente de salida/ (generado por recolector_gnews.py),
le pide a Gemini que agrupe las notas por tema y arma el corte.

Reparto de trabajo:
  - Gemini: agrupa notas del mismo evento, redacta y detecta alertas.
  - El código: cuenta medios, ordena temas y pone links (para que esos
    datos no dependan del LLM).

Uso:
    export GEMINI_API_KEY="tu_clave"
    export TELEGRAM_BOT_TOKEN="token_del_bot"   # opcional: sin esto solo imprime
    export TELEGRAM_CHAT_ID="tu_chat_id"        # opcional
    python3 resumen.py              # llama a Gemini, arma el corte y lo envía
    python3 resumen.py --reenviar   # reusa la última respuesta de Gemini (no gasta cuota)
"""

import html
import json
import sys
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------- CONFIGURACIÓN ----------
PLAZA = "CDMX"
ACTOR = "Clara Brugada"
HORA_CORTE = "16:00"
MAX_TEMAS = 5
MAX_ALERTAS = 3
MODELOS = ["gemini-flash-latest", "gemini-flash-lite-latest"]  # en orden; el 2o es respaldo
REINTENTOS = 3                   # intentos por modelo si Google está saturado
ESPERA_INICIAL = 10              # segundos; se duplica en cada reintento
CARPETA = Path("salida")
# -----------------------------------

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

PROMPT = """Eres analista de monitoreo de medios para un cliente político en {plaza}.
Actor principal: {actor}.

Abajo hay una lista de titulares numerados con su medio. Solo tienes los
titulares, no el texto de las notas.

Tareas:
1. Agrupa en un mismo tema los titulares que hablan del MISMO evento o
   anuncio, aunque estén redactados distinto.
2. Descarta lo que no tenga relación con {actor} ni con el gobierno o la
   política de {plaza}.
3. Para cada tema escribe un título corto y un resumen de 1-2 líneas.
4. Actividad pública de {actor}: una sola línea de máximo 15 palabras,
   solo enumerando actividades, sin explicarlas (ej. "Segundo Informe;
   inauguró X"). Si no hubo, "hubo": false.
5. Alertas: críticas, señalamientos o temas que puedan afectar
   negativamente a {actor}. Cada alerta es UN solo asunto; nunca juntes
   en una misma alerta titulares de asuntos distintos. Si no hay, deja
   la lista vacía.

Reglas estrictas:
- Usa SOLO lo que dicen los titulares. No agregues cifras, nombres ni
  datos que no aparezcan en ellos.
- Tono neutral, sin adjetivos valorativos.
- Español de México.
- En "ids" pon los números de TODOS los titulares que usaste.

Responde solo con JSON con esta forma:
{{"temas": [{{"titulo": "", "resumen": "", "ids": [0]}}],
 "actividad_actor": {{"hubo": true, "detalle": ""}},
 "alertas": [{{"texto": "", "ids": [0]}}]}}

TITULARES:
{titulares}
"""


def cargar_notas() -> tuple[list[dict], Path]:
    archivos = sorted(CARPETA.glob("notas_*.json"))
    if not archivos:
        raise SystemExit("No hay notas en salida/. Corre primero recolector_gnews.py")
    ruta = archivos[-1]
    return json.loads(ruta.read_text(encoding="utf-8")), ruta


def preguntar_gemini(prompt: str) -> dict:
    clave = os.environ.get("GEMINI_API_KEY")
    if not clave:
        raise SystemExit('Falta la clave. Corre: export GEMINI_API_KEY="tu_clave"')
    cuerpo = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    datos_envio = json.dumps(cuerpo).encode()
    ultimo_error = ""
    for modelo in MODELOS:
        espera = ESPERA_INICIAL
        for intento in range(1, REINTENTOS + 1):
            req = urllib.request.Request(
                API_URL.format(m=modelo),
                data=datos_envio,
                headers={"Content-Type": "application/json", "x-goog-api-key": clave},
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    respuesta = json.loads(r.read())
                print(f"Modelo usado: {modelo}\n")
                texto = respuesta["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(texto)
            except urllib.error.HTTPError as e:
                ultimo_error = f"Error {e.code} de Gemini ({modelo}):\n{e.read().decode(errors='replace')}"
                if e.code in (429, 500, 503) and intento < REINTENTOS:
                    print(f"[{modelo}] saturado (error {e.code}), reintento {intento}/{REINTENTOS - 1} en {espera}s...")
                    time.sleep(espera)
                    espera *= 2
                    continue
                if e.code in (404, 429, 500, 503):
                    print(f"[{modelo}] no disponible, probando el siguiente modelo...")
                    break   # pasa al modelo de respaldo
                raise SystemExit(ultimo_error)  # 400/401/403: no tiene caso reintentar
    raise SystemExit(f"Ningún modelo respondió. Último error:\n{ultimo_error}")


def medios_de(ids: list[int], notas: list[dict]) -> set[str]:
    medios = set()
    for i in ids:
        n = notas[i]
        medios.add(n["fuente"])
        medios.update(n.get("tambien_en", []))
    medios.discard("")
    return medios


def preparar(items: list[dict], notas: list[dict]) -> list[dict]:
    """Limpia ids inválidos, cuenta medios y ordena de mayor a menor cobertura."""
    listos = []
    for it in items:
        ids = [i for i in it.get("ids", []) if isinstance(i, int) and 0 <= i < len(notas)]
        if ids:
            listos.append({**it, "ids": ids, "n_medios": len(medios_de(ids, notas))})
    return sorted(listos, key=lambda x: x["n_medios"], reverse=True)


def conteo(n: int) -> str:
    return f"{n} {'medio' if n == 1 else 'medios'}"


def armar_corte(datos: dict, notas: list[dict], para_telegram: bool = False) -> str:
    """para_telegram=True: HTML con negritas y links ocultos en 'Ver nota'.
    para_telegram=False: texto plano con links completos (para el archivo)."""
    e = html.escape if para_telegram else (lambda x: x)
    b = (lambda x: f"<b>{x}</b>") if para_telegram else (lambda x: x)

    def link(i: int) -> str:
        url = notas[i]["link"]
        return f'<a href="{html.escape(url)}">Ver nota</a>' if para_telegram else url

    temas = preparar(datos.get("temas", []), notas)[:MAX_TEMAS]
    alertas = preparar(datos.get("alertas", []), notas)[:MAX_ALERTAS]

    fecha = datetime.now().strftime("%d/%m")
    L = [b(f"📍 MONITOREO {e(PLAZA)} · {fecha} · corte {HORA_CORTE}"), "", b("🔝 TEMAS DEL DÍA")]
    for n, t in enumerate(temas, 1):
        L.append(f"{n}. {b(e(t['titulo']))} ({conteo(t['n_medios'])})")
        L.append(e(t['resumen']))
        L.append(link(t['ids'][0]))
        L.append("")

    act = datos.get("actividad_actor", {})
    detalle = act.get("detalle") if act.get("hubo") else "Sin actividad pública."
    L += [b(f"👤 {e(ACTOR.upper())}"), e(detalle), "", b("⚠️ PARA ESTAR ATENTOS")]
    if alertas:
        for a in alertas:
            L.append(f"• {e(a['texto'])} ({conteo(a['n_medios'])}) {link(a['ids'][0])}")
    else:
        L.append("Sin alertas.")
    return "\n".join(L)


def enviar_telegram(texto_html: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("\n(Telegram no configurado: solo se imprimió el corte)")
        return
    cuerpo = {
        "chat_id": chat,
        "text": texto_html,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(cuerpo).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
        print("\n✅ Enviado a Telegram")
    except urllib.error.HTTPError as err:
        print(f"\n❌ Telegram respondió {err.code}: {err.read().decode(errors='replace')}")


if __name__ == "__main__":
    notas, ruta = cargar_notas()
    print(f"Usando {ruta} ({len(notas)} notas)\n")
    ruta_resp = CARPETA / f"gemini_{ruta.stem}.json"

    if "--reenviar" in sys.argv:
        if not ruta_resp.exists():
            raise SystemExit(f"No existe {ruta_resp}. Corre primero sin --reenviar.")
        datos = json.loads(ruta_resp.read_text(encoding="utf-8"))
        print("Reusando respuesta guardada de Gemini\n")
    else:
        titulares = "\n".join(f"[{i}] ({n['fuente']}) {n['titulo']}" for i, n in enumerate(notas))
        datos = preguntar_gemini(PROMPT.format(plaza=PLAZA, actor=ACTOR, titulares=titulares))
        ruta_resp.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")

    corte = armar_corte(datos, notas)
    print(corte)
    salida = CARPETA / f"corte_{datetime.now():%Y-%m-%d_%H%M}.txt"
    salida.write_text(corte, encoding="utf-8")
    print(f"\nGuardado en: {salida}")
    enviar_telegram(armar_corte(datos, notas, para_telegram=True))
