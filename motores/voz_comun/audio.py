"""Audio compartido por los motores de voz.

PCM s16le mono a 44,1 kHz, que es lo que el resto del estudio ya sabe unir,
espaciar y medir. Cartesia lo entrega asi; GenAI Pro entrega MP3 y se convierte
aqui antes de seguir.
"""
import os
import shutil
import struct
import subprocess

SR = 44100


def wav_desde_pcm(pcm: bytes) -> bytes:
    """Cabecera WAV PCM s16le mono sobre PCM crudo."""
    cabecera = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    cabecera += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SR, SR * 2, 2, 16)
    cabecera += b"data" + struct.pack("<I", len(pcm))
    return cabecera + pcm


def pcm_de_wav(wav: bytes) -> bytes:
    """El PCM de un wav que escribimos nosotros (cabecera de 44 bytes)."""
    return wav[44:] if len(wav) >= 44 else wav


def duracion_pcm(pcm: bytes) -> float:
    return len(pcm) / float(SR * 2)


def ffmpeg():
    puesto = (os.environ.get("ESTUDIO_FFMPEG") or "").strip().strip('"')
    if puesto and os.path.isfile(puesto):
        return puesto
    ruta = shutil.which("ffmpeg")
    if not ruta:
        raise RuntimeError("no se encuentra ffmpeg: hace falta para pasar el "
                           "MP3 de GenAI Pro a PCM")
    return ruta


def mp3_a_pcm(mp3: bytes) -> bytes:
    """MP3 -> PCM s16le mono 44,1 kHz, por la entrada estandar de ffmpeg."""
    if not mp3:
        raise RuntimeError("el MP3 de la voz ha llegado vacio")
    proc = subprocess.run(
        [ffmpeg(), "-v", "error", "-i", "pipe:0", "-f", "s16le",
         "-ac", "1", "-ar", str(SR), "pipe:1"],
        input=mp3, capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        detalle = (proc.stderr or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ffmpeg no ha podido leer el MP3 de la voz: {detalle}")
    pcm = proc.stdout
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return pcm


def relleno_de_sala(pcm: bytes, duracion_s: float) -> bytes:
    """Un trozo del propio audio para no pegar los bloques con silencio digital.

    Se toma la cola (donde suele estar el aire de la frase) y se repite. Si no
    hay cola util, ceros: peor un patron inventado que un hueco corto.
    """
    necesarios = int(duracion_s * SR * 2) & ~1
    if necesarios <= 0:
        return b""
    if len(pcm) < SR:  # menos de medio segundo: no hay sala que copiar
        return b"\x00" * necesarios
    toma = pcm[-min(len(pcm) // 5, SR * 2):]
    if len(toma) < 4:
        return b"\x00" * necesarios
    toma = toma[:len(toma) & ~1]
    piezas = []
    while sum(len(p) for p in piezas) < necesarios:
        piezas.append(toma)
        toma = toma[::-1]
        # el reverso invierte los bytes de cada muestra; se recoloca de dos en dos
        if len(toma) >= 2:
            toma = b"".join(toma[i:i + 2][::-1] for i in range(0, len(toma) - 1, 2))
    return b"".join(piezas)[:necesarios]


def unir(trozos, aire_s=0.04):
    """Une PCM de varios trozos con un poco de aire de sala entre ellos.

    Devuelve (pcm, offsets) donde offsets[i] es el segundo en el que empieza
    el trozo i.
    """
    pcm = b""
    offsets = []
    for indice, trozo in enumerate(trozos):
        if indice and aire_s > 0:
            pcm += relleno_de_sala(pcm, aire_s)
        offsets.append(duracion_pcm(pcm))
        pcm += trozo
    return pcm, offsets
