"""Alineado forzado de un texto conocido contra un wav, en CPU.

GenAI Pro no devuelve el instante de cada palabra, y el montaje lo necesita
(`{"w","s","e"}` en segundos). Esto lo saca en local con un modelo CTC, sin
GPU: la maquina de destino son 8 nucleos y 15 GB.

El modelo por defecto es el MMS de 300M de ctc-forced-aligner, que cubre el
castellano. Se cachea en ESTUDIO_MODELOS (si no, cache/modelos del repo). Es
una dependencia opcional: si no esta instalada, `alinear` levanta
`AlineadorAusente` y el motor de voz reparte el SRT a ojo, avisandolo.

Instalacion (CPU, sin CUDA). El paquete de PyPI con el mismo nombre es otro
proyecto: hay que instalar el de Mahmoud Ashraf desde git.

    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
    pip install -r requirements-alineador.txt

La primera llamada baja el modelo MMS-300M (~1,2 GB) a ESTUDIO_MODELOS.
Ese modelo va con licencia CC-BY-NC: para un uso comercial se cambia
ESTUDIO_ALINEADOR_MODELO por otro modelo CTC compatible.
"""
import os

IDIOMAS = {"es": "spa", "en": "eng", "pt": "por", "fr": "fra", "de": "deu",
           "it": "ita", "ca": "cat"}
MODELO_POR_DEFECTO = "MahmoudAshraf/mms-300m-1130-forced-aligner"

_MODELO = {}


class AlineadorAusente(RuntimeError):
    """No esta instalado el alineador, o no ha podido cargar el modelo."""


def carpeta_modelos():
    puesta = (os.environ.get("ESTUDIO_MODELOS") or "").strip()
    if puesta:
        return puesta
    raiz = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    return os.path.join(raiz, "cache", "modelos")


def _preparar_cache():
    carpeta = carpeta_modelos()
    os.makedirs(carpeta, exist_ok=True)
    os.environ.setdefault("HF_HOME", carpeta)
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", os.path.join(carpeta, "hub"))
    return carpeta


def _idioma(codigo):
    codigo = str(codigo or "es").strip().lower().replace("_", "-")
    corto = codigo.split("-", 1)[0]
    if len(corto) == 3:
        return corto
    return IDIOMAS.get(corto, "spa")


def _modelo():
    clave = (os.environ.get("ESTUDIO_ALINEADOR_MODELO") or MODELO_POR_DEFECTO).strip()
    guardado = _MODELO.get(clave)
    if guardado is not None:
        return guardado
    _preparar_cache()
    try:
        import torch
        from ctc_forced_aligner import load_alignment_model
    except ImportError as fallo:
        raise AlineadorAusente(
            "falta el alineador de voz (ctc-forced-aligner y torch). "
            "Sin el, las marcas salen del subtitulo y son aproximadas. "
            "Se instala con: pip install -r requirements-alineador.txt"
        ) from fallo
    try:
        modelo, tokenizador = load_alignment_model(
            "cpu", clave, None, torch.float32)
    except Exception as fallo:  # noqa: BLE001
        raise AlineadorAusente(
            f"no se ha podido cargar el modelo de alineado ({clave}): {fallo}"
        ) from fallo
    _MODELO[clave] = (modelo, tokenizador)
    return _MODELO[clave]


def _exigir_paquete():
    try:
        import torch  # noqa: F401
        import ctc_forced_aligner  # noqa: F401
    except ImportError as fallo:
        raise AlineadorAusente(
            "falta el alineador de voz (ctc-forced-aligner y torch). "
            "Sin el, las marcas salen del subtitulo y son aproximadas. "
            "Se instala con: pip install torch torchaudio --index-url "
            "https://download.pytorch.org/whl/cpu && "
            "pip install -r requirements-alineador.txt"
        ) from fallo


def alinear(wav_path, texto, idioma="es"):
    """Wav + texto conocido -> [{"w","s","e"}] en segundos.

    `idioma` es el codigo corto del estudio (`es`) o el ISO 639-3 (`spa`).
    """
    texto = " ".join(str(texto or "").split())
    if not texto:
        return []
    _exigir_paquete()
    if not wav_path or not os.path.isfile(wav_path):
        raise FileNotFoundError(f"no esta el audio para alinear: {wav_path}")
    modelo, tokenizador = _modelo()
    try:
        from ctc_forced_aligner import (
            generate_emissions, get_alignments, get_spans, load_audio,
            postprocess_results, preprocess_text)
    except ImportError as fallo:
        raise AlineadorAusente(
            "ctc-forced-aligner no esta instalado") from fallo

    audio = load_audio(wav_path, modelo.dtype, modelo.device)
    emisiones, stride = generate_emissions(modelo, audio, 30, 2, 1)
    tokens, estrellado = preprocess_text(
        texto, False, _idioma(idioma), "word", "edges")
    segmentos, puntuaciones, blanco = get_alignments(
        emisiones, tokens, tokenizador)
    tramos = get_spans(tokens, segmentos, blanco)
    resultados = postprocess_results(estrellado, tramos, stride, puntuaciones, 0.0)
    palabras = []
    for fila in resultados or []:
        dicho = " ".join(str(fila.get("text") or "").split())
        if not dicho or dicho == "*":
            continue
        try:
            ini = float(fila.get("start"))
            fin = float(fila.get("end"))
        except (TypeError, ValueError):
            continue
        if fin < ini:
            fin = ini
        piezas = [p for p in dicho.split() if p]
        if not piezas:
            continue
        ancho = max(0.0, fin - ini)
        pesos = [max(1, len(p)) for p in piezas]
        total = float(sum(pesos))
        cursor = ini
        for pieza, peso in zip(piezas, pesos):
            siguiente = fin if pieza is piezas[-1] else cursor + ancho * peso / total
            palabras.append({"w": pieza, "s": round(cursor, 3),
                             "e": round(max(cursor, siguiente), 3)})
            cursor = siguiente
    return palabras
