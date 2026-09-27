"""Motor de imagen sobre SnapGen (gpt-image-2.5-ext).

Misma interfaz publica que motores/imagen_openai/imagen.py: quien pide una
imagen no sabe que proveedor la ha hecho. SnapGen cobra por imagen entregada,
no por tokens, y admite el plano sin referencias (el moodboard descrito).

Las referencias viajan como data URL JPEG, en el mismo orden en que el prompt
las cita. El cuerpo tiene que quedarse por debajo de 9 MiB: si no cabe, se
encogen y, si aun asi sobran, se suelta primero la continuidad mas vieja.
"""
import base64
import hashlib
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

API_BASE = os.environ.get("SNAPGEN_API_BASE") or "https://api.snapgen.org/v1"
MODELO = os.environ.get("SNAPGEN_MODELO") or "gpt-image-2.5-ext"
#: El apaisado del estudio. 16:9 es el de la ficha; 3:2 tambien vale en el modelo.
TAMANO_APAISADO = os.environ.get("SNAPGEN_SIZE_APAISADO") or "16:9"
TAMANOS = {"apaisado": TAMANO_APAISADO, "cuadrado": "1:1", "vertical": "9:16"}
RATIOS_OK = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9", "auto"}

TOPE_REFS = 16
TOPE_CUERPO = 9 * 1024 * 1024
POLL_S = 12.0
MAX_ESPERA_S = 600.0
ESPERA_LIMITE_S = 20.0
ESPERA_MAXIMA_S = 120.0

_gasto = {"usd": 0.0, "llamadas": 0}
_FRENO = threading.Condition()


class SinSaldo(RuntimeError):
    """No queda saldo en SnapGen, o la clave ha llegado a su tope de gasto."""


def _simulado():
    valor = str(os.environ.get("ESTUDIO_SIMULAR", "")).strip().lower()
    return valor not in ("", "0", "false", "no")


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
    tabla = ((datos.get("snapgen") or {}).get("usd_por_imagen") or {})
    return {"1K": 0.0085, "2K": 0.014, "4K": 0.021, **{
        k: float(v) for k, v in tabla.items() if isinstance(v, (int, float))}}


def resolucion_de(quality):
    """low/medium salen a 1K; high a 2K. 4K no se pide solo."""
    return "2K" if str(quality or "").lower() == "high" else "1K"


def tamano_de(tamano):
    puesto = TAMANOS.get(tamano, TAMANOS["apaisado"])
    if puesto not in RATIOS_OK:
        raise ValueError(
            f"SnapGen no admite el tamano {puesto!r}. Valen "
            + ", ".join(sorted(RATIOS_OK)))
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
            "SnapGen rechaza la clave (401). Revisala en Configuracion.")
    if not vivas:
        raise SinSaldo(
            "SnapGen no tiene saldo, o la clave ha llegado a su tope de gasto. "
            "Recarga en snapgen.org y retoma: lo ya generado no se vuelve a pagar.")
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
    la penultima, no la ultima.
    """
    rutas = list(rutas)
    while len(rutas) > tope:
        if len(rutas) >= 2:
            del rutas[-2]
        else:
            rutas.pop()
    return rutas


def _data_url(ruta, lado, calidad):
    img = Image.open(ruta).convert("RGB")
    if max(img.size) > lado:
        escala = lado / max(img.size)
        img = img.resize((max(1, int(img.width * escala)),
                          max(1, int(img.height * escala))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=calidad, optimize=True)
    crudo = buf.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(crudo).decode("ascii"), crudo


def empaquetar(referencias):
    """Data URLs en el orden original, con el cuerpo por debajo de 9 MiB.

    Devuelve (rutas_usadas, urls, huellas) donde huellas son los bytes que
    entran en la clave de idempotencia.
    """
    faltan = [r for r in referencias if not os.path.exists(r)]
    if faltan:
        raise ValueError("estas imagenes de referencia no existen: "
                         + ", ".join(str(f) for f in faltan[:5]))
    rutas = _recortar(referencias, TOPE_REFS)
    lado, calidad = 1024, 80
    while True:
        pares = [_data_url(r, lado, calidad) for r in rutas]
        urls = [p[0] for p in pares]
        peso = sum(len(u.encode("ascii")) for u in urls) + 8192
        if peso <= TOPE_CUERPO or not rutas:
            return rutas, urls, [p[1] for p in pares]
        if lado > 768:
            lado = 768
            continue
        if lado > 512:
            lado = 512
            continue
        if calidad > 45:
            calidad -= 10
            continue
        if len(rutas) > 1:
            rutas = _recortar(rutas, len(rutas) - 1)
            lado, calidad = 768, 70
            continue
        raise RuntimeError(
            "la referencia no cabe en 9 MiB ni encogida. SnapGen limita el "
            "cuerpo de la peticion.")


def _clave_idem(prompt, huellas, tamano, resolucion, modelo):
    firma = hashlib.sha1()
    firma.update(str(prompt).encode("utf-8"))
    firma.update(b"\0")
    firma.update(f"{modelo}|{tamano}|{resolucion}".encode("utf-8"))
    for trozo in huellas:
        firma.update(hashlib.sha1(trozo).digest())
    return firma.hexdigest()


def _error_de(respuesta):
    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = {}
    error = cuerpo.get("error") if isinstance(cuerpo, dict) else None
    error = error if isinstance(error, dict) else {}
    return str(error.get("code") or ""), str(error.get("message") or respuesta.text or "")


def _url_invalida(codigo, mensaje):
    texto = f"{codigo} {mensaje}".lower()
    return ("invalid_image" in texto or "invalid image url" in texto
            or "image url" in texto and "invalid" in texto)


def _id_tarea(respuesta):
    cabecera = (respuesta.headers or {}).get("x-gateway-task-id")
    if cabecera:
        return str(cabecera).strip()
    lugar = (respuesta.headers or {}).get("Location") or ""
    if "/tasks/" in lugar:
        return lugar.rstrip("/").rsplit("/", 1)[-1]
    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = {}
    if isinstance(cuerpo, dict):
        for clave in ("id", "task_id"):
            if cuerpo.get(clave):
                return str(cuerpo[clave])
    return ""


def _urls_de(cuerpo):
    if not isinstance(cuerpo, dict):
        return []
    datos = cuerpo.get("data")
    if isinstance(datos, list):
        return [d.get("url") for d in datos if isinstance(d, dict) and d.get("url")]
    resultado = cuerpo.get("result") if isinstance(cuerpo.get("result"), dict) else {}
    urls = resultado.get("urls") or resultado.get("url")
    if isinstance(urls, str):
        return [urls]
    if isinstance(urls, list):
        return [u for u in urls if u]
    return []


def _a_png(contenido):
    img = Image.open(io.BytesIO(contenido))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _bajar(url, clave):
    respuesta = requests.get(url, timeout=120)
    if respuesta.status_code != 200 or not respuesta.content:
        # el CDN a veces quiere el mismo bearer
        respuesta = requests.get(
            url, headers={"Authorization": f"Bearer {clave}"}, timeout=120)
    if respuesta.status_code != 200 or not respuesta.content:
        raise RuntimeError(
            f"SnapGen ha dado una imagen que no se ha podido bajar "
            f"(HTTP {respuesta.status_code})")
    return _a_png(respuesta.content)


def _esperar_tarea(cuenta, tarea, progreso=None):
    limite = time.monotonic() + MAX_ESPERA_S
    while time.monotonic() < limite:
        time.sleep(POLL_S)
        respuesta = requests.get(
            f"{API_BASE}/tasks/{tarea}",
            headers={"Authorization": f"Bearer {cuenta.clave}"},
            timeout=60)
        if respuesta.status_code == 401:
            cuenta.clave_rechazada = True
            raise RuntimeError("SnapGen rechaza la clave (401) al mirar la tarea")
        if respuesta.status_code != 200:
            continue
        cuerpo = respuesta.json()
        estado = str(cuerpo.get("status") or "").lower()
        if callable(progreso) and cuerpo.get("progress") is not None:
            progreso(cuerpo.get("progress"))
        if estado == "succeeded":
            urls = _urls_de(cuerpo)
            if not urls:
                raise RuntimeError("SnapGen ha marcado la tarea hecha y no hay imagen")
            return urls[0], cuerpo
        if estado in ("failed", "expired"):
            error = cuerpo.get("error") if isinstance(cuerpo.get("error"), dict) else {}
            raise RuntimeError(
                "SnapGen no ha generado la imagen: "
                + str(error.get("message") or estado))
        if estado == "reconciliation_required":
            raise RuntimeError(
                "SnapGen ha dejado la tarea en reconciliation_required: no se "
                "vuelve a enviar, hay que mirarla en el panel")
    raise RuntimeError(
        f"SnapGen no ha terminado la imagen en {int(MAX_ESPERA_S)} s "
        f"(tarea {tarea})")


def _png_simulado(prompt, referencias, quality, tamano):
    img = Image.new("RGB", (64, 36), (30, 48, 72))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    resolucion = resolucion_de(quality)
    return buf.getvalue(), {
        "segundos": 0.0, "quality": quality, "refs": len(referencias or []),
        "coste": 0.0, "modelo": MODELO, "tamano": tamano_de(tamano),
        "resolucion": resolucion, "proveedor": "snapgen", "simulado": True,
        "usage": {},
    }


def generar(prompt, referencias, *, quality="low", tamano="apaisado",
            api_key=None, reintentos=4):
    referencias = list(referencias or [])
    if _simulado():
        return _png_simulado(prompt, referencias, quality, tamano)
    if not str(prompt or "").strip():
        raise ValueError("SnapGen necesita un prompt")
    ratio = tamano_de(tamano)
    resolucion = resolucion_de(quality)
    rutas, urls, huellas = empaquetar(referencias) if referencias else ([], [], [])
    cuerpo = {"model": MODELO, "prompt": prompt, "resolution": resolucion,
              "size": ratio, "n": 1}
    if urls:
        cuerpo["image_urls"] = urls
    idem = _clave_idem(prompt, huellas, ratio, resolucion, MODELO)
    fija = _Cuenta("clave explicita", api_key) if api_key else None
    ultimo = "SnapGen no ha contestado"
    precio = _tarifas().get(resolucion, 0.0)

    for intento in range(reintentos + 1):
        cuenta = _elegir(fija)
        _esperar(cuenta)
        t0 = time.time()
        respuesta = requests.post(
            f"{API_BASE}/images/generations",
            headers={"Authorization": f"Bearer {cuenta.clave}",
                     "Content-Type": "application/json",
                     "Prefer": "respond-async",
                     "Idempotency-Key": idem},
            json=cuerpo, timeout=200)
        codigo, mensaje = _error_de(respuesta)
        if respuesta.status_code in (200, 202):
            if respuesta.status_code == 200 and _urls_de(respuesta.json()):
                url = _urls_de(respuesta.json())[0]
                meta_extra = {}
            else:
                tarea = _id_tarea(respuesta)
                if not tarea:
                    raise RuntimeError(
                        "SnapGen ha contestado 202 sin id de tarea: "
                        + (respuesta.text or "")[:200])
                url, meta_extra = _esperar_tarea(cuenta, tarea)
            png = _bajar(url, cuenta.clave)
            _gasto["usd"] += precio
            _gasto["llamadas"] += 1
            return png, {
                "segundos": round(time.time() - t0, 1),
                "quality": quality,
                "refs": len(rutas),
                "coste": precio,
                "modelo": MODELO,
                "tamano": ratio,
                "resolucion": resolucion,
                "proveedor": "snapgen",
                "usage": {},
                "charged_microusd": (meta_extra or {}).get("charged_microusd"),
            }
        ultimo = f"HTTP {respuesta.status_code}: {mensaje[:200]}"
        if respuesta.status_code == 402 or codigo in (
                "insufficient_funds", "api_key_spend_limit_exceeded"):
            cuenta.sin_saldo = True
            raise SinSaldo(
                "SnapGen no tiene saldo para esta imagen "
                f"({codigo or '402'}). Recarga en snapgen.org y retoma: "
                "lo ya generado no se vuelve a pagar.")
        if respuesta.status_code == 401:
            cuenta.clave_rechazada = True
            raise RuntimeError(
                "SnapGen rechaza la clave (401). Revisala en Configuracion. "
                + ultimo)
        if _url_invalida(codigo, mensaje):
            raise RuntimeError(
                "SnapGen ha rechazado las imagenes de referencia: hacen falta "
                "URLs publicas (http o https). " + mensaje[:200])
        if respuesta.status_code == 429 and intento < reintentos:
            espera = _segundos_de((respuesta.headers or {}).get("retry-after"),
                                  ESPERA_LIMITE_S)
            _frenar(cuenta, max(1.0, espera), "429")
            continue
        if respuesta.status_code in (502, 503) and intento < reintentos:
            time.sleep(min(3 * (intento + 1), 30))
            continue
        if respuesta.status_code in (400, 413):
            break
        if intento < reintentos and respuesta.status_code >= 500:
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
    return dict(_gasto, usd=round(_gasto["usd"], 4))
