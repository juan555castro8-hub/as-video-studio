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
    igual(coste.tarifa_snapgen("2K"), 0.014, "SnapGen 2K a 0,014")
    igual(coste.tarifa_snapgen("1K"), 0.0085, "y 1K a 0,0085")
    comprobar(coste.tarifa_caracter("genaipro") is None,
              "GenAI Pro no tiene precio por caracter: sale sin tarifa")
    tabla = {f["calidad"]: f for f in ajustes.tabla_snapgen()}
    igual(tabla["low"]["resolucion"], "1K", "baja de SnapGen es 1K")
    igual(tabla["medium"]["usd_total"], 0.0085, "media tambien, al mismo precio")
    igual(tabla["high"]["resolucion"], "2K", "alta es 2K")
    igual(tabla["high"]["usd_total"], 0.014, "y sale a 0,014")
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
    recortadas = imagen._recortar(rutas, 16)
    igual(len(recortadas), 16, "mas de 16 se quedan en 16")
    igual(recortadas[-1], rutas[-1], "la continuidad mas reciente se queda")
    comprobar(rutas[-2] not in recortadas, "la continuidad vieja es la primera que sale")
    imagen.TOPE_CUERPO = 30_000
    try:
        usadas, urls, huellas = imagen.empaquetar(rutas[:4])
    finally:
        imagen.TOPE_CUERPO = 9 * 1024 * 1024
    comprobar(all(u.startswith("data:image/jpeg;base64,") for u in urls),
              "las referencias van como data URL JPEG")
    comprobar(sum(len(u) for u in urls) < 30_000, "y el cuerpo cabe en el tope")
    igual(len(usadas), len(urls), "el orden se conserva")
    vacias, urls0, _ = imagen.empaquetar([])
    igual(vacias, [], "sin referencias no se inventa ninguna")


def prueba_snapgen_red():
    print("snapgen")
    imagen = medios.motor("imagen_snapgen/imagen.py")
    imagen._gasto.update(usd=0.0, llamadas=0)
    imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
    imagen._SELLO[0] = imagen._sello_claves()
    llamadas = []
    estado = {"veces": 0}

    def post(url, **kw):
        llamadas.append(("POST", url, kw.get("headers") or {}))
        cuerpo = kw.get("json") or {}
        if cuerpo.get("prompt") == "sin-saldo":
            return Respuesta(402, {"error": {"code": "insufficient_funds",
                                             "message": "Wallet balance is insufficient"}})
        if cuerpo.get("prompt") == "clave-mala":
            return Respuesta(401, {"error": {"code": "invalid_api_key", "message": "bad"}})
        if cuerpo.get("prompt") == "url-mala":
            return Respuesta(400, {"error": {"code": "invalid_image_urls",
                                             "message": "invalid image url"}})
        if cuerpo.get("prompt") == "demasiado":
            return Respuesta(429, {}, {"retry-after": "1"})
        return Respuesta(202, {"id": "task_prueba", "status": "queued"},
                         {"x-gateway-task-id": "task_prueba"})

    def get(url, **kw):
        llamadas.append(("GET", url))
        if url.endswith("/tasks/task_prueba"):
            estado["veces"] += 1
            if estado["veces"] < 2:
                return Respuesta(200, {"id": "task_prueba", "status": "processing",
                                       "progress": 30})
            return Respuesta(200, {"id": "task_prueba", "status": "succeeded",
                                   "result": {"urls": ["https://cdn.ejemplo/img.png"]},
                                   "charged_microusd": "8500"})
        return Respuesta(200, None, contenido=png_de())

    original = (imagen.requests.post, imagen.requests.get, imagen.time.sleep, imagen.POLL_S)
    imagen.requests.post = post
    imagen.requests.get = get
    imagen.time.sleep = lambda *_a, **_k: None
    imagen._esperar = lambda *_a, **_k: None
    imagen.POLL_S = 0
    try:
        png, meta = imagen.generar("un farol", [], quality="medium", tamano="apaisado")
        comprobar(png[:8] == b"\x89PNG\r\n\x1a\n", "el 202 se sondea y vuelve un PNG")
        igual(meta["proveedor"], "snapgen", "la meta dice snapgen")
        igual(meta["tamano"], "16:9", "el apaisado sale 16:9")
        igual(meta["resolucion"], "1K", "medium es 1K")
        igual(meta["coste"], 0.0085, "y se cobra la tarifa de SnapGen, no tokens")
        cabecera = llamadas[0][2]
        comprobar(cabecera.get("Prefer") == "respond-async", "pide respond-async")
        comprobar(len(cabecera.get("Idempotency-Key") or "") == 40, "la clave es un sha1")
        png_h, meta_h = imagen.generar("detalle", [], quality="high", tamano="vertical")
        igual(meta_h["resolucion"], "2K", "high es 2K")
        igual(meta_h["tamano"], "9:16", "vertical es 9:16")
        comprobar(png_h[:4] == b"\x89PNG", "y tambien es PNG")
        imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
        try:
            imagen.generar("sin-saldo", [])
            comprobar(False, "402 tiene que ser SinSaldo")
        except imagen.SinSaldo:
            comprobar(True, "402 es SinSaldo")
        imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
        try:
            imagen.generar("clave-mala", [])
            comprobar(False, "401 tiene que rechazar la clave")
        except RuntimeError as fallo:
            comprobar("401" in str(fallo), "401 dice que la clave no vale")
        imagen._CUENTAS[:] = [imagen._Cuenta("prueba", "clave-snap-prueba")]
        antes = len(llamadas)
        try:
            imagen.generar("url-mala", [])
            comprobar(False, "la url invalida tiene que fallar")
        except RuntimeError as fallo:
            comprobar("URLs publicas" in str(fallo), "y dice que hacen falta URLs publicas")
        igual(len([x for x in llamadas[antes:] if x[0] == "POST"]), 1,
              "un 400 no se reintenta")
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
        vistas.append((metodo, url))
        if "snapgen" in url:
            return Respuesta(200, {"data": []}), ""
        if "genaipro" in url:
            return Respuesta(200, []), ""
        return Respuesta(200, {"data": []}), ""

    original = comprobar_claves._pedir
    comprobar_claves._pedir = pedir
    try:
        ficha = comprobar_claves.probar_snapgen("abc")
        igual(ficha["estado"], "ok", "SnapGen se prueba con /v1/models")
        comprobar(any("/v1/models" in u for _m, u in vistas), "y esa es la URL")
        ficha = comprobar_claves.probar_genaipro("abc")
        igual(ficha["estado"], "ok", "GenAI Pro se prueba con /labs/voices")
        comprobar(any("labs/voices" in u for _m, u in vistas),
                  "sin generar audio")
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
