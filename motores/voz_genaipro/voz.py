"""Voz con GenAI Pro: Lyra (defecto, con clon) o Labs (ElevenLabs).

La ficha oficial es https://docs.genaipro.io/openapi.yaml.

Lyra es POST /v1/tts/lyra. El cuerpo lleva `content` y exactamente uno de
`voice_id` (preset de GET /v1/tts/lyra/voices) o `voice_asset_id` (clon de
GET /v1/tts/voice-assets). `language` hay que mandarlo siempre: si se omite
la API usa `vi`. El audio acaba en `result`. No hay marcas de palabra ni
campo de subtítulo en TtsLyraTaskDetail, y `content` no tiene tope. El
montaje necesita {"w","s","e"}, así que cada trozo se alinea en local. Si
la respuesta trajera tiempos de palabra o un subtítulo usable, se prefieren.
No se pide /v1/labs/task/subtitle con un id de Lyra.

Labs sigue en POST /v1/labs/task. Su subtítulo es la URL de un VTT: si trae
cues, se prefieren al alineador.

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

# El OpenAPI publica el servidor https://genaipro.io/api y las rutas /v1/…
# GENAIPRO_BASE ya incluye /api/v1, que es lo que se concatena con /labs/…
# y con /tts/lyra.
API_BASE = os.environ.get("GENAIPRO_BASE") or "https://genaipro.io/api/v1"
MODELO = os.environ.get("GENAIPRO_MODELO") or "eleven_multilingual_v2"
MODELOS = ("eleven_multilingual_v2", "eleven_turbo_v2_5",
           "eleven_flash_v2_5", "eleven_v3")
# Defectos del POST /v1/labs/task en el OpenAPI, no los del cliente antiguo.
ESTABILIDAD = 0.75
SIMILITUD = 0.5
TOPE_TROZO = int(os.environ.get("GENAIPRO_MAX_CHARS") or 4000)
# Lyra no documenta tope de `content`. Solo se trocea si se fija este entorno.
POLL_S = 3.0
MAX_ESPERA_S = 600.0
AIRE_S = 0.04
MOTOR_DEFECTO = "lyra"
#: «voz propia 2», el clon de la cuenta. El mismo id está en pasos/claves.py.
VOZ_CLON_DEFECTO = "01a08285-c365-7017-835f-53f700dd3040"
VOZ_CLON_TITULO = "voz propia 2"
IDIOMA_LYRA_DEFECTO = "es"
# El estudio nombra las emociones como Cartesia. Lyra admite este otro juego.
EMOCION_A_LYRA = {
    "anger": "angry",
    "sadness": "sad",
    "positivity": "content",
    "surprise": "excited",
}
_LYRA_EN_CURSO = {"pending", "cloning", "queued", "processing", "merging",
                  "process_merging"}

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


def tope_lyra():
    """0 si Lyra no debe trocear. El OpenAPI no pone maxLength en content."""
    crudo = (os.environ.get("GENAIPRO_LYRA_MAX_CHARS") or "").strip()
    if not crudo:
        return 0
    try:
        return max(0, int(crudo))
    except ValueError:
        return 0


def _ajustes_genaipro():
    """motor y voice_asset_id guardados. Vacios quieren decir el defecto."""
    ruta = os.path.join(_carpeta_secretos(), "claves.json")
    try:
        with open(ruta, "r", encoding="utf-8-sig") as fh:
            import json
            datos = json.load(fh)
    except (OSError, ValueError):
        return {"motor": "", "voice_asset_id": ""}
    cruda = datos.get("genaipro") if isinstance(datos, dict) else None
    if not isinstance(cruda, dict):
        return {"motor": "", "voice_asset_id": ""}
    motor = str(cruda.get("motor") or "").strip().lower()
    asset = str(cruda.get("voice_asset_id") or "").strip()
    return {
        "motor": motor if motor in ("labs", "lyra") else "",
        "voice_asset_id": asset,
    }


def motor_configurado(cfg=None):
    """labs o lyra. El entorno manda, luego lo guardado, luego lyra."""
    pedido = str((cfg or {}).get("motor") or "").strip().lower()
    if pedido in ("labs", "lyra"):
        return pedido
    entorno = (os.environ.get("GENAIPRO_MOTOR") or "").strip().lower()
    if entorno in ("labs", "lyra"):
        return entorno
    guardado = _ajustes_genaipro().get("motor") or ""
    return guardado or MOTOR_DEFECTO


def asset_configurado():
    """El clon de Lyra: entorno, luego el guardado, luego el de fabrica."""
    entorno = (os.environ.get("GENAIPRO_VOICE_ASSET_ID") or "").strip()
    if entorno:
        return entorno
    guardado = _ajustes_genaipro().get("voice_asset_id") or ""
    return guardado or VOZ_CLON_DEFECTO


def motor_de_toma(cfg):
    """Lyra si la toma eligio clon o preset, o si el motor configurado es lyra.

    Un voice_asset_id en la toma (no el de Configuracion) tambien es Lyra:
    Labs no acepta clones. El motor labs solo locuta cuando nadie ha elegido
    una voz de Lyra.
    """
    cfg = cfg or {}
    origen = str(cfg.get("voz_origen") or "").strip().lower()
    if origen in ("clon", "preset"):
        return "lyra"
    if str(cfg.get("voice_asset_id") or "").strip():
        return "lyra"
    return motor_configurado(cfg)


def idioma_lyra(cfg):
    """Codigo de dos letras. Siempre se manda: omitirlo deja el vietnamita."""
    pedido = str((cfg or {}).get("idioma") or "").strip().lower().replace("_", "-")
    corto = pedido.split("-", 1)[0]
    if len(corto) == 2 and corto.isalpha():
        return corto
    entorno = (os.environ.get("GENAIPRO_LYRA_IDIOMA") or "").strip().lower()
    if len(entorno) == 2 and entorno.isalpha():
        return entorno
    return IDIOMA_LYRA_DEFECTO


def elegir_voz_lyra(cfg):
    """(campo, id) con exactamente un campo: voice_id o voice_asset_id.

    Un voz_id de Cartesia guardado de antes no es un preset de Lyra. Sin
    voz_origen, se usa el clon configurado y ese id no se manda.
    """
    cfg = cfg or {}
    origen = str(cfg.get("voz_origen") or "").strip().lower()
    asset = str(cfg.get("voice_asset_id") or "").strip()
    voz = str(cfg.get("voz_id") or "").strip()
    if origen == "preset" and voz:
        return "voice_id", voz
    if origen == "clon":
        return "voice_asset_id", asset or voz or asset_configurado()
    if asset:
        return "voice_asset_id", asset
    return "voice_asset_id", asset_configurado()


def firma_de_voz(cfg):
    """Lo que distingue una preescucha de otra cuando el proveedor es este."""
    if motor_de_toma(cfg) == "lyra":
        campo, valor = elegir_voz_lyra(cfg)
        return f"lyra:{campo}:{valor}:{idioma_lyra(cfg)}:{_velocidad(cfg)}"
    return f"labs:{cfg.get('voz_id')}:{_modelo(cfg)}:{_velocidad(cfg)}"


def _emocion_lyra(cfg, avisar):
    """La primera emocion que Lyra entiende, o vacio."""
    for etiqueta in (cfg or {}).get("emociones") or []:
        nombre = str(etiqueta).partition(":")[0].strip().lower()
        if nombre in EMOCION_A_LYRA:
            return EMOCION_A_LYRA[nombre]
        if nombre in ("neutral", "angry", "excited", "content", "sad", "scared"):
            return nombre
        if nombre == "curiosity" and callable(avisar):
            avisar("Lyra no tiene la emocion curiosity: se locuta sin ella")
        elif nombre and callable(avisar):
            avisar(f"Lyra no locuta la emocion {nombre}: se deja fuera")
    return ""


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


def _esperar_tarea(sesion, tarea, ruta=None):
    ruta = ruta or f"{API_BASE}/labs/task/{tarea}"
    lyra = "/tts/lyra/" in ruta
    limite = time.monotonic() + MAX_ESPERA_S
    while time.monotonic() < limite:
        respuesta = sesion.get(ruta, timeout=60)
        if respuesta.status_code == 401:
            raise RuntimeError("GenAI Pro rechaza la clave (401)")
        if respuesta.status_code == 404 and lyra:
            raise RuntimeError("GenAI Pro no encuentra la tarea de Lyra")
        if respuesta.status_code == 429:
            time.sleep(_espera_reintento(respuesta))
            continue
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
        if lyra and estado and estado not in _LYRA_EN_CURSO:
            # un estado nuevo se espera igual: la ficha dice que el ciclo
            # acaba en completed o failed
            pass
        time.sleep(POLL_S)
    raise RuntimeError("GenAI Pro no ha terminado la toma a tiempo")


def _espera_reintento(respuesta):
    cruda = ""
    try:
        cruda = str(respuesta.headers.get("Retry-After") or "")
    except AttributeError:
        cruda = ""
    try:
        return max(0.5, min(30.0, float(cruda)))
    except (TypeError, ValueError):
        return POLL_S


def _error_de(respuesta):
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return (respuesta.text or "")[:240]
    if isinstance(cuerpo, dict):
        return str(cuerpo.get("error") or cuerpo.get("message") or cuerpo)[:240]
    return str(cuerpo)[:240]


def _post_json(sesion, url, cuerpo, intentos=3):
    respuesta = None
    for intento in range(intentos):
        respuesta = sesion.post(url, json=cuerpo, timeout=60)
        if respuesta.status_code == 429 and intento + 1 < intentos:
            time.sleep(_espera_reintento(respuesta))
            continue
        if respuesta.status_code == 503 and intento + 1 < intentos:
            time.sleep(POLL_S)
            continue
        return respuesta
    return respuesta


def _exigir_alta(respuesta, cual):
    if respuesta.status_code == 401:
        raise RuntimeError(
            "GenAI Pro rechaza la clave (401). Revisala en Configuracion.")
    if respuesta.status_code == 400:
        mensaje = _error_de(respuesta)
        if "not enough credit" in mensaje.lower():
            raise RuntimeError(
                "GenAI Pro no tiene credito para esta locucion. "
                "Recarga la cuenta.")
        raise RuntimeError(f"GenAI Pro: {mensaje}")
    if respuesta.status_code not in (200, 201):
        raise RuntimeError(
            f"GenAI Pro HTTP {respuesta.status_code}: {_error_de(respuesta)}")
    try:
        datos = respuesta.json()
    except ValueError as fallo:
        raise RuntimeError(f"GenAI Pro no ha devuelto JSON al crear {cual}") from fallo
    if not isinstance(datos, dict):
        raise RuntimeError(f"GenAI Pro no ha devuelto la tarea de {cual}")
    return datos


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
    respuesta = _post_json(sesion, f"{API_BASE}/labs/task", cuerpo)
    datos = _exigir_alta(respuesta, "Labs")
    tarea = datos.get("task_id") or datos.get("id")
    if not tarea:
        raise RuntimeError("GenAI Pro no ha devuelto task_id")
    ficha = _esperar_tarea(sesion, tarea)
    _locutar_trozo.ultima = ficha
    url = ficha.get("result") or ""
    if not url:
        raise RuntimeError("GenAI Pro ha terminado la tarea sin MP3")
    bajada = _bajar(sesion, url)
    if bajada.status_code != 200 or not bajada.content:
        raise RuntimeError(f"no se ha podido bajar el MP3 (HTTP {bajada.status_code})")
    pcm = audio().mp3_a_pcm(bajada.content)
    return pcm, str(tarea)


def _estimar_lyra(sesion, texto):
    """Caracteres facturables, o None. Estimar es gratis y no tumba la toma."""
    try:
        respuesta = sesion.post(
            f"{API_BASE}/tts/lyra/estimate",
            json={"content": texto}, timeout=30)
    except requests.RequestException:
        return None
    if respuesta.status_code != 200:
        return None
    try:
        return int(respuesta.json().get("characters"))
    except (TypeError, ValueError, AttributeError):
        return None


def _cuerpo_lyra(texto, cfg, avisar):
    campo, valor = elegir_voz_lyra(cfg)
    cuerpo = {
        "content": _limpiar_dicho(texto),
        "language": idioma_lyra(cfg),
        "speed": _velocidad(cfg),
        campo: valor,
    }
    emocion = _emocion_lyra(cfg, avisar)
    if emocion:
        cuerpo["emotion"] = emocion
    return cuerpo


def _locutar_lyra(sesion, texto, cfg, avisar=None):
    cuerpo = _cuerpo_lyra(texto, cfg, avisar)
    if "voice_id" in cuerpo and "voice_asset_id" in cuerpo:
        raise RuntimeError("Lyra admite voice_id o voice_asset_id, no los dos")
    respuesta = _post_json(sesion, f"{API_BASE}/tts/lyra", cuerpo)
    datos = _exigir_alta(respuesta, "Lyra")
    tarea = datos.get("id") or datos.get("task_id")
    if not tarea:
        raise RuntimeError("GenAI Pro no ha devuelto el id de la tarea de Lyra")
    ficha = _esperar_tarea(sesion, tarea, f"{API_BASE}/tts/lyra/{tarea}")
    url = ficha.get("result") or ""
    if isinstance(url, dict):
        url = url.get("url") or url.get("audio_url") or ""
    if not url:
        raise RuntimeError("Lyra ha terminado la tarea sin MP3")
    bajada = _bajar(sesion, url)
    if bajada.status_code != 200 or not bajada.content:
        raise RuntimeError(f"no se ha podido bajar el MP3 (HTTP {bajada.status_code})")
    pcm = audio().mp3_a_pcm(bajada.content)
    return pcm, str(tarea), ficha


def _numero(valor):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _marcas_de_lista(filas):
    salida = []
    if not isinstance(filas, list):
        return []
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        palabra = fila.get("w") or fila.get("word") or fila.get("text") or ""
        palabra = " ".join(str(palabra).split())
        ini = _numero(fila.get("s", fila.get("start")))
        fin = _numero(fila.get("e", fila.get("end")))
        if not palabra or ini is None or fin is None:
            continue
        if fin < ini:
            fin = ini
        salida.append({"w": palabra, "s": round(ini, 3), "e": round(fin, 3)})
    return salida


def _marcas_de_mapa(mapa):
    if not isinstance(mapa, dict):
        return []
    palabras = mapa.get("words") or mapa.get("word")
    inicios = mapa.get("start") or mapa.get("starts")
    fines = mapa.get("end") or mapa.get("ends")
    if not isinstance(palabras, list) or not isinstance(inicios, list):
        return []
    fines = fines if isinstance(fines, list) else []
    salida = []
    for indice, palabra in enumerate(palabras):
        if indice >= len(inicios):
            break
        ini = _numero(inicios[indice])
        fin = _numero(fines[indice]) if indice < len(fines) else None
        palabra = " ".join(str(palabra).split())
        if not palabra or ini is None:
            continue
        if fin is None or fin < ini:
            fin = ini
        salida.append({"w": palabra, "s": round(ini, 3), "e": round(fin, 3)})
    return salida


def _tiempos_de_ficha(ficha, texto, sesion=None):
    """Marcas de la propia respuesta, o None si no trae tiempos usables.

    Sirve para Lyra y para Labs. Un subtítulo con cues cuenta: se reparte
    dentro de cada cue. No se inventan tiempos si no hay nada.
    """
    if not isinstance(ficha, dict):
        return None
    for clave in ("word_timestamps", "words", "timestamps", "alignment"):
        valor = ficha.get(clave)
        marcas = _marcas_de_lista(valor) or _marcas_de_mapa(valor)
        if marcas:
            return marcas
    sub = ficha.get("subtitle") or ficha.get("srt") or ""
    if isinstance(sub, str) and sub.strip().startswith("http") and sesion is not None:
        bajada = _bajar(sesion, sub.strip())
        if bajada.status_code == 200 and bajada.text and _CUE.search(bajada.text):
            return palabras_de_srt(bajada.text, _limpiar_dicho(texto))
    if isinstance(sub, str) and _CUE.search(sub):
        return palabras_de_srt(sub, _limpiar_dicho(texto))
    return None


def _marcas_del_trozo(sesion, pcm, texto, cfg, ficha, avisos, motor):
    """Tiempos de la respuesta si los hay; si no, el alineador local."""
    propias = _tiempos_de_ficha(ficha, texto, sesion)
    if propias:
        return propias, "proveedor"
    if motor == "labs":
        tarea = ""
        if isinstance(ficha, dict):
            tarea = str(ficha.get("id") or ficha.get("task_id") or "")
        if tarea:
            srt = _subtitulo(sesion, tarea)
            if srt and _CUE.search(srt):
                return palabras_de_srt(srt, _limpiar_dicho(texto)), "subtitulo"
    marcas, como = _alinear_trozo(pcm, texto, (cfg or {}).get("idioma") or "es", avisos)
    if marcas:
        return marcas, "alineador"
    return palabras_de_srt("", _limpiar_dicho(texto)) or _reparto_plano(
        _limpiar_dicho(texto), 0.0,
        max(0.4, 0.32 * max(1, len(_limpiar_dicho(texto).split())))), "aproximadas"


def _juntar(pcms, marcas_trozo):
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
    return wav, modulo.duracion_pcm(unido), palabras


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


def _cerrar_toma(pcms, marcas_trozo, origen, avanza):
    wav, duracion, palabras = _juntar(pcms, marcas_trozo)
    if origen == "aproximadas":
        avanza(0.95, "las marcas de palabra son aproximadas: falta el alineador")
    elif origen == "subtitulo":
        avanza(0.95, "las marcas salen del subtitulo")
    toma.marcas = origen
    return wav, duracion, palabras


def _toma_labs(texto, cfg, avanza):
    if not cfg.get("voz_id"):
        raise RuntimeError(
            "GenAI Pro en Labs necesita una voz. Elige una en el catalogo "
            "o pasa el motor a Lyra.")
    _avisar_estilo(cfg, avanza)
    trozos = trocear(texto)
    if not trozos:
        raise ValueError("no hay texto que sintetizar")
    sesion = _sesion(cargar_api_key())
    avisos = []
    pcms, marcas_trozo = [], []
    origen = "alineador"
    for indice, trozo in enumerate(trozos):
        avanza(0.05 + 0.7 * indice / len(trozos),
               f"locutando {indice + 1} de {len(trozos)}")
        pcm, tarea = _locutar_trozo(sesion, trozo, cfg)
        ficha = getattr(_locutar_trozo, "ultima", None) or {}
        if isinstance(ficha, dict) and not ficha.get("id"):
            ficha = dict(ficha, id=tarea)
        marcas, como = _marcas_del_trozo(
            sesion, pcm, trozo, cfg, ficha, avisos, "labs")
        if como == "aproximadas":
            origen = "aproximadas"
        elif origen == "alineador" and como != "alineador":
            origen = como
        if avisos and como == "aproximadas":
            avanza(0.1, avisos[-1])
        pcms.append(pcm)
        marcas_trozo.append(marcas)
    return _cerrar_toma(pcms, marcas_trozo, origen, avanza)


def _toma_lyra(texto, cfg, avanza):
    texto = _limpiar_dicho(texto)
    tope = tope_lyra()
    trozos = trocear(texto, tope) if tope else ([texto] if texto else [])
    if not trozos:
        raise ValueError("no hay texto que sintetizar")
    sesion = _sesion(cargar_api_key())
    toma.creditos = _estimar_lyra(sesion, texto)
    if toma.creditos is not None:
        avanza(0.04, f"Lyra: {toma.creditos} creditos (1 por caracter)")
    avisos = []
    pcms, marcas_trozo = [], []
    origen = "alineador"

    def avisar(mensaje):
        avisos.append(mensaje)
        avanza(0.03, mensaje)

    for indice, trozo in enumerate(trozos):
        avanza(0.05 + 0.7 * indice / len(trozos),
               f"locutando {indice + 1} de {len(trozos)}")
        pcm, _tarea, ficha = _locutar_lyra(sesion, trozo, cfg, avisar)
        marcas, como = _marcas_del_trozo(
            sesion, pcm, trozo, cfg, ficha, avisos, "lyra")
        if como == "aproximadas":
            origen = "aproximadas"
        elif origen == "alineador" and como != "alineador":
            origen = como
        if avisos and como == "aproximadas":
            avanza(0.1, avisos[-1])
        pcms.append(pcm)
        marcas_trozo.append(marcas)
    return _cerrar_toma(pcms, marcas_trozo, origen, avanza)


def toma(texto, cfg, progreso=None):
    """Un texto -> (wav, duracion_s, palabras). Lyra por defecto, Labs si se pide."""
    def avanza(fraccion, mensaje=""):
        if callable(progreso):
            progreso(fraccion, mensaje)
        return fraccion

    cfg = dict(cfg or {})
    if not str(texto or "").strip():
        raise ValueError("no hay texto que sintetizar")
    if _simulado():
        avanza(0.5, "simulando la voz de GenAI Pro")
        toma.marcas = "simuladas"
        toma.creditos = None
        return _toma_simulada(texto, cfg)
    toma.creditos = None
    if motor_de_toma(cfg) == "labs":
        return _toma_labs(texto, cfg, avanza)
    return _toma_lyra(texto, cfg, avanza)


toma.marcas = ""
toma.creditos = None


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


def _lista_de(cuerpo):
    if isinstance(cuerpo, list):
        return cuerpo
    if not isinstance(cuerpo, dict):
        return []
    for clave in ("items", "voices", "data", "results"):
        if isinstance(cuerpo.get(clave), list):
            return cuerpo[clave]
    return []


def _ficha_clon(cruda):
    identificador = str(cruda.get("id") or "").strip()
    if not identificador:
        return None
    if str(cruda.get("status") or "ready").lower() == "failed":
        return None
    titulo = str(cruda.get("title") or identificador).strip()
    return {
        "id": identificador,
        "nombre": titulo,
        "descripcion": "clon de voz",
        "idioma": "",
        "genero": "",
        "pais": "",
        "locales": [],
        "locales_nativos": [],
        "pro": False,
        "publica": False,
        "clon": True,
        "nativa": True,
        "proveedor": "genaipro",
        "motor": "lyra",
        "voice_asset_id": identificador,
    }


def _ficha_preset_lyra(cruda, idioma):
    identificador = str(cruda.get("id") or cruda.get("voice_id") or "").strip()
    if not identificador:
        return None
    declarado = str(cruda.get("language") or cruda.get("lang") or idioma or "")
    declarado = declarado.split("-")[0].lower()
    return {
        "id": identificador,
        "nombre": str(cruda.get("name") or cruda.get("title") or identificador),
        "descripcion": str(cruda.get("description") or cruda.get("style") or "").strip(),
        "idioma": declarado,
        "genero": cruda.get("gender") or "",
        "pais": "",
        "locales": [declarado] if declarado else [],
        "locales_nativos": [declarado] if declarado else [],
        "pro": False,
        "publica": True,
        "clon": False,
        "nativa": True,
        "proveedor": "genaipro",
        "motor": "lyra",
        "voice_id": identificador,
    }


def _clon_defecto():
    return _ficha_clon({
        "id": asset_configurado(),
        "title": VOZ_CLON_TITULO if asset_configurado() == VOZ_CLON_DEFECTO
        else asset_configurado(),
        "status": "ready",
    })


def _bajar_clones(sesion):
    fichas, pagina = [], 1
    while pagina <= 5:
        respuesta = sesion.get(
            f"{API_BASE}/tts/voice-assets",
            params={"page": pagina, "limit": 20}, timeout=60)
        if respuesta.status_code != 200:
            raise RuntimeError(
                f"GenAI Pro /tts/voice-assets HTTP {respuesta.status_code}: "
                f"{_error_de(respuesta)}")
        cuerpo = respuesta.json()
        filas = _lista_de(cuerpo)
        for cruda in filas:
            if isinstance(cruda, dict):
                ficha = _ficha_clon(cruda)
                if ficha:
                    fichas.append(ficha)
        total = cuerpo.get("total") if isinstance(cuerpo, dict) else None
        if not filas or (isinstance(total, int) and pagina * 20 >= total):
            break
        if len(filas) < 20:
            break
        pagina += 1
    return fichas


def _bajar_presets_lyra(sesion, idioma):
    fichas, cursor, paginas = [], "", 0
    while paginas < 8:
        params = {"limit": 100, "language": idioma}
        if cursor:
            params["starting_after"] = cursor
        respuesta = sesion.get(
            f"{API_BASE}/tts/lyra/voices", params=params, timeout=60)
        if respuesta.status_code != 200:
            raise RuntimeError(
                f"GenAI Pro /tts/lyra/voices HTTP {respuesta.status_code}: "
                f"{_error_de(respuesta)}")
        cuerpo = respuesta.json()
        filas = _lista_de(cuerpo)
        ultimo = ""
        for cruda in filas:
            if not isinstance(cruda, dict):
                continue
            ficha = _ficha_preset_lyra(cruda, idioma)
            if not ficha:
                continue
            if ficha["idioma"] and ficha["idioma"] != idioma:
                continue
            fichas.append(ficha)
            ultimo = ficha["id"]
        paginas += 1
        hay_mas = isinstance(cuerpo, dict) and cuerpo.get("has_more")
        if not filas or not hay_mas or not ultimo or ultimo == cursor:
            break
        cursor = ultimo
    return fichas


def listar_voces_genaipro(idioma=None, refrescar=False):
    """Clones de la cuenta y presets de Lyra, cacheados una semana.

    Labs no entra en este catalogo: un preset de aqui es voice_id de Lyra
    y un clon es voice_asset_id. El motor labs se elige en Configuracion
    cuando la toma no trae ni lo uno ni lo otro.
    """
    import json
    raiz = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cache_ruta = os.path.join(raiz, "cache", "voces_genaipro_lyra.json")
    idioma = (idioma or "es").split("-")[0].lower()
    cache = None
    if os.path.exists(cache_ruta):
        try:
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
        if isinstance(cache, dict) and cache.get("voces"):
            return list(cache["voces"])
        return [_clon_defecto()]
    sesion = _sesion(clave)
    clones = _bajar_clones(sesion)
    presets = _bajar_presets_lyra(sesion, idioma)
    vistos = {f["id"] for f in clones}
    fichas = list(clones)
    for preset in presets:
        if preset["id"] not in vistos:
            fichas.append(preset)
    os.makedirs(os.path.dirname(cache_ruta), exist_ok=True)
    with open(cache_ruta, "w", encoding="utf-8") as fh:
        json.dump({"epoch": time.time(), "clave": huella, "idioma": idioma,
                   "voces": fichas}, fh, ensure_ascii=False, indent=2)
    return fichas


def listar_voces(idioma=None, refrescar=False, solo_nativas=False):
    fichas = listar_voces_genaipro(idioma, refrescar)
    if not solo_nativas:
        return fichas
    return [f for f in fichas if f.get("clon") or f.get("nativa", True)]
