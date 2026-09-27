"""Motor de imagen sobre snapgen.ai (por defecto gpt-image-2-lower).

Misma interfaz publica que motores/imagen_openai/imagen.py. La cuenta del
dueno esta en snapgen.ai, no en snapgen.org. El contrato sale de
https://docs.snapgen.ai (contenido en docs-content.zip):

  * base https://api.snapgen.ai, cabecera x-api-key (no Bearer)
  * POST /uapi/v1/imagen/gpt-image-2-lower, multipart
  * referencias en el campo repetido `files` (png, jpg, jpeg, webp), hasta 10
  * 16:9 en `aspect_ratio`; este modelo no admite mode, resolution ni background
  * la respuesta trae uuid y status 1; se sondea GET /uapi/v1/history/{uuid}
    hasta status 2 (hecha) o 3 (fallida)
  * 2 creditos por imagen. El dolar por credito no esta publicado
  * sin saldo: 402 NOT_ENOUGH_CREDIT o NOT_ENOUGH_AND_LOCK_CREDIT
  * clave mala: 400 API_KEY_REQUIRED o 404 API_KEY_NOT_FOUND
  * plan Premium: 402 GPT_IMAGE_2_LOWER_PREMIUM_PLAN_REQUIRED, que no es saldo
  * la clave se comprueba con GET /uapi/v1/account, que no genera nada

SNAPGEN_MODELO puede cambiar a otro modelo documentado con referencias y 16:9
(gpt-image-2, nano-banana-pro, nano-banana-2, nano-banana-2-lite). Un nombre
que las docs no listan no se envia a un endpoint inventado.
"""
import hashlib
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from PIL import Image

API_BASE = "https://api.snapgen.ai"
MODELO = "gpt-image-2-lower"
TAMANO_APAISADO = "16:9"
TAMANOS = {"apaisado": "16:9", "cuadrado": "1:1", "vertical": "9:16"}

#: Los ocho ratios de gpt-image-2 y gpt-image-2-lower. Cualquier otro es 400.
RATIOS_GPT = {"1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "3:2", "2:3"}
#: generate_image solo documenta estos.
RATIOS_NANO = {"1:1", "16:9", "9:16", "4:3", "3:4"}
RESOLUCIONES_GPT = {"1K", "2K", "4K", "8K", "10K", "12K"}
RESOLUCIONES_NANO = {"1K", "2K", "4K"}

#: Tope documentado de gpt-image-2-lower (files + ref_history). El de los
#: otros modelos no esta publicado, y no se recorta por una cifra inventada.
TOPE_REFS = 10
POLL_S = 12.0
MAX_ESPERA_S = 600.0
ESPERA_LIMITE_S = 20.0
ESPERA_MAXIMA_S = 120.0

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

#: familia: lower | gpt | nano. Solo entran modelos con referencias y 16:9.
MODELOS = {
    "gpt-image-2-lower": {
        "ruta": "/uapi/v1/imagen/gpt-image-2-lower",
        "familia": "lower",
        "tope": 10,
        "ratios": RATIOS_GPT,
        "resolucion": "720p",
    },
    "gpt-image-2": {
        "ruta": "/uapi/v1/imagen/gpt-image-2",
        "familia": "gpt",
        "tope": None,
        "ratios": RATIOS_GPT,
        "resolucion": None,
    },
    "nano-banana-pro": {
        "ruta": "/uapi/v1/generate_image",
        "familia": "nano",
        "tope": None,
        "ratios": RATIOS_NANO,
        "resolucion": None,
    },
    "nano-banana-2": {
        "ruta": "/uapi/v1/generate_image",
        "familia": "nano",
        "tope": None,
        "ratios": RATIOS_NANO,
        "resolucion": None,
    },
    "nano-banana-2-lite": {
        "ruta": "/uapi/v1/generate_image",
        "familia": "nano",
        "tope": None,
        "ratios": RATIOS_NANO,
        "resolucion": None,
    },
}

CODIGOS_SIN_SALDO = {"NOT_ENOUGH_CREDIT", "NOT_ENOUGH_AND_LOCK_CREDIT"}
CODIGOS_CLAVE = {"API_KEY_REQUIRED", "API_KEY_NOT_FOUND", "USER_NOT_FOUND"}

_gasto = {"usd": 0.0, "llamadas": 0, "creditos": 0}
_FRENO = threading.Condition()


class SinSaldo(RuntimeError):
    """No quedan creditos en snapgen.ai, o estan bloqueados por otra generacion."""


def _simulado():
    valor = str(os.environ.get("ESTUDIO_SIMULAR", "")).strip().lower()
    return valor not in ("", "0", "false", "no")


def api_base():
    return (os.environ.get("SNAPGEN_API_BASE") or API_BASE).rstrip("/")


def modelo_activo():
    return (os.environ.get("SNAPGEN_MODELO") or MODELO).strip() or MODELO


def ficha_modelo(nombre=None):
    """El modelo documentado, o ValueError si las docs no lo listan."""
    nombre = (nombre or modelo_activo()).strip()
    ficha = MODELOS.get(nombre)
    if not ficha:
        raise ValueError(
            f"snapgen.ai no documenta el modelo {nombre!r} con referencias "
            f"y 16:9. Los que si estan en las docs son: "
            + ", ".join(sorted(MODELOS)))
    return nombre, ficha


def _carpeta_secretos():
    return os.environ.get("ESTUDIO_SECRETOS") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "secretos")


def _tarifas():
    raiz = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ruta = os.environ.get("ESTUDIO_TARIFAS") or os.path.join(raiz, "tarifas.json")
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        datos = {}
    return datos.get("snapgen") or {}


def creditos_publicados(modelo=None):
    """Creditos por imagen publicados para ese modelo, o None.

    Solo gpt-image-2-lower tiene un precio fijo en las docs (2). El resto
    calcula por modo y resolucion y no publica la tabla: no se inventa.
    """
    nombre = (modelo or modelo_activo()).strip()
    tabla = _tarifas().get("creditos_por_imagen")
    if isinstance(tabla, dict):
        valor = tabla.get(nombre)
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            return int(valor)
        return None
    if nombre == "gpt-image-2-lower":
        if isinstance(tabla, (int, float)) and not isinstance(tabla, bool):
            return int(tabla)
        return 2
    return None


def usd_de(creditos, modelo=None):
    """Dolares, o None si usd_por_credito no esta relleno."""
    if creditos is None:
        return None
    bruto = _tarifas().get("usd_por_credito")
    if not isinstance(bruto, (int, float)) or isinstance(bruto, bool):
        return None
    return round(float(creditos) * float(bruto), 6)


def tamano_de(tamano, ficha):
    puesto = os.environ.get("SNAPGEN_SIZE_APAISADO") or TAMANO_APAISADO
    if tamano in TAMANOS and tamano != "apaisado":
        puesto = TAMANOS[tamano]
    elif tamano in TAMANOS:
        puesto = os.environ.get("SNAPGEN_SIZE_APAISADO") or TAMANOS["apaisado"]
    elif tamano:
        puesto = str(tamano)
    ratios = ficha["ratios"]
    if puesto not in ratios:
        raise ValueError(
            f"snapgen.ai no admite el tamano {puesto!r} en este modelo. Valen "
            + ", ".join(sorted(ratios)))
    return puesto


class _Cuenta:
    def __init__(self, nombre, clave, etiqueta=""):
        self.nombre = nombre
        self.clave = clave
        self.etiqueta = etiqueta
        self.libre_en = [0.0]
        self._sin_saldo = False
        self.clave_rechazada = False
        self.limite = [0]
        self.vistas = [False]

    @property
    def sin_saldo(self):
        return self._sin_saldo

    @sin_saldo.setter
    def sin_saldo(self, valor):
        self._sin_saldo = bool(valor)

    def espera_estimada(self, cuantas=1, ahora=None):
        ahora = time.monotonic() if ahora is None else ahora
        return max(0.0, self.libre_en[0] - ahora)


_CUENTAS = []
_SELLO = [None]


def _sello_claves():
    ruta = os.path.join(_carpeta_secretos(), "claves.json")
    try:
        st = os.stat(ruta)
        return (st.st_mtime, st.st_size, os.environ.get("SNAPGEN_API_KEY") or "")
    except OSError:
        return (0, 0, os.environ.get("SNAPGEN_API_KEY") or "")


def _cuentas():
    sello = _sello_claves()
    if _CUENTAS and _SELLO[0] == sello:
        return _CUENTAS
    cuentas = []
    entorno = (os.environ.get("SNAPGEN_API_KEY") or "").strip()
    if entorno:
        cuentas.append(_Cuenta("SNAPGEN_API_KEY", entorno, "entorno"))
    else:
        ruta = os.path.join(_carpeta_secretos(), "claves.json")
        try:
            with open(ruta, "r", encoding="utf-8-sig") as fh:
                datos = json.load(fh)
        except (OSError, ValueError):
            datos = {}
        for indice, cruda in enumerate(datos.get("snapgen") or [], start=1):
            if not isinstance(cruda, dict) or cruda.get("activa") is False:
                continue
            clave = str(cruda.get("clave") or "").strip()
            if not clave:
                continue
            cuentas.append(_Cuenta(
                cruda.get("etiqueta") or f"snapgen {indice}", clave,
                str(cruda.get("etiqueta") or "")))
    _CUENTAS[:] = cuentas
    _SELLO[0] = sello
    return _CUENTAS


def cuentas_para_la_pantalla():
    fichas = []
    for cuenta in _cuentas():
        fichas.append({
            "nombre": cuenta.nombre,
            "etiqueta": cuenta.etiqueta,
            "cola": f"…{cuenta.clave[-4:]}" if len(cuenta.clave or "") > 4 else "",
            "sin_saldo": bool(cuenta.sin_saldo),
            "clave_rechazada": bool(cuenta.clave_rechazada),
            "limite_por_minuto": int(cuenta.limite[0]),
            "medido": bool(cuenta.vistas[0]),
            "espera_s": round(cuenta.espera_estimada(), 2),
            "proveedor": "snapgen",
        })
    return fichas


def cargar_api_key():
    cuentas = _cuentas()
    if not cuentas:
        raise SystemExit("Falta SNAPGEN_API_KEY")
    return cuentas[0].clave


def _elegir(fija=None):
    if fija is not None:
        return fija
    if not _cuentas():
        raise RuntimeError(
            "Falta la clave de SnapGen. Se pone en Configuracion, o en "
            "SNAPGEN_API_KEY.")
    vivas = [c for c in _cuentas() if not c.sin_saldo and not c.clave_rechazada]
    if not vivas and _cuentas() and all(c.clave_rechazada for c in _cuentas()):
        raise RuntimeError(
            "SnapGen no reconoce la clave. Se crea en snapgen.ai, en "
            "Service Integration.")
    if not vivas:
        raise SinSaldo(
            "SnapGen no tiene creditos para esta imagen. Recarga en "
            "https://snapgen.ai/profile/credits/ y retoma: lo ya generado "
            "no se vuelve a pagar.")
    return min(vivas, key=lambda c: c.espera_estimada())


def _esperar(cuenta):
    while True:
        with _FRENO:
            falta = cuenta.libre_en[0] - time.monotonic()
        if falta <= 0:
            return
        time.sleep(min(falta, 1.0))


def _frenar(cuenta, segundos, motivo=""):
    with _FRENO:
        cuenta.libre_en[0] = max(cuenta.libre_en[0], time.monotonic() + float(segundos))
    if motivo:
        print(f"[imagen snapgen] {cuenta.nombre}: esperando {segundos:.0f}s "
              f"({motivo})", flush=True)


def _segundos_de(crudo, defecto):
    texto = str(crudo or "").strip().lower()
    encaje = re.match(r"^([\d.]+)\s*(ms|s|m)?$", texto)
    if not encaje:
        return defecto
    try:
        valor = float(encaje.group(1))
    except ValueError:
        return defecto
    unidad = encaje.group(2) or "s"
    segundos = valor / 1000 if unidad == "ms" else (valor * 60 if unidad == "m" else valor)
    return min(max(segundos, 0.0), ESPERA_MAXIMA_S)


def normalizar(ruta, cache_dir, lado_max=1024):
    """PNG RGBA en la cache, igual que el motor de OpenAI.

    El nombre lleva la huella de la ruta completa: dos referencias distintas
    se llaman igual mas a menudo de lo que parece.
    """
    os.makedirs(cache_dir, exist_ok=True)
    nombre, extension = os.path.splitext(os.path.basename(ruta))
    firma = hashlib.sha1(os.path.normcase(os.path.abspath(ruta)).encode("utf-8"))
    destino = os.path.join(cache_dir, f"{nombre}__{firma.hexdigest()[:8]}{extension or '.png'}")
    try:
        if (os.path.exists(destino)
                and os.path.getmtime(destino) >= os.path.getmtime(ruta)):
            return destino
    except OSError:
        pass
    img = Image.open(ruta).convert("RGBA")
    if max(img.size) > lado_max:
        escala = lado_max / max(img.size)
        img = img.resize((max(1, int(img.width * escala)),
                          max(1, int(img.height * escala))), Image.LANCZOS)
    temporal = destino + ".tmp"
    img.save(temporal, "PNG")
    os.replace(temporal, destino)
    return destino


def _recortar(rutas, tope=TOPE_REFS):
    """Si sobran, suelta primero la continuidad mas vieja.

    El orden del estudio es estilo, estructura, reparto y, al final, los
    planos anteriores: el ultimo es el mas reciente. La continuidad vieja es
    la penultima, no la ultima. tope None no recorta: ese modelo no publica
    un maximo.
    """
    rutas = list(rutas)
    if not tope or tope < 1:
        return rutas
    while len(rutas) > tope:
        if len(rutas) >= 2:
            del rutas[-2]
        else:
            rutas.pop()
    return rutas


def _parte(ruta):
    """(nombre, bytes, mime) en un formato que el endpoint acepta."""
    ext = os.path.splitext(ruta)[1].lower()
    if ext in _MIME:
        with open(ruta, "rb") as fh:
            return os.path.basename(ruta), fh.read(), _MIME[ext]
    img = Image.open(ruta).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    base = os.path.splitext(os.path.basename(ruta))[0] + ".png"
    return base, buf.getvalue(), "image/png"


def empaquetar(referencias, tope=TOPE_REFS):
    """Referencias locales listas para el campo multipart `files`.

    Devuelve (rutas_usadas, partes). Cada parte es
    ("files", (nombre, bytes, mime)). Sin referencias, partes va vacia: el
    plano de texto sigue siendo valido.
    """
    faltan = [r for r in referencias if not os.path.exists(r)]
    if faltan:
        raise ValueError("estas imagenes de referencia no existen: "
                         + ", ".join(str(f) for f in faltan[:5]))
    rutas = _recortar(referencias, tope)
    partes = [("files", _parte(ruta)) for ruta in rutas]
    return rutas, partes


def _cabeceras(clave):
    return {"x-api-key": clave, "Accept": "application/json"}


def _error_de(respuesta):
    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = {}
    detalle = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
    if isinstance(detalle, dict):
        codigo = str(detalle.get("error_code") or detalle.get("code") or "")
        mensaje = str(detalle.get("error_message") or detalle.get("message") or "")
        return codigo, mensaje
    if isinstance(detalle, str):
        return "", detalle
    return "", str(getattr(respuesta, "text", "") or "")[:300]


def _es_premium(codigo):
    return codigo == "PREMIUM_PLAN_REQUIRED" or codigo.endswith("_PREMIUM_PLAN_REQUIRED")


def _estado(cuerpo):
    if not isinstance(cuerpo, dict):
        return 1
    bruto = cuerpo.get("status")
    try:
        return int(bruto)
    except (TypeError, ValueError):
        texto = str(cuerpo.get("status_desc") or bruto or "").strip().lower()
        if texto in ("2", "completed", "complete"):
            return 2
        if texto in ("3", "failed", "error"):
            return 3
        return 1


def _creditos_de(cuerpo, modelo):
    if isinstance(cuerpo, dict):
        for clave in ("used_credit", "estimated_credit"):
            valor = cuerpo.get(clave)
            if isinstance(valor, bool) or valor is None or valor == "":
                continue
            try:
                numero = int(valor)
            except (TypeError, ValueError):
                continue
            if numero > 0:
                return numero
    return creditos_publicados(modelo)


def _url_imagen(cuerpo):
    """URL o ('b64', datos) de la imagen terminada. None si todavia no esta."""
    if not isinstance(cuerpo, dict):
        return None
    imagenes = cuerpo.get("generated_image") or []
    if isinstance(imagenes, list):
        for item in imagenes:
            if not isinstance(item, dict):
                continue
            for clave in ("image_url", "file_download_url", "image_uri"):
                if item.get(clave):
                    return str(item[clave])
            if item.get("base64_data"):
                return ("b64", item["base64_data"])
    resultado = cuerpo.get("generate_result")
    if isinstance(resultado, str) and resultado.startswith(("http://", "https://")):
        return resultado
    return None


def _a_png(contenido):
    img = Image.open(io.BytesIO(contenido))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _host_de(url):
    return (urlparse(url).hostname or "").lower()


def _bajar(url, clave):
    if isinstance(url, tuple) and url and url[0] == "b64":
        import base64
        crudo = url[1]
        if isinstance(crudo, str) and "," in crudo and crudo.strip().startswith("data:"):
            crudo = crudo.split(",", 1)[1]
        return _a_png(base64.b64decode(crudo))
    respuesta = requests.get(url, timeout=120)
    propio = _host_de(url) == _host_de(api_base()) or _host_de(url).endswith(".snapgen.ai")
    if (respuesta.status_code != 200 or not respuesta.content) and propio:
        respuesta = requests.get(url, headers=_cabeceras(clave), timeout=120)
    if respuesta.status_code != 200 or not respuesta.content:
        raise RuntimeError(
            f"SnapGen ha dado una imagen que no se ha podido bajar "
            f"(HTTP {respuesta.status_code})")
    return _a_png(respuesta.content)


def _esperar_historia(cuenta, uuid, progreso=None):
    limite = time.monotonic() + MAX_ESPERA_S
    url = f"{api_base()}/uapi/v1/history/{uuid}"
    while time.monotonic() < limite:
        time.sleep(POLL_S)
        respuesta = requests.get(url, headers=_cabeceras(cuenta.clave), timeout=60)
        codigo, mensaje = _error_de(respuesta)
        if codigo in CODIGOS_CLAVE or respuesta.status_code == 401:
            cuenta.clave_rechazada = True
            raise RuntimeError(
                "SnapGen no reconoce la clave al mirar la historia "
                f"({codigo or respuesta.status_code})")
        if respuesta.status_code != 200:
            continue
        cuerpo = respuesta.json()
        estado = _estado(cuerpo)
        if callable(progreso) and cuerpo.get("status_percentage") is not None:
            progreso(cuerpo.get("status_percentage"))
        if estado == 2:
            encontrado = _url_imagen(cuerpo)
            if not encontrado:
                raise RuntimeError(
                    "SnapGen ha marcado la historia hecha y no hay imagen")
            return encontrado, cuerpo
        if estado == 3:
            detalle = str(cuerpo.get("error_message") or cuerpo.get("error_code")
                          or cuerpo.get("status_desc") or "failed")
            raise RuntimeError("SnapGen no ha generado la imagen: " + detalle)
    raise RuntimeError(
        f"SnapGen no ha terminado la imagen en {int(MAX_ESPERA_S)} s "
        f"(historia {uuid})")


def _campos(nombre, ficha, prompt, ratio, quality):
    campos = {"prompt": prompt, "aspect_ratio": ratio}
    if ficha["familia"] == "gpt":
        modo = (os.environ.get("SNAPGEN_MODE") or quality or "low").strip().lower()
        if modo not in ("low", "medium", "high"):
            modo = "low"
        campos["mode"] = modo
        resolucion = (os.environ.get("SNAPGEN_RESOLUTION") or "").strip()
        if resolucion:
            if resolucion not in RESOLUCIONES_GPT:
                raise ValueError(
                    f"snapgen.ai no documenta la resolucion {resolucion!r} "
                    "en gpt-image-2")
            campos["resolution"] = resolucion
        fondo = (os.environ.get("SNAPGEN_BACKGROUND") or "").strip()
        if fondo:
            if fondo not in ("auto", "transparent", "opaque"):
                raise ValueError(
                    f"snapgen.ai no documenta background {fondo!r}")
            campos["background"] = fondo
    elif ficha["familia"] == "nano":
        campos["model"] = nombre
        resolucion = (os.environ.get("SNAPGEN_RESOLUTION") or "1K").strip()
        if resolucion not in RESOLUCIONES_NANO:
            raise ValueError(
                f"generate_image no documenta la resolucion {resolucion!r}")
        campos["resolution"] = resolucion
        campos["output_format"] = "png"
        estilo = (os.environ.get("SNAPGEN_STYLE") or "").strip()
        if estilo:
            campos["style"] = estilo
    return campos


def _multipart(campos, partes):
    salida = []
    for clave, valor in campos.items():
        salida.append((clave, (None, str(valor))))
    salida.extend(partes or [])
    return salida


def _fallar_http(cuenta, respuesta, codigo, mensaje):
    """Levanta si el codigo no se reintenta. Devuelve True si hay que reintentar."""
    texto = mensaje or codigo or f"HTTP {respuesta.status_code}"
    if codigo in CODIGOS_SIN_SALDO or (
            respuesta.status_code == 402 and not _es_premium(codigo)):
        cuenta.sin_saldo = True
        raise SinSaldo(
            "SnapGen no tiene creditos para esta imagen "
            f"({codigo or '402'}). Recarga en "
            "https://snapgen.ai/profile/credits/ y retoma: lo ya generado "
            "no se vuelve a pagar.")
    if _es_premium(codigo):
        raise RuntimeError(
            "Este modelo de SnapGen pide el plan Premium "
            f"({codigo}). No es falta de creditos: el saldo no lo arregla. "
            + texto[:200])
    if codigo in CODIGOS_CLAVE or respuesta.status_code == 401:
        cuenta.clave_rechazada = True
        raise RuntimeError(
            "SnapGen no reconoce la clave "
            f"({codigo or respuesta.status_code}). Se crea en snapgen.ai, "
            "en Service Integration. " + texto[:200])
    if respuesta.status_code in (400, 413):
        raise RuntimeError(
            f"SnapGen ha rechazado la peticion ({codigo or respuesta.status_code}): "
            + texto[:300])
    return False


def _png_simulado(prompt, referencias, quality, tamano):
    img = Image.new("RGB", (64, 36), (30, 48, 72))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    nombre, ficha = ficha_modelo()
    try:
        ratio = tamano_de(tamano, ficha)
    except ValueError:
        ratio = "16:9"
    creditos = creditos_publicados(nombre)
    return buf.getvalue(), {
        "segundos": 0.0, "quality": quality, "refs": len(referencias or []),
        "coste": None, "creditos": creditos, "modelo": nombre,
        "tamano": ratio, "resolucion": ficha.get("resolucion"),
        "proveedor": "snapgen", "simulado": True, "usage": {},
    }


def generar(prompt, referencias, *, quality="low", tamano="apaisado",
            api_key=None, reintentos=4):
    referencias = list(referencias or [])
    if _simulado():
        return _png_simulado(prompt, referencias, quality, tamano)
    if not str(prompt or "").strip():
        raise ValueError("SnapGen necesita un prompt")
    nombre, ficha = ficha_modelo()
    ratio = tamano_de(tamano, ficha)
    rutas, partes = empaquetar(referencias, ficha["tope"]) if referencias else ([], [])
    campos = _campos(nombre, ficha, prompt, ratio, quality)
    cuerpo = _multipart(campos, partes)
    fija = _Cuenta("clave explicita", api_key) if api_key else None
    ultimo = "SnapGen no ha contestado"
    url = f"{api_base()}{ficha['ruta']}"

    for intento in range(reintentos + 1):
        cuenta = _elegir(fija)
        _esperar(cuenta)
        t0 = time.time()
        respuesta = requests.post(
            url, headers=_cabeceras(cuenta.clave), files=cuerpo, timeout=200)
        codigo, mensaje = _error_de(respuesta)
        if 200 <= respuesta.status_code < 300:
            try:
                creado = respuesta.json()
            except ValueError:
                creado = {}
            if not isinstance(creado, dict) or not creado.get("uuid"):
                raise RuntimeError(
                    "SnapGen ha contestado sin uuid: "
                    + (respuesta.text or "")[:200])
            estado = _estado(creado)
            if estado == 3:
                raise RuntimeError(
                    "SnapGen no ha generado la imagen: "
                    + str(creado.get("error_message") or creado.get("status_desc")
                          or "failed"))
            if estado == 2 and _url_imagen(creado):
                destino, historia = _url_imagen(creado), creado
            else:
                destino, historia = _esperar_historia(cuenta, str(creado["uuid"]))
            png = _bajar(destino, cuenta.clave)
            creditos = _creditos_de(historia, nombre)
            if creditos is None:
                creditos = _creditos_de(creado, nombre)
            precio = usd_de(creditos, nombre)
            if precio:
                _gasto["usd"] += precio
            if creditos:
                _gasto["creditos"] += int(creditos)
            _gasto["llamadas"] += 1
            return png, {
                "segundos": round(time.time() - t0, 1),
                "quality": quality,
                "refs": len(rutas),
                "coste": precio,
                "creditos": creditos,
                "modelo": nombre,
                "tamano": ratio,
                "resolucion": ficha.get("resolucion") or campos.get("resolution"),
                "proveedor": "snapgen",
                "usage": {},
                "uuid": str(creado.get("uuid") or ""),
            }
        try:
            _fallar_http(cuenta, respuesta, codigo, mensaje)
        except (SinSaldo, RuntimeError):
            raise
        ultimo = f"HTTP {respuesta.status_code}: {(mensaje or codigo)[:200]}"
        if respuesta.status_code == 429 and intento < reintentos:
            espera = _segundos_de((respuesta.headers or {}).get("retry-after"),
                                  ESPERA_LIMITE_S)
            _frenar(cuenta, max(1.0, espera), "429")
            continue
        if respuesta.status_code in (500, 502, 503) and intento < reintentos:
            time.sleep(min(3 * (intento + 1), 30))
            continue
        break
    raise RuntimeError(ultimo)


def generar_lote(trabajos, *, concurrencia=4):
    def uno(trabajo):
        try:
            png, meta = generar(trabajo["prompt"], trabajo["referencias"],
                                quality=trabajo.get("quality", "low"),
                                tamano=trabajo.get("tamano", "apaisado"))
        except Exception as exc:  # noqa: BLE001
            return {"id": trabajo.get("id"), "error": str(exc)}
        os.makedirs(os.path.dirname(trabajo["destino"]), exist_ok=True)
        with open(trabajo["destino"], "wb") as fh:
            fh.write(png)
        return {"id": trabajo.get("id"), "destino": trabajo["destino"], **meta}

    with ThreadPoolExecutor(max_workers=concurrencia) as pool:
        return list(pool.map(uno, trabajos))


def gasto():
    return dict(_gasto, usd=round(_gasto["usd"], 4), creditos=int(_gasto["creditos"]))
