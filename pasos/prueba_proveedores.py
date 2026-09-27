"""SnapGen, GenAI Pro y el cambio de proveedor, sin red y sin gastar.

    python pasos/prueba_proveedores.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import wave

_TMP = tempfile.mkdtemp(prefix="proveedores_prueba_")
os.environ["ESTUDIO_SECRETOS"] = os.path.join(_TMP, "secretos")
os.environ["ESTUDIO_TARIFAS"] = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tarifas.json")
os.environ.pop("ESTUDIO_SIMULAR", None)
os.environ.pop("ESTUDIO_PROVEEDOR_IMAGEN", None)
os.environ.pop("ESTUDIO_PROVEEDOR_VOZ", None)
os.environ.pop("SNAPGEN_API_KEY", None)
os.environ.pop("GENAIPRO_API_KEY", None)
os.makedirs(os.environ["ESTUDIO_SECRETOS"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ajustes  # noqa: E402
import claves  # noqa: E402
import comprobar_claves  # noqa: E402
import marcas_tts  # noqa: E402
import medios  # noqa: E402
from nucleo import coste  # noqa: E402

FALLOS = []


def comprobar(condicion, texto):
    if condicion:
        print(f"  ok   {texto}")
    else:
        FALLOS.append(texto)
        print(f"  FALLO  {texto}")


def igual(a, b, texto):
    comprobar(a == b, f"{texto}  ({a!r})")


class Respuesta:
    def __init__(self, status, cuerpo=None, cabeceras=None, contenido=b"", texto=""):
        self.status_code = status
        self.headers = cabeceras or {}
        self._cuerpo = cuerpo if cuerpo is not None else {}
        self.content = contenido or json.dumps(self._cuerpo).encode("utf-8")
        self.text = texto or self.content.decode("utf-8", "replace")

    def json(self):
        if isinstance(self._cuerpo, (dict, list)):
            return self._cuerpo
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def png_de(color=(10, 20, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, "PNG")
    return buf.getvalue()


def prueba_proveedor():
    print("proveedor")
    igual(claves.proveedor_imagen(), "openai", "sin claves sigue OpenAI")
    igual(claves.proveedor_voz(), "cartesia", "sin claves sigue Cartesia")
    claves.guardar({
        "snapgen": [{"etiqueta": "la mia", "clave": "snap-clave-123456", "activa": True}],
        "genaipro": {"clave": "jwt-genaipro-123456"},
    })
    igual(claves.proveedor_imagen(), "snapgen", "con clave de SnapGen, esa es la de la instalacion")
    igual(claves.proveedor_voz(), "genaipro", "y GenAI Pro en la voz")
    ficha = claves.resumen()
    comprobar("clave" not in json.dumps({k: ficha[k] for k in ("snapgen", "genaipro")}),
              "el resumen no lleva la clave entera")
    comprobar(ficha["snapgen"][0]["cola"].endswith("3456"), "y si la cola")
    claves.guardar({"proveedores": {"imagen": "openai", "voz": "cartesia"}})
    igual(claves.proveedor_imagen(), "openai", "elegir OpenAI se queda")
    igual(claves.proveedor_voz(), "cartesia", "y Cartesia tambien")
    os.environ["ESTUDIO_PROVEEDOR_IMAGEN"] = "snapgen"
    os.environ["ESTUDIO_PROVEEDOR_VOZ"] = "genaipro"
    igual(claves.proveedor_imagen(), "snapgen", "el entorno manda sobre lo guardado")
    igual(claves.proveedor_voz(), "genaipro", "en la voz igual")
    os.environ.pop("ESTUDIO_PROVEEDOR_IMAGEN")
    os.environ.pop("ESTUDIO_PROVEEDOR_VOZ")
    igual(coste.creditos_snapgen("gpt-image-2-lower"), 2,
          "gpt-image-2-lower son 2 creditos")
    comprobar(coste.tarifa_snapgen("gpt-image-2-lower") is None,
              "el dolar por credito no esta publicado")
    comprobar(coste.creditos_snapgen("gpt-image-2") is None,
              "gpt-image-2 no publica tabla: no se inventa")
    comprobar(coste.tarifa_caracter("genaipro") is None,
              "GenAI Pro no tiene precio por caracter: sale sin tarifa")
    tabla = {f["calidad"]: f for f in ajustes.tabla_snapgen()}
    igual(tabla["low"]["modelo"], "gpt-image-2-lower", "el modelo por defecto es el lower")
    igual(tabla["low"]["creditos"], 2, "baja son 2 creditos")
    igual(tabla["medium"]["creditos"], 2, "media tambien, el modelo no tiene escalones")
    igual(tabla["high"]["creditos"], 2, "alta igual")
    igual(tabla["high"]["resolucion"], "720p", "la salida documentada es 720p")
    comprobar(tabla["low"]["sin_tarifa"] and tabla["high"]["sin_tarifa"],
              "sin dolar publicado, las tres salen sin tarifa")
    motor = medios.motor_imagen()
    comprobar(motor.__file__.replace("\\", "/").endswith("imagen_openai/imagen.py"),
              "con openai elegido, el motor es el de siempre")
    claves.guardar({"proveedores": {"imagen": "snapgen"}})
    motor = medios.motor_imagen()
    comprobar("imagen_snapgen" in motor.__file__.replace("\\", "/"),
              "al elegir snapgen cambia el motor")


def prueba_empaquetar():
    print("referencias")
    imagen = medios.motor("imagen_snapgen/imagen.py")
    from PIL import Image
    rutas = []
    for i in range(18):
        ruta = os.path.join(_TMP, f"ref{i:02d}.png")
        Image.new("RGB", (1400, 900), (i * 10 % 255, 40, 80)).save(ruta, "PNG")
        rutas.append(ruta)
    recortadas = imagen._recortar(rutas, imagen.TOPE_REFS)
    igual(len(recortadas), 10, "mas de 10 se quedan en 10")
    igual(recortadas[-1], rutas[-1], "la continuidad mas reciente se queda")
    comprobar(rutas[-2] not in recortadas, "la continuidad vieja es la primera que sale")
    usadas, partes = imagen.empaquetar(rutas[:4])
    comprobar(all(p[0] == "files" and p[1][2] == "image/png" for p in partes),
              "las referencias van como ficheros multipart png")
    igual(len(usadas), len(partes), "el orden se conserva")
    vacias, partes0 = imagen.empaquetar([])
    igual(vacias, [], "sin referencias no se inventa ninguna")
    igual(partes0, [], "y no se manda ningun fichero")


def _campo(files, clave):
    for nombre, valor in files or []:
        if nombre == clave and isinstance(valor, tuple):
            return valor[1]
    return None


def prueba_snapgen_red():
    print("snapgen")
    imagen = medios.motor("imagen_snapgen/imagen.py")
    imagen._gasto.update(usd=0.0, llamadas=0, creditos=0)
    imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
    imagen._SELLO[0] = imagen._sello_claves()
    llamadas = []
    estado = {"veces": 0}
    os.environ.pop("SNAPGEN_MODELO", None)
    os.environ.pop("SNAPGEN_API_BASE", None)
    os.environ.pop("SNAPGEN_RESOLUTION", None)
    os.environ.pop("SNAPGEN_MODE", None)

    def post(url, **kw):
        cabeceras = kw.get("headers") or {}
        files = kw.get("files") or []
        llamadas.append(("POST", url, cabeceras, files))
        prompt = _campo(files, "prompt")
        if prompt == "sin-saldo":
            return Respuesta(402, {"detail": {
                "error_code": "NOT_ENOUGH_CREDIT",
                "error_message": "Not enough credits"}})
        if prompt == "premium":
            return Respuesta(402, {"detail": {
                "error_code": "GPT_IMAGE_2_LOWER_PREMIUM_PLAN_REQUIRED",
                "message": "Premium plan is required"}})
        if prompt == "clave-mala":
            return Respuesta(404, {"detail": {
                "error_code": "API_KEY_NOT_FOUND",
                "error_message": "Api key is not found"}})
        if prompt == "demasiadas":
            return Respuesta(400, {"detail": {
                "error_code": "TOO_MANY_IMAGES",
                "error_message": "too many"}})
        if prompt == "limite":
            return Respuesta(429, {"detail": {"error_code": "RATE_LIMIT_ERROR",
                                             "message": "slow down"}},
                             {"retry-after": "1"})
        return Respuesta(200, {
            "uuid": "hist-prueba", "status": 1, "status_desc": "Processing",
            "model_name": "gpt-image-2-lower", "estimated_credit": 2,
            "generate_result": None,
        })

    def get(url, **kw):
        llamadas.append(("GET", url, kw.get("headers") or {}))
        if "/uapi/v1/history/" in url:
            estado["veces"] += 1
            if estado["veces"] < 2:
                return Respuesta(200, {"uuid": "hist-prueba", "status": 1,
                                       "status_percentage": 30})
            return Respuesta(200, {
                "uuid": "hist-prueba", "status": 2, "status_desc": "COMPLETED",
                "used_credit": 2,
                "generated_image": [{
                    "image_url": "https://cdn.ejemplo/img.png",
                    "aspect_ratio": "16:9",
                }],
            })
        return Respuesta(200, None, contenido=png_de())

    original = (imagen.requests.post, imagen.requests.get, imagen.time.sleep, imagen.POLL_S)
    imagen.requests.post = post
    imagen.requests.get = get
    imagen.time.sleep = lambda *_a, **_k: None
    imagen._esperar = lambda *_a, **_k: None
    imagen.POLL_S = 0
    try:
        png, meta = imagen.generar("un farol", [], quality="medium", tamano="apaisado")
        comprobar(png[:8] == b"\x89PNG\r\n\x1a\n", "el status 1 se sondea y vuelve un PNG")
        igual(meta["proveedor"], "snapgen", "la meta dice snapgen")
        igual(meta["modelo"], "gpt-image-2-lower", "el modelo por defecto es el lower")
        igual(meta["tamano"], "16:9", "el apaisado sale 16:9")
        igual(meta["resolucion"], "720p", "la salida fija es 720p")
        igual(meta["creditos"], 2, "se anotan 2 creditos, no un dolar inventado")
        comprobar(meta["coste"] is None, "sin usd_por_credito el coste en dolares es None")
        url, cabecera, files = llamadas[0][1], llamadas[0][2], llamadas[0][3]
        comprobar(url == "https://api.snapgen.ai/uapi/v1/imagen/gpt-image-2-lower",
                  "el alta va a api.snapgen.ai, al endpoint del lower")
        comprobar(cabecera.get("x-api-key") == "clave-snap-prueba", "autentica con x-api-key")
        comprobar("Authorization" not in cabecera and "Prefer" not in cabecera
                  and "Idempotency-Key" not in cabecera,
                  "no manda Bearer, ni Prefer, ni idempotencia")
        comprobar(_campo(files, "resolution") is None and _campo(files, "mode") is None,
                  "el lower no recibe mode ni resolution")
        igual(_campo(files, "aspect_ratio"), "16:9", "el aspect_ratio viaja en el formulario")
        comprobar(not any(nombre == "files" for nombre, _v in files),
                  "sin referencias no se adjunta ningun fichero")
        png_h, meta_h = imagen.generar("detalle", [], quality="high", tamano="vertical")
        igual(meta_h["resolucion"], "720p", "high no cambia la resolucion del lower")
        igual(meta_h["tamano"], "9:16", "vertical es 9:16")
        comprobar(png_h[:4] == b"\x89PNG", "y tambien es PNG")
        alta_h = [x for x in llamadas if x[0] == "POST"][-1]
        igual(_campo(alta_h[3], "aspect_ratio"), "9:16", "el vertical viaja como aspect_ratio")
        imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
        try:
            imagen.generar("sin-saldo", [])
            comprobar(False, "NOT_ENOUGH_CREDIT tiene que ser SinSaldo")
        except imagen.SinSaldo:
            comprobar(True, "NOT_ENOUGH_CREDIT es SinSaldo")
        imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
        try:
            imagen.generar("premium", [])
            comprobar(False, "el plan Premium tiene que fallar")
        except imagen.SinSaldo:
            comprobar(False, "pedir Premium no es quedarse sin creditos")
        except RuntimeError as fallo:
            comprobar("Premium" in str(fallo), "y dice que hace falta el plan Premium")
        imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
        try:
            imagen.generar("clave-mala", [])
            comprobar(False, "API_KEY_NOT_FOUND tiene que rechazar la clave")
        except RuntimeError as fallo:
            comprobar("clave" in str(fallo).lower(), "404 API_KEY_NOT_FOUND dice que la clave no vale")
        imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
        antes = len(llamadas)
        try:
            imagen.generar("demasiadas", [])
            comprobar(False, "TOO_MANY_IMAGES tiene que fallar")
        except RuntimeError as fallo:
            comprobar("TOO_MANY_IMAGES" in str(fallo), "y nombra el codigo")
        igual(len([x for x in llamadas[antes:] if x[0] == "POST"]), 1,
              "un 400 no se reintenta")
        from PIL import Image as _Imagen
        ref = os.path.join(_TMP, "ref-snap.png")
        _Imagen.new("RGB", (8, 8), (1, 2, 3)).save(ref, "PNG")
        imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
        antes = len(llamadas)
        imagen.generar("con-ref", [ref], tamano="apaisado")
        alta = [x for x in llamadas[antes:] if x[0] == "POST"][0]
        ficheros = [p for p in alta[3] if p[0] == "files"]
        igual(len(ficheros), 1, "la referencia viaja en el campo files")
        comprobar(ficheros[0][1][2] == "image/png", "y el mime es png")
    finally:
        imagen.requests.post, imagen.requests.get, imagen.time.sleep, imagen.POLL_S = original
        imagen._CUENTAS[:] = []
        imagen._SELLO[0] = None


def prueba_simular():
    print("simular")
    os.environ["ESTUDIO_SIMULAR"] = "1"
    try:
        imagen = medios.motor("imagen_snapgen/imagen.py")
        def prohibido(*_a, **_k):
            raise AssertionError("no tenia que salir a la red")
        anterior = imagen.requests.post
        imagen.requests.post = prohibido
        png, meta = imagen.generar("hola", [], quality="low")
        comprobar(png[:4] == b"\x89PNG" and meta.get("simulado"),
                  "SnapGen en simulado no llama a la red")
        imagen.requests.post = anterior
        voz = medios.motor("voz_genaipro/voz.py")
        wav, duracion, palabras = voz.toma(
            "Hola mundo desde el estudio.", {"voz_id": "voz-prueba", "idioma": "es"},
            lambda *_a, **_k: None)
        comprobar(wav[:4] == b"RIFF" and palabras and palabras[0]["w"] == "Hola",
                  "GenAI Pro en simulado devuelve wav y palabras")
        comprobar(duracion > 0 and palabras[-1]["e"] <= duracion + 0.05,
                  "las palabras caben en la duracion")
    finally:
        os.environ.pop("ESTUDIO_SIMULAR", None)


def prueba_voz():
    print("voz")
    avisos = []
    salida = marcas_tts.para_genaipro(
        'Hola.<break time="1200ms"/> <speed ratio="0.8"/>Adios.', avisos)
    comprobar('<break time="1.2s"/>' in salida, "la pausa pasa a segundos")
    comprobar("<speed" not in salida, "y se quita lo que Cartesia solo entiende")
    comprobar(avisos, "avisando de lo que se quita")
    voz = medios.motor("voz_genaipro/voz.py")
    trozos = voz.trocear("Una frase. " * 800, tope=4000)
    comprobar(all(len(t) <= 4000 for t in trozos), "ningun trozo pasa de 4000")
    comprobar(len(trozos) > 1, "un texto largo se parte")
    srt = ("1\n00:00:00,000 --> 00:00:01,000\nHola mundo\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\ndesde aqui\n")
    marcas = voz.palabras_de_srt(srt, "Hola mundo desde aqui")
    igual([m["w"] for m in marcas], ["Hola", "mundo", "desde", "aqui"],
          "el SRT reparte sus palabras")
    comprobar(marcas[0]["s"] == 0 and marcas[-1]["e"] == 2, "de punta a punta del cue")
    audio = voz.audio()
    a = b"\x01\x00" * 1000
    b = b"\x02\x00" * 1000
    unido, offsets = audio.unir([a, b], 0.04)
    igual(len(offsets), 2, "un offset por trozo")
    comprobar(offsets[0] == 0 and offsets[1] > audio.duracion_pcm(a),
              "el segundo trozo empieza despues del aire")
    # ffmpeg de verdad, si esta: un mp3 minimo y de vuelta a PCM.
    if shutil.which("ffmpeg"):
        ruta = os.path.join(_TMP, "tono.mp3")
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
             "-q:a", "9", ruta],
            check=True, capture_output=True)
        pcm = audio.mp3_a_pcm(open(ruta, "rb").read())
        wav = audio.wav_desde_pcm(pcm)
        comprobar(wav[:4] == b"RIFF" and len(pcm) > 1000, "el MP3 vuelve a PCM 44,1 kHz")
    else:
        print("  --   ffmpeg no esta: no se prueba el MP3")
    cartesia = medios.motor("voz_cartesia/voz.py")
    reparto = cartesia._repartir_palabras(
        [{"id": "B1", "narracion": "Hola mundo"},
         {"id": "B2", "narracion": "desde aqui"}],
        marcas)
    igual([p["w"] for p in reparto["B1"]], ["Hola", "mundo"],
          "el reparto de siempre lee las marcas alineadas")
    igual([p["w"] for p in reparto["B2"]], ["desde", "aqui"], "y el segundo bloque")
    vtt = ("WEBVTT\n\n00:00.000 --> 00:01.000\nHola mundo\n\n"
           "00:01.000 --> 00:02.000\ndesde aqui\n")
    marcas = voz.palabras_de_srt(vtt, "Hola mundo desde aqui")
    igual([m["w"] for m in marcas], ["Hola", "mundo", "desde", "aqui"],
          "el VTT de la ficha se reparte igual")
    comprobar("genaipro.io/api/v1" in voz.API_BASE,
              "la base por defecto es genaipro.io")
    igual(voz.ESTABILIDAD, 0.75, "stability sale del OpenAPI")
    igual(voz.SIMILITUD, 0.5, "y similarity tambien")

    class Sesion:
        def __init__(self):
            self.posts = []
            self.n = 0

        def post(self, url, json=None, timeout=None):
            self.posts.append((url, json))
            return Respuesta(200, {"task_id": "tarea-1"})

        def get(self, url, timeout=None):
            if "media" in url:
                return Respuesta(200, contenido=b"ID3falso")
            self.n += 1
            if self.n == 1:
                return Respuesta(200, {"status": "processing", "result": ""})
            return Respuesta(200, {
                "status": "completed",
                "result": "https://media.genaipro.io/audio/tarea-1.mp3",
                "subtitle": "",
            })

    class AudioFalso:
        def mp3_a_pcm(self, contenido):
            return b"\x00\x00" * 20

    anterior_audio, anterior_sueno, anterior_poll = voz._AUDIO, voz.time.sleep, voz.POLL_S
    voz._AUDIO = AudioFalso()
    voz.time.sleep = lambda *_a, **_k: None
    voz.POLL_S = 0
    try:
        pcm, tarea = voz._locutar_trozo(
            Sesion(), "Hola", {"voz_id": "v", "velocidad": "normal"})
    finally:
        voz._AUDIO, voz.time.sleep, voz.POLL_S = anterior_audio, anterior_sueno, anterior_poll
    igual(tarea, "tarea-1", "el sondeo usa el task_id")
    sesion = Sesion()
    voz._AUDIO = AudioFalso()
    voz.time.sleep = lambda *_a, **_k: None
    voz.POLL_S = 0
    try:
        voz._locutar_trozo(sesion, "Hola", {"voz_id": "v", "velocidad": "normal"})
    finally:
        voz._AUDIO, voz.time.sleep, voz.POLL_S = anterior_audio, anterior_sueno, anterior_poll
    url, cuerpo = sesion.posts[0]
    comprobar(url.endswith("/labs/task") and "genaipro.io/api/v1" in url,
              "la tarea se crea en genaipro.io /labs/task")
    igual(cuerpo["stability"], 0.75, "el cuerpo manda stability 0.75")
    igual(cuerpo["similarity"], 0.5, "y similarity 0.5")
    comprobar(pcm, "y se baja el MP3 de result")


def prueba_alineador():
    print("alineador")
    alinear = medios.motor("alineador/alinear.py")
    try:
        import ctc_forced_aligner  # noqa: F401
    except ImportError:
        print("  --   alineador real omitido: no esta ctc-forced-aligner")
        try:
            alinear.alinear(os.path.join(_TMP, "no.wav"), "hola", "es")
            comprobar(False, "sin el paquete tenia que avisar")
        except alinear.AlineadorAusente:
            comprobar(True, "sin el paquete se dice que falta, no se inventan tiempos")
        return
    if not shutil.which("ffmpeg") or not shutil.which("espeak-ng") and not shutil.which("espeak"):
        print("  --   alineador real omitido: falta espeak o ffmpeg")
        return
    voz = shutil.which("espeak-ng") or shutil.which("espeak")
    wav = os.path.join(_TMP, "frase.wav")
    import subprocess
    subprocess.run([voz, "-v", "es", "-w", wav, "Hola mundo"],
                   check=True, capture_output=True)
    marcas = alinear.alinear(wav, "Hola mundo", "es")
    comprobar(marcas and all(set(m) >= {"w", "s", "e"} for m in marcas),
              "el alineador devuelve w, s, e")
    comprobar(marcas[0]["e"] >= marcas[0]["s"], "el fin no va antes del inicio")


def prueba_comprobar():
    print("comprobar claves")
    vistas = []

    def pedir(metodo, url, **kw):
        vistas.append((metodo, url, kw.get("headers") or {}))
        if "uapi/v1/account" in url:
            if (kw.get("headers") or {}).get("x-api-key") == "mala":
                return Respuesta(404, {"detail": {
                    "error_code": "API_KEY_NOT_FOUND",
                    "error_message": "Api key is not found"}}), ""
            return Respuesta(200, {"uuid": "cuenta", "user_credit": {
                "available_credit": 40, "locked_credit": 0}}), ""
        if "genaipro" in url:
            return Respuesta(200, []), ""
        return Respuesta(200, {"data": []}), ""

    original = comprobar_claves._pedir
    comprobar_claves._pedir = pedir
    try:
        ficha = comprobar_claves.probar_snapgen("abc")
        igual(ficha["estado"], "ok", "SnapGen se prueba con /uapi/v1/account")
        comprobar(any("api.snapgen.ai" in u and "/uapi/v1/account" in u
                      for _m, u, _h in vistas), "y esa es la URL")
        comprobar(any(h.get("x-api-key") == "abc" for _m, _u, h in vistas),
                  "la prueba manda x-api-key")
        comprobar("40" in ficha["mensaje"], "y lee los creditos sin generar")
        ficha = comprobar_claves.probar_snapgen("mala")
        igual(ficha["estado"], "mal", "API_KEY_NOT_FOUND no da la clave por buena")
        ficha = comprobar_claves.probar_genaipro("abc")
        igual(ficha["estado"], "ok", "GenAI Pro se prueba con /labs/voices")
        comprobar(any("genaipro.io" in u and "labs/voices" in u for _m, u, _h in vistas),
                  "sin generar audio, contra genaipro.io")
        ficha = comprobar_claves.probar_snapgen("")
        igual(ficha["estado"], "sin_clave", "sin clave no sale a la red")
    finally:
        comprobar_claves._pedir = original


def main():
    prueba_proveedor()
    prueba_empaquetar()
    prueba_snapgen_red()
    prueba_simular()
    prueba_voz()
    prueba_alineador()
    prueba_comprobar()
    print()
    if FALLOS:
        print(f"{len(FALLOS)} fallo(s)")
        for texto in FALLOS:
            print(f"  - {texto}")
        return 1
    print("proveedores en verde")
    return 0


if __name__ == "__main__":
    sys.exit(main())
