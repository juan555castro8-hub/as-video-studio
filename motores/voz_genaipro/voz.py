"""Voz con GenAI Pro (Labs), un revendedor de ElevenLabs.

La ficha oficial (https://docs.genaipro.io/openapi.yaml, enlazada desde
https://genaipro.io/docs-api) describe POST /v1/labs/task, el sondeo
GET /v1/labs/task/{id} y el audio en `result`. No hay marcas de palabra ni
alineado: el subtítulo, si se pide, es la URL de un VTT. El montaje necesita
{"w","s","e"}, así que cada trozo se alinea en local con el texto que se le
mandó. Si el alineador no está, se reparte ese VTT (o un SRT) entre las
palabras del cue: eso se marca como aproximado y se avisa.

`toma(texto, cfg, progreso)` devuelve (wav_bytes, duracion, palabras) con
palabras `{"w","s","e"}` en segundos, igual que Cartesia.
"""
import hashlib
import importlib.util
import os
import re
import tempfile
import time

import requests

# El OpenAPI publica el servidor https://genaipro.io/api y las rutas /v1/labs/…
# GENAIPRO_BASE ya incluye /api/v1, que es lo que se concatena con /labs/…
API_BASE = os.environ.get("GENAIPRO_BASE") or "https://genaipro.io/api/v1"
MODELO = os.environ.get("GENAIPRO_MODELO") or "eleven_multilingual_v2"
MODELOS = ("eleven_multilingual_v2", "eleven_turbo_v2_5",
           "eleven_flash_v2_5", "eleven_v3")
# Defectos del POST /v1/labs/task en el OpenAPI, no los del cliente antiguo.
ESTABILIDAD = 0.75
SIMILITUD = 0.5
TOPE_TROZO = int(os.environ.get("GENAIPRO_MAX_CHARS") or 4000)
POLL_S = 3.0
MAX_ESPERA_S = 600.0
AIRE_S = 0.04

_AUDIO = None
_ALINEADOR = None


def _simulado():
    valor = str(os.environ.get("ESTUDIO_SIMULAR", "")).strip().lower()
    return valor not in ("", "0", "false", "no")


def _cargar(relativo, nombre):
    ruta = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", relativo))
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def audio():
    global _AUDIO
    if _AUDIO is None:
        _AUDIO = _cargar(os.path.join("voz_comun", "audio.py"), "voz_comun_audio")
    return _AUDIO


def alineador():
    global _ALINEADOR
    if _ALINEADOR is None:
        _ALINEADOR = _cargar(
            os.path.join("alineador", "alinear.py"), "alineador_alinear")
    return _ALINEADOR


def _carpeta_secretos():
    return os.environ.get("ESTUDIO_SECRETOS") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "secretos")


def cargar_api_key():
    entorno = (os.environ.get("GENAIPRO_API_KEY") or "").strip()
    if entorno:
        return entorno
    ruta = os.path.join(_carpeta_secretos(), "claves.json")
    try:
        with open(ruta, "r", encoding="utf-8-sig") as fh:
            import json
            datos = json.load(fh)
    except (OSError, ValueError):
        datos = {}
    clave = ""
    cruda = datos.get("genaipro")
    if isinstance(cruda, dict):
        clave = str(cruda.get("clave") or "").strip()
    elif isinstance(cruda, str):
        clave = cruda.strip()
    if not clave:
        raise SystemExit("Falta GENAIPRO_API_KEY")
    return clave


def _velocidad(cfg):
    """La velocidad del estudio (slow..fast o -1..1) a 0,7..1,2."""
    tabla = {"slowest": 0.7, "slow": 0.85, "normal": 1.0, "fast": 1.1,
             "fastest": 1.2}
    cruda = (cfg or {}).get("velocidad")
    if isinstance(cruda, str) and cruda.strip().lower() in tabla:
        return tabla[cruda.strip().lower()]
    try:
        numero = float(cruda)
    except (TypeError, ValueError):
        return 1.0
    numero = max(-1.0, min(1.0, numero))
    return round(0.7 + (numero + 1.0) * 0.25, 3)


def _modelo(cfg):
    pedido = str((cfg or {}).get("modelo") or "").strip()
    if pedido in MODELOS:
        return pedido
    return MODELO


def _avisar_estilo(cfg, progreso):
    if not (cfg or {}).get("emociones"):
        return
    progreso(0.02, "GenAI Pro no locuta las emociones de Cartesia: se usa la "
                   "velocidad y se deja el color")


def trocear(texto, tope=None):
    """Corta por frase, sin pasar de `tope` caracteres. No parte una palabra."""
    tope = int(tope or TOPE_TROZO)
    texto = " ".join(str(texto or "").split())
    if not texto:
        return []
    if len(texto) <= tope:
        return [texto]
    frases = re.split(r"(?<=[\.\!\?\u2026])\s+", texto)
    trozos, actual = [], ""
    for frase in frases:
        if len(frase) > tope:
            if actual:
                trozos.append(actual)
                actual = ""
            while len(frase) > tope:
                corte = frase.rfind(" ", 0, tope)
                if corte < tope // 2:
                    corte = tope
                trozos.append(frase[:corte].strip())
                frase = frase[corte:].strip()
            actual = frase
            continue
        candidato = (actual + " " + frase).strip() if actual else frase
        if len(candidato) > tope and actual:
            trozos.append(actual)
            actual = frase
        else:
            actual = candidato
    if actual:
        trozos.append(actual)
    return [t for t in trozos if t]


def _sesion(clave):
    sesion = requests.Session()
    sesion.headers.update({
        "Authorization": f"Bearer {clave}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return sesion


def _esperar_tarea(sesion, tarea):
    limite = time.monotonic() + MAX_ESPERA_S
    while time.monotonic() < limite:
        respuesta = sesion.get(f"{API_BASE}/labs/task/{tarea}", timeout=60)
        if respuesta.status_code == 401:
            raise RuntimeError("GenAI Pro rechaza la clave (401)")
        if respuesta.status_code != 200:
            time.sleep(POLL_S)
            continue
        cuerpo = respuesta.json()
        estado = str(cuerpo.get("status") or "").lower()
        if estado in ("completed", "done", "success"):
            return cuerpo
        if estado in ("failed", "error"):
            raise RuntimeError(
                "GenAI Pro no ha podido locutar: "
                + str(cuerpo.get("error") or cuerpo.get("message") or estado))
        time.sleep(POLL_S)
    raise RuntimeError("GenAI Pro no ha terminado la toma a tiempo")


def _bajar(sesion, url):
    """El MP3 y el VTT viven en un CDN. Si el bearer estorba, se repite sin el."""
    respuesta = sesion.get(url, timeout=120)
    if respuesta.status_code == 200 and respuesta.content:
        return respuesta
    return requests.get(url, timeout=120)


def _texto_subtitulo(respuesta):
    """El POST puede devolver el VTT, su URL, o nada y dejarlo en la tarea."""
    if respuesta is None or respuesta.status_code not in (200, 201):
        return ""
    texto = (respuesta.text or "").lstrip()
    if texto.startswith("WEBVTT") or "-->" in texto[:400]:
        return respuesta.text
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return ""
    if not isinstance(cuerpo, dict):
        return ""
    sub = cuerpo.get("subtitle") or ""
    if isinstance(sub, str) and sub.strip() and not sub.startswith("http"):
        return sub
    return ""


def _subtitulo(sesion, tarea):
    """Pide el subtítulo. En la ficha oficial `subtitle` es la URL de un VTT."""
    try:
        respuesta = sesion.post(
            f"{API_BASE}/labs/task/subtitle/{tarea}",
            json={"max_characters_per_line": 42, "max_lines_per_cue": 1,
                  "max_seconds_per_cue": 2},
            timeout=60)
    except requests.RequestException:
        respuesta = None
    directo = _texto_subtitulo(respuesta)
    if directo:
        return directo
    limite = time.monotonic() + MAX_ESPERA_S
    while time.monotonic() < limite:
        try:
            respuesta = sesion.get(f"{API_BASE}/labs/task/{tarea}", timeout=60)
        except requests.RequestException:
            time.sleep(POLL_S)
            continue
        if respuesta.status_code != 200:
            time.sleep(POLL_S)
            continue
        try:
            cuerpo = respuesta.json()
        except ValueError:
            time.sleep(POLL_S)
            continue
        estado = str(cuerpo.get("status") or "").lower()
        if estado in ("failed", "error"):
            return ""
        sub = cuerpo.get("subtitle") or ""
        if isinstance(sub, str) and sub.startswith("http"):
            bajada = _bajar(sesion, sub)
            if bajada.status_code == 200 and bajada.text:
                return bajada.text
            return ""
        if isinstance(sub, str) and sub.strip():
            return sub
        time.sleep(POLL_S)
    return ""


# SRT (hh:mm:ss,mmm) y VTT (mm:ss.mmm o hh:mm:ss.mmm). La ficha de ejemplo es VTT.
_CUE = re.compile(
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[,.](\d{3})")


def _reloj(h, m, s, ms):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def palabras_de_srt(srt, texto):
    """Reparte el tiempo de cada cue entre las palabras del propio cue.

    Si el SRT no trae las palabras del texto, se reparte todo el texto sobre
    la suma de los cues. Las marcas quedan aproximadas.
    """
    cues = []
    bloques = re.split(r"\n\s*\n", str(srt or "").replace("\r\n", "\n").strip())
    for bloque in bloques:
        encaje = _CUE.search(bloque)
        if not encaje:
            continue
        ini = _reloj(*encaje.group(1, 2, 3, 4))
        fin = _reloj(*encaje.group(5, 6, 7, 8))
        lineas = [ln.strip() for ln in bloque.splitlines()
                  if ln.strip() and not ln.strip().isdigit() and "-->" not in ln]
        dicho = " ".join(lineas)
        if dicho and fin > ini:
            cues.append((ini, fin, dicho))
    palabras = []
    if not cues:
        return _reparto_plano(texto, 0.0, max(0.4, 0.32 * max(1, len(str(texto).split()))))
    for ini, fin, dicho in cues:
        palabras.extend(_reparto_plano(dicho, ini, fin))
    return palabras


def _reparto_plano(texto, ini, fin):
    piezas = [p for p in str(texto or "").split() if p]
    if not piezas:
        return []
    pesos = [max(1, len(p)) for p in piezas]
    total = float(sum(pesos))
    ancho = max(0.05, fin - ini)
    cursor = ini
    salida = []
    for indice, (pieza, peso) in enumerate(zip(piezas, pesos)):
        siguiente = fin if indice == len(piezas) - 1 else cursor + ancho * peso / total
        salida.append({"w": pieza, "s": round(cursor, 3),
                       "e": round(max(cursor + 0.01, siguiente), 3)})
        cursor = siguiente
    return salida


def _limpiar_dicho(texto):
    """Lo que se oye, sin etiquetas, para alinear y para el SRT."""
    sin_etiquetas = re.sub(r"<[^>]+>", " ", str(texto or ""))
    return " ".join(sin_etiquetas.split())


def _alinear_trozo(pcm, texto, idioma, avisos):
    dicho = _limpiar_dicho(texto)
    if not dicho:
        return [], "vacio"
    modulo_audio = audio()
    wav = modulo_audio.wav_desde_pcm(pcm)
    temporal = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(wav)
            temporal = fh.name
        try:
            marcas = alineador().alinear(temporal, dicho, idioma or "es")
        except alineador().AlineadorAusente as fallo:
            avisos.append(str(fallo))
            return None, "aproximadas"
        if not marcas:
            return None, "aproximadas"
        return marcas, "alineador"
    finally:
        if temporal and os.path.exists(temporal):
            os.unlink(temporal)


def _locutar_trozo(sesion, texto, cfg):
    cuerpo = {
        "input": texto,
        "voice_id": cfg["voz_id"],
        "model_id": _modelo(cfg),
        "speed": _velocidad(cfg),
        "stability": ESTABILIDAD,
        "similarity": SIMILITUD,
        "style": 0.0,
        "use_speaker_boost": True,
    }
    respuesta = sesion.post(f"{API_BASE}/labs/task", json=cuerpo, timeout=60)
    if respuesta.status_code == 401:
        raise RuntimeError("GenAI Pro rechaza la clave (401). Revisala en Configuracion.")
    if respuesta.status_code not in (200, 201):
        raise RuntimeError(
            f"GenAI Pro HTTP {respuesta.status_code}: {(respuesta.text or '')[:240]}")
    datos = respuesta.json()
    tarea = datos.get("task_id") or datos.get("id")
    if not tarea:
        raise RuntimeError("GenAI Pro no ha devuelto task_id")
    ficha = _esperar_tarea(sesion, tarea)
    url = ficha.get("result") or ""
    if not url:
        raise RuntimeError("GenAI Pro ha terminado la tarea sin MP3")
    bajada = _bajar(sesion, url)
    if bajada.status_code != 200 or not bajada.content:
        raise RuntimeError(f"no se ha podido bajar el MP3 (HTTP {bajada.status_code})")
    pcm = audio().mp3_a_pcm(bajada.content)
    return pcm, str(tarea)


def _toma_simulada(texto, cfg):
    dicho = _limpiar_dicho(texto)
    piezas = dicho.split() or ["."]
    factor = 1.0 / max(0.7, _velocidad(cfg))
    palabras = []
    cursor = 0.0
    for pieza in piezas:
        ancho = max(0.12, 0.08 * len(pieza)) * factor
        palabras.append({"w": pieza, "s": round(cursor, 3),
                         "e": round(cursor + ancho, 3)})
        cursor += ancho + 0.04
    modulo = audio()
    pcm = b"\x00" * (int(cursor * modulo.SR) * 2)
    return modulo.wav_desde_pcm(pcm), cursor, palabras


def toma(texto, cfg, progreso=None):
    """Un texto -> (wav, duracion_s, palabras). Puede ir en varios trozos."""
    def avanza(fraccion, mensaje=""):
        if callable(progreso):
            progreso(fraccion, mensaje)
        return fraccion

    cfg = dict(cfg or {})
    if not str(texto or "").strip():
        raise ValueError("no hay texto que sintetizar")
    if not cfg.get("voz_id"):
        raise RuntimeError(
            "GenAI Pro necesita una voz. Elige una en el catalogo de voces.")
    if _simulado():
        avanza(0.5, "simulando la voz de GenAI Pro")
        toma.marcas = "simuladas"
        return _toma_simulada(texto, cfg)

    _avisar_estilo(cfg, avanza)
    trozos = trocear(texto)
    if not trozos:
        raise ValueError("no hay texto que sintetizar")
    clave = cargar_api_key()
    sesion = _sesion(clave)
    avisos = []
    pcms = []
    marcas_trozo = []
    origen = "alineador"
    for indice, trozo in enumerate(trozos):
        avanza(0.05 + 0.7 * indice / len(trozos),
               f"locutando {indice + 1} de {len(trozos)}")
        pcm, tarea = _locutar_trozo(sesion, trozo, cfg)
        marcas, como = _alinear_trozo(pcm, trozo, cfg.get("idioma") or "es", avisos)
        if marcas is None:
            origen = "aproximadas"
            srt = _subtitulo(sesion, tarea)
            marcas = palabras_de_srt(srt, _limpiar_dicho(trozo))
            if avisos:
                avanza(0.1, avisos[-1])
        pcms.append(pcm)
        marcas_trozo.append(marcas)
    modulo = audio()
    unido, offsets = modulo.unir(pcms, AIRE_S if len(pcms) > 1 else 0.0)
    palabras = []
    for marcas, origen_s in zip(marcas_trozo, offsets):
        for marca in marcas:
            palabras.append({
                "w": marca["w"],
                "s": round(marca["s"] + origen_s, 3),
                "e": round(marca["e"] + origen_s, 3),
            })
    wav = modulo.wav_desde_pcm(unido)
    duracion = modulo.duracion_pcm(unido)
    if origen == "aproximadas":
        avanza(0.95, "las marcas de palabra son aproximadas: falta el alineador")
    toma.marcas = origen
    return wav, duracion, palabras


toma.marcas = ""


def _ficha(cruda):
    return {
        "id": cruda.get("voice_id") or cruda.get("id"),
        "nombre": cruda.get("name") or cruda.get("voice_id") or "",
        "descripcion": (cruda.get("description") or "").strip(),
        "idioma": cruda.get("language") or "",
        "genero": cruda.get("gender") or "",
        "pais": "",
        "locales": [cruda.get("locale")] if cruda.get("locale") else [],
        "locales_nativos": [cruda.get("locale")] if cruda.get("locale") else [],
        "pro": False,
        "publica": True,
        "proveedor": "genaipro",
    }


def listar_voces_genaipro(idioma=None, refrescar=False):
    """Voces de /labs/voices, cacheadas como voces_genaipro.json."""
    raiz = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cache_ruta = os.path.join(raiz, "cache", "voces_genaipro.json")
    idioma = (idioma or "es").split("-")[0].lower()
    cache = None
    if os.path.exists(cache_ruta):
        try:
            import json
            with open(cache_ruta, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = None
    try:
        clave = cargar_api_key()
    except BaseException:  # noqa: BLE001
        clave = ""
    huella = hashlib.sha256(clave.encode("utf-8")).hexdigest()[:12] if clave else ""
    fresca = False
    if isinstance(cache, dict) and cache.get("voces"):
        edad = time.time() - float(cache.get("epoch") or 0)
        fresca = edad < 7 * 86400 and (not huella or cache.get("clave") == huella)
        if cache.get("idioma") != idioma:
            fresca = False
    if not refrescar and fresca:
        return list(cache["voces"])
    if _simulado() or not clave:
        return list(cache["voces"]) if isinstance(cache, dict) and cache.get("voces") else []
    sesion = _sesion(clave)
    respuesta = sesion.get(
        f"{API_BASE}/labs/voices",
        params={"language": idioma, "page_size": 100, "page": 0},
        timeout=60)
    if respuesta.status_code != 200:
        if isinstance(cache, dict) and cache.get("voces"):
            return list(cache["voces"])
        raise RuntimeError(
            f"GenAI Pro /labs/voices HTTP {respuesta.status_code}: "
            f"{(respuesta.text or '')[:200]}")
    crudas = respuesta.json()
    if isinstance(crudas, dict):
        crudas = crudas.get("voices") or crudas.get("data") or []
    fichas = [_ficha(c) for c in crudas if isinstance(c, dict) and _ficha(c)["id"]]
    os.makedirs(os.path.dirname(cache_ruta), exist_ok=True)
    import json
    with open(cache_ruta, "w", encoding="utf-8") as fh:
        json.dump({"epoch": time.time(), "clave": huella, "idioma": idioma,
                   "voces": fichas}, fh, ensure_ascii=False, indent=2)
    return fichas


def listar_voces(idioma=None, refrescar=False, solo_nativas=False):
    return listar_voces_genaipro(idioma, refrescar)
