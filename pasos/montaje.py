"""El montaje, como lo haria un editor: distinto en cada video, misma marca.

La marca (paleta, tipografia, voz, dibujo) no se toca aqui. Lo que cambia de un
video al siguiente es como se monta: el temperamento, el movimiento de camara,
las transiciones, el gancho y el cierre, y la energia de la musica.

TODO ESTO ES GRATIS. Vive en callouts y en el render, nunca en los params de
assets ni en la semilla que reparte las cartas de plano: esa entra en el prompt
de la imagen, y moverla se paga. La semilla de montaje es otra. Volver a
sortearla deja obsoletos callouts y el render, y nada de lo que cuesta dinero.

La decision es determinista a partir de la semilla guardada. La memoria del
canal solo se consulta al SORTEAR (un video nuevo, o un re-roll): una vez
escrito el plan, rehacer el paso con la misma semilla da el mismo montaje
aunque entretanto se hayan hecho otros videos.
"""
import hashlib
import json
import os
import re
import time

try:
    from . import cartelas, medios, transiciones
except ImportError:  # pasos/ suelto en sys.path
    import cartelas
    import medios
    import transiciones

# ---------------------------------------------------------------------------
# Tope de la camara. El plano se amplia x2 y el video recorta dentro. Pasado
# este cierre ya no quedan pixeles reales (1536*2/1.28/1920 sigue por encima
# de 1, en horizontal y en vertical). El zoom de antes era ~1,05: esto deja
# sitio para un empuje de verdad y para un paneo, sin inventar detalle.
# ---------------------------------------------------------------------------

ESCALA_MIN = 1.0
ESCALA_MAX = 1.28

#: Curvas de recorrido. `suave` es la de siempre (smoothstep): un plan viejo,
#: que no trae curva, se sigue leyendo igual.
CURVAS = ("lineal", "suave", "entrada", "salida", "cine")

MOVIMIENTOS = ("estatico", "deriva", "paneo", "acercar", "alejar", "empuje")

DIRECCIONES = (
    "izquierda", "derecha", "arriba", "abajo",
    "diagonal_ne", "diagonal_no", "diagonal_se", "diagonal_so",
)
_VECTOR = {
    "izquierda": (-1, 0), "derecha": (1, 0), "arriba": (0, -1), "abajo": (0, 1),
    "diagonal_ne": (1, -1), "diagonal_no": (-1, -1),
    "diagonal_se": (1, 1), "diagonal_so": (-1, 1),
}

#: Beats que entiende el montaje. El modelo puede contestar con tilde; se
#: normaliza a esta lista.
BEATS = ("gancho", "contexto", "revelacion", "giro", "dato", "emocion",
         "cierre", "desarrollo")
_BEAT_ALIAS = {
    "revelación": "revelacion", "revelacion": "revelacion",
    "emoción": "emocion", "emocion": "emocion",
    "gancho": "gancho", "contexto": "contexto", "giro": "giro",
    "dato": "dato", "cierre": "cierre", "desarrollo": "desarrollo",
}

# ---------------------------------------------------------------------------
# Temperamentos. Cada uno pesa la camara, la intensidad del viaje, cada
# cuantos cortes cae un acento, que familias de transicion prefiere (aqui
# entran las que el catalogo trae apagadas de fabrica) y la energia de la
# musica. La marca no esta: ni paleta, ni tipo, ni voz.
# ---------------------------------------------------------------------------

TEMPERAMENTOS = {
    "contemplativo": {
        "nombre": "Contemplativo",
        "camara": {"estatico": 5, "deriva": 4, "alejar": 2, "acercar": 1,
                   "paneo": 1, "empuje": 0},
        "viaje": 0.45,
        "acento_cada": 8,
        "familias": {"mezcla": 6, "destello": 1, "iris": 1},
        "energia": 0.22,
        "curva": "cine",
    },
    "sobrio": {
        "nombre": "Sobrio",
        "camara": {"deriva": 3, "acercar": 3, "alejar": 2, "estatico": 2,
                   "paneo": 1, "empuje": 0},
        "viaje": 0.62,
        "acento_cada": 6,
        "familias": {"mezcla": 4, "destello": 2, "optica": 1},
        "energia": 0.45,
        "curva": "suave",
    },
    "dinamico": {
        "nombre": "Dinámico",
        "camara": {"paneo": 4, "empuje": 3, "acercar": 2, "alejar": 1,
                   "deriva": 1, "estatico": 0},
        "viaje": 0.95,
        "acento_cada": 3,
        "familias": {"barrido": 4, "destello": 3, "optica": 2, "digital": 2,
                     "onda": 1, "mezcla": 1},
        "energia": 0.82,
        "curva": "salida",
    },
    "cinematografico": {
        "nombre": "Cinematográfico",
        "camara": {"acercar": 3, "alejar": 3, "deriva": 2, "paneo": 2,
                   "empuje": 2, "estatico": 1},
        "viaje": 0.78,
        "acento_cada": 4,
        "familias": {"optica": 3, "iris": 2, "destello": 2, "disolvencia": 2,
                     "mezcla": 2, "colapso": 1},
        "energia": 0.58,
        "curva": "cine",
    },
}

GANCHOS = {
    "escena": ("Arranca con una escena concreta que se pueda ver: un lugar, "
               "un gesto, un objeto. Nada de presentarte ni de anunciar el tema."),
    "cifra": ("Arranca con una cifra concreta, dicha en la primera frase, y "
              "solo despues explica de que es."),
    "pregunta": ("Arranca con una sola pregunta, la que el video va a "
                 "contestar. Que se pueda responder con lo que viene."),
    "contraste": ("Arranca oponiendo dos cosas que no deberian estar juntas. "
                  "El resto del video explica por que lo estan."),
    "in_medias_res": ("Arranca en mitad de un momento, como si el video ya "
                      "hubiera empezado. El contexto llega en el bloque "
                      "siguiente, no en la primera frase."),
}
CIERRES = {
    "imagen": ("Cierra con una imagen concreta, no con un resumen. La ultima "
               "frase se tiene que poder filmar."),
    "pregunta": ("Cierra dejando una pregunta abierta, distinta de la del "
                 "arranque si lo hubo. No la respondas."),
    "cifra": ("Cierra con un numero que recoja lo que se ha contado. No "
              "repitas la cifra del arranque."),
    "eco": ("Cierra volviendo a la imagen o a la frase del arranque, cambiada "
            "por lo que el video ha contado en medio."),
    "sentencia": ("Cierra con una frase corta y llana, sin moralina y sin "
                  "pedir nada. Una sola oracion."),
}

_NUMERO = re.compile(
    r"\d|%|\b(un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|"
    r"veinte|treinta|cien|ciento|mil|miles|millon|millones)\b",
    re.IGNORECASE)
_EMOCION = re.compile(r"<\s*emotion\b", re.IGNORECASE)
_PAUSA = re.compile(r"<\s*break\b|\.\.\.", re.IGNORECASE)
_ETIQUETA = re.compile(r"<[^>]+>")


def catalogo_temperamentos():
    """Lo que pinta el selector: id y nombre, en el orden de la tabla."""
    return [{"id": k, "nombre": v["nombre"]} for k, v in TEMPERAMENTOS.items()]


def catalogo_aperturas():
    return {
        "ganchos": [{"id": k, "texto": v} for k, v in GANCHOS.items()],
        "cierres": [{"id": k, "texto": v} for k, v in CIERRES.items()],
    }


def normalizar_temperamento(valor):
    """'' si no es uno de la tabla. No inventa un defecto: vacio es sortear."""
    texto = str(valor or "").strip().lower()
    texto = {"cinematográfico": "cinematografico",
             "dinámico": "dinamico"}.get(texto, texto)
    return texto if texto in TEMPERAMENTOS else ""


def normalizar_gancho(valor):
    texto = str(valor or "").strip().lower().replace(" ", "_")
    return texto if texto in GANCHOS else ""


def normalizar_cierre(valor):
    texto = str(valor or "").strip().lower()
    return texto if texto in CIERRES else ""


# ---------------------------------------------------------------- semilla

def semilla_de(params, proyecto_id=""):
    """La semilla de MONTAJE. No es la de las imagenes.

    Si nadie la ha guardado, sale del id del proyecto y no se escribe: abrir
    la pantalla no puede mover una firma. El re-roll si la guarda, y a partir
    de ahi manda el numero guardado.
    """
    cruda = (params or {}).get("semilla_montaje")
    if not isinstance(cruda, bool) and cruda not in (None, ""):
        try:
            return int(cruda) % 10_000_000
        except (TypeError, ValueError):
            pass
    return medios.desempatar(proyecto_id or "video", "montaje") % 10_000_000


def semilla_nueva(anterior, proyecto_id=""):
    """Otra semilla, distinta de la que hay. El reloj solo desempata."""
    base = medios.desempatar(proyecto_id or "video", time.time_ns(),
                             "remonte") % 10_000_000
    try:
        previa = int(anterior)
    except (TypeError, ValueError):
        previa = -1
    if base == previa:
        base = (base + 1) % 10_000_000
    return base


# --------------------------------------------------------------- memoria

def _ruta_memoria():
    """Al lado de los ajustes, y redirigible: las suites no escriben la de verdad."""
    forzada = os.environ.get("ESTUDIO_MEMORIA")
    if forzada:
        return forzada
    ajustes = os.environ.get("ESTUDIO_AJUSTES")
    if ajustes:
        return os.path.join(os.path.dirname(os.path.abspath(ajustes)),
                            "memoria_canal.json")
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(raiz, "memoria_canal.json")


def leer_memoria():
    ruta = _ruta_memoria()
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        return {"videos": []}
    if not isinstance(datos, dict):
        return {"videos": []}
    videos = [v for v in (datos.get("videos") or []) if isinstance(v, dict)]
    return {"videos": videos}


def recordar(proyecto_id, **campos):
    """Apunta este video. Si ya estaba, se actualiza EN SU SITIO.

    No se mueve al final: rehacer un video viejo no puede convertirse en «el
    ultimo del canal» y hacer que el siguiente evite un perfil que ya no es
    el reciente.
    """
    pid = str(proyecto_id or "").strip()
    if not pid:
        return leer_memoria()
    datos = leer_memoria()
    lista = datos["videos"]
    limpio = {k: v for k, v in campos.items()
              if v not in (None, "", [], {})}
    puesto = None
    for ficha in lista:
        if str(ficha.get("id") or "") == pid:
            ficha.update(limpio)
            ficha["cuando"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            puesto = ficha
            break
    if puesto is None:
        lista.append({"id": pid, "cuando": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      **limpio})
    datos["videos"] = lista[-24:]
    ruta = _ruta_memoria()
    os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)
    temporal = ruta + ".parcial"
    with open(temporal, "w", encoding="utf-8") as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=1)
    os.replace(temporal, ruta)
    return datos


def ultimo(excepto=""):
    """El video mas reciente del canal, saltando `excepto` (este mismo)."""
    pid = str(excepto or "")
    for ficha in reversed(leer_memoria()["videos"]):
        if str(ficha.get("id") or "") != pid:
            return ficha
    return {}


def _sortear(opciones, semilla, *clave, vetado=""):
    """Una opcion, ponderada si `opciones` es un dict, si no uniforme.

    `vetado` se salta si queda alguna otra: es como no se repite el perfil
    del video anterior. Con una sola opcion no hay a donde ir.
    """
    if isinstance(opciones, dict):
        bolsa = []
        for nombre, peso in opciones.items():
            try:
                n = int(round(float(peso) * 10))
            except (TypeError, ValueError):
                n = 0
            if n > 0 and nombre != vetado:
                bolsa.extend([nombre] * n)
        if not bolsa:
            bolsa = [nombre for nombre, peso in opciones.items()
                     if float(peso or 0) > 0]
        if not bolsa:
            bolsa = list(opciones)
    else:
        bolsa = [o for o in opciones if o != vetado] or list(opciones)
    if not bolsa:
        return ""
    return bolsa[medios.desempatar(semilla, *clave) % len(bolsa)]


def elegir_temperamento(semilla, forzado="", vetado=""):
    """El perfil de este video. El forzado manda; si no, se sortea sin repetir."""
    puesto = normalizar_temperamento(forzado)
    if puesto:
        return puesto
    return _sortear(list(TEMPERAMENTOS), semilla, "temperamento", vetado=vetado)


def elegir_apertura(semilla, gancho="", cierre="", vetar_gancho="",
                    vetar_cierre=""):
    """Gancho y cierre. Lo que ya este escrito se respeta; el resto se sortea."""
    g = normalizar_gancho(gancho) or _sortear(
        list(GANCHOS), semilla, "gancho", vetado=vetar_gancho)
    c = normalizar_cierre(cierre) or _sortear(
        list(CIERRES), semilla, "cierre", vetado=vetar_cierre)
    # No arrancar y cerrar con la misma formula (pregunta/pregunta, cifra/cifra)
    # si hay otra. El eco si puede volver sobre el arranque: esa es su funcion.
    if g == c and c in CIERRES and len(CIERRES) > 1:
        c = _sortear(list(CIERRES), semilla, "cierre", "distinto", vetado=g)
    # El desempate de arriba veta el arranque, no el cierre reciente: sin
    # esta segunda pasada se puede volver a caer en el del video anterior.
    if vetar_cierre and c == vetar_cierre:
        otros = [x for x in CIERRES if x not in (g, vetar_cierre)]
        if otros:
            c = _sortear(otros, semilla, "cierre", "memoria")
    return g, c


def resolver_apertura(semilla, params=None, anterior=None, proyecto_id=""):
    """Gancho y cierre de un brief, sin moverlos si ya estaban elegidos.

    Lo escrito en los params manda. Si el brief anterior ya los trae, se
    quedan: rehacer el paso no puede sortear otro arranque. Solo se consulta
    la memoria del canal cuando de verdad se esta eligiendo.
    """
    params = params or {}
    anterior = anterior or {}
    g_param = normalizar_gancho(params.get("gancho"))
    c_param = normalizar_cierre(params.get("cierre"))
    g_ant = normalizar_gancho(anterior.get("gancho"))
    c_ant = normalizar_cierre(anterior.get("cierre"))
    if g_param and c_param:
        return g_param, c_param
    if g_ant and c_ant and not g_param and not c_param:
        return g_ant, c_ant
    memoria = ultimo(proyecto_id)
    return elegir_apertura(
        semilla,
        gancho=g_param or g_ant,
        cierre=c_param or c_ant,
        vetar_gancho="" if (g_param or g_ant) else memoria.get("gancho"),
        vetar_cierre="" if (c_param or c_ant) else memoria.get("cierre"))


def texto_para_guion(brief, propio=False):
    """La seccion que se le anade al redactor. Vacia si el guion es suyo.

    Con guion propio el gancho y el cierre siguen existiendo, pero solo para
    el montaje del arranque y del final: el texto no se reescribe.
    """
    if propio:
        return ""
    gancho = normalizar_gancho((brief or {}).get("gancho"))
    cierre = normalizar_cierre((brief or {}).get("cierre"))
    if not gancho and not cierre:
        return ""
    lineas = ["== COMO EMPIEZA Y COMO ACABA ESTE VIDEO =="]
    lineas.append(
        "Esto es de ESTE video, no una formula del canal. No la cambies por "
        "la que usarias por costumbre.")
    if gancho:
        lineas.append(f"Arranque ({gancho}): {GANCHOS[gancho]}")
    if cierre:
        lineas.append(f"Cierre ({cierre}): {CIERRES[cierre]}")
    lineas.append(
        "El primer bloque hace el arranque y el ultimo hace el cierre. "
        "Los de en medio no repiten ni lo uno ni lo otro.")
    return "\n".join(lineas)


# ------------------------------------------------------------- el ritmo

def _hablado(texto):
    return " ".join(_ETIQUETA.sub(" ", str(texto or "")).split())


def _senales_bloque(bloque, indice, total):
    """Lo que se puede leer del texto sin llamar a nadie."""
    texto = str((bloque or {}).get("texto") or "")
    dicho = _hablado(texto)
    return {
        "id": str((bloque or {}).get("id") or ""),
        "primero": indice == 0,
        "ultimo": indice == total - 1,
        "abre": bool((bloque or {}).get("abre_seccion")),
        "pregunta": "?" in dicho,
        "numero": bool(_NUMERO.search(dicho)),
        "emocion": bool(_EMOCION.search(texto)),
        "pausa": bool(_PAUSA.search(texto)),
    }


def etiquetas_por_reglas(bloques):
    """Beat e intensidad de cada bloque, sin modelo. Es el respaldo."""
    bloques = [b for b in (bloques or []) if isinstance(b, dict)]
    total = len(bloques)
    salida = {}
    for indice, bloque in enumerate(bloques):
        s = _senales_bloque(bloque, indice, total)
        if s["primero"]:
            beat = "gancho"
        elif s["ultimo"]:
            beat = "cierre"
        elif s["emocion"] or s["pausa"]:
            beat = "emocion"
        elif s["abre"]:
            beat = "giro"
        elif s["numero"] and s["pregunta"]:
            beat = "revelacion"
        elif s["numero"]:
            beat = "dato"
        elif s["pregunta"]:
            beat = "revelacion"
        elif indice < 2:
            beat = "contexto"
        else:
            beat = "desarrollo"
        intensidad = {
            "gancho": 4, "revelacion": 5, "dato": 4, "giro": 4,
            "desarrollo": 3, "contexto": 2, "emocion": 2, "cierre": 2,
        }[beat]
        if s["pausa"] or s["emocion"]:
            intensidad = min(intensidad, 2)
        if s["primero"] and s["pregunta"]:
            intensidad = max(intensidad, 4)
        salida[s["id"] or f"#{indice}"] = {
            "beat": beat, "intensidad": intensidad, "origen": "reglas",
        }
    return salida


def _simular():
    return str(os.environ.get("ESTUDIO_SIMULAR", "")).strip().lower() in (
        "1", "true", "si", "sí")


def _huella_bloques(bloques):
    trozos = []
    for bloque in bloques or []:
        if not isinstance(bloque, dict):
            continue
        trozos.append(str(bloque.get("id") or ""))
        trozos.append(str(bloque.get("texto") or ""))
        trozos.append("1" if bloque.get("abre_seccion") else "0")
    return hashlib.sha256("\n".join(trozos).encode("utf-8")).hexdigest()[:16]


def _parsear_ritmo(texto, ids):
    """El JSON del modelo, o None si no se puede usar."""
    crudo = str(texto or "")
    inicio, fin = crudo.find("{"), crudo.rfind("}")
    if inicio < 0 or fin <= inicio:
        return None
    try:
        datos = json.loads(crudo[inicio:fin + 1])
    except ValueError:
        return None
    lista = datos.get("bloques") if isinstance(datos, dict) else None
    if not isinstance(lista, list):
        return None
    salida = {}
    for ficha in lista:
        if not isinstance(ficha, dict):
            continue
        bid = str(ficha.get("id") or "")
        beat = _BEAT_ALIAS.get(str(ficha.get("beat") or "").strip().lower())
        if not bid or not beat:
            continue
        try:
            intensidad = int(ficha.get("intensidad"))
        except (TypeError, ValueError):
            intensidad = 3
        salida[bid] = {"beat": beat,
                       "intensidad": max(1, min(5, intensidad)),
                       "origen": "modelo"}
    if not ids:
        return salida or None
    reconocidos = sum(1 for i in ids if i in salida)
    if reconocidos < max(1, (len(ids) + 1) // 2):
        return None
    return salida


def etiquetar_bloques(bloques, cache_ruta="", sin_modelo=False):
    """Beat e intensidad por bloque. Una llamada barata; si falla, las reglas.

    La cache viaja con la version de callouts (el mismo guion no se vuelve a
    pagar). `sin_modelo` es el re-roll: cero llamadas, cache o reglas.
    """
    bloques = [b for b in (bloques or []) if isinstance(b, dict)]
    reglas = etiquetas_por_reglas(bloques)
    if not bloques:
        return reglas, "reglas"
    huella = _huella_bloques(bloques)
    ids = [str(b.get("id") or "") for b in bloques]
    guardado = None
    if cache_ruta and os.path.exists(cache_ruta):
        try:
            with open(cache_ruta, "r", encoding="utf-8") as fh:
                guardado = json.load(fh)
        except (OSError, ValueError):
            guardado = None
    if isinstance(guardado, dict) and guardado.get("huella") == huella:
        previo = guardado.get("bloques") or {}
        # La cache vale igual si la hizo el modelo o las reglas: un CLI caido
        # no se vuelve a llamar en cada montaje hasta que el guion cambie.
        if previo:
            return previo, guardado.get("origen") or "cache"

    if sin_modelo or _simular():
        return reglas, "reglas"

    try:
        from . import cli_claude
    except ImportError:
        import cli_claude
    partes = []
    for bloque in bloques:
        texto = _hablado(bloque.get("texto") or "")
        if len(texto) > 420:
            texto = texto[:417] + "..."
        marca = " abre_seccion" if bloque.get("abre_seccion") else ""
        partes.append(f"{bloque.get('id') or '?'}{marca}: {texto}")
    encargo = (
        "Etiqueta cada bloque de este guion para montar el video. "
        "Devuelve SOLO un JSON con esta forma, sin mas texto: "
        '{"bloques": [{"id": "B001", "beat": "gancho", "intensidad": 4}]}\n'
        "beat es uno de: gancho, contexto, revelacion, giro, dato, emocion, "
        "cierre, desarrollo.\n"
        "intensidad es un entero de 1 (calma, se sostiene) a 5 (golpe).\n"
        "El primer bloque es el gancho y el ultimo el cierre, salvo que el "
        "texto diga otra cosa con claridad. Una seccion nueva (abre_seccion) "
        "es giro o contexto. Un numero o una pregunta que destapa algo es "
        "dato o revelacion. Una emocion o una pausa baja la intensidad.\n\n"
        "BLOQUES:\n" + "\n".join(partes)
    )
    try:
        # Sin el ejecutable no hay llamada: localizar falla al momento y no
        # deja una salud de cuenta apuntada por un CLI que no esta.
        cli_claude.localizar("el ritmo del montaje")
        # haiku/low A PROPOSITO, no el defecto del canal: esto es una etiqueta
        # por bloque, y el defecto (opus) se iria a minutos para decir cuatro
        # palabras. Es la misma razon que el asistente.
        texto, _sobre = cli_claude.ejecutar(
            encargo, modelo="haiku", esfuerzo="low",
            tiempo_max_s=40, base_tiempo_s=20,
            para="el ritmo del montaje")
    except Exception:
        _guardar_ritmo(cache_ruta, huella, reglas, "reglas")
        return reglas, "reglas"
    parsed = _parsear_ritmo(texto, [i for i in ids if i])
    if not parsed:
        _guardar_ritmo(cache_ruta, huella, reglas, "reglas")
        return reglas, "reglas"
    # Lo que el modelo no haya devuelto se rellena con la regla, no se inventa.
    for indice, bloque in enumerate(bloques):
        bid = str(bloque.get("id") or "") or f"#{indice}"
        if bid not in parsed:
            parsed[bid] = reglas.get(bid) or reglas.get(f"#{indice}") or {
                "beat": "desarrollo", "intensidad": 3, "origen": "reglas"}
    _guardar_ritmo(cache_ruta, huella, parsed, "modelo")
    return parsed, "modelo"


def bloque_ok(bloque):
    return isinstance(bloque, dict)


def _guardar_ritmo(ruta, huella, bloques, origen):
    if not ruta:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump({"huella": huella, "origen": origen, "bloques": bloques},
                      fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


# --------------------------------------------------------------- la camara

def _centro(x, y, escala):
    """Un centro cuyo recorte 1/escala cabe entero en el cuadro."""
    escala = max(ESCALA_MIN, min(ESCALA_MAX, float(escala)))
    medio = 0.5 / escala
    lo, hi = medio, 1.0 - medio
    return [round(min(max(x, lo), hi), 4), round(min(max(y, lo), hi), 4)]


def _escala(base, techo, viaje, intensidad):
    """Entre `base` y `techo`, segun lo que viaje el perfil y la intensidad."""
    t = 0.35 + 0.65 * ((max(1, min(5, int(intensidad))) - 1) / 4.0)
    t *= max(0.2, min(1.0, float(viaje)))
    return round(min(ESCALA_MAX, max(ESCALA_MIN, base + (techo - base) * t)), 4)


def construir_zoom(movimiento, intensidad, viaje, semilla, sid, direccion="",
                   curva=""):
    """El zoom que entiende el motor, con dos centros si la camara se desplaza.

    `de`/`a` son escalas (1 = cuadro entero). El centro de llegada va en
    `centro_fin`: sin eso un paneo seria un zoom quieto, que es lo que habia.
    """
    movimiento = movimiento if movimiento in MOVIMIENTOS else "deriva"
    intensidad = max(1, min(5, int(intensidad or 3)))
    if not direccion:
        direccion = _sortear(DIRECCIONES, semilla, sid, "direccion")
    vx, vy = _VECTOR.get(direccion, (1, 0))
    # normalizar la diagonal para que no viaje mas que un eje
    if vx and vy:
        vx, vy = vx * 0.7, vy * 0.7
    curva = curva if curva in CURVAS else {
        "estatico": "suave", "deriva": "cine", "paneo": "suave",
        "acercar": "salida", "empuje": "entrada", "alejar": "salida",
    }[movimiento]

    if movimiento == "estatico":
        de = a = ESCALA_MIN
        c0 = c1 = [0.5, 0.5]
    elif movimiento == "deriva":
        de = _escala(1.02, 1.06, viaje, intensidad)
        a = _escala(1.05, 1.10, viaje, max(intensidad, 2))
        margen = (1.0 - 1.0 / max(de, a)) / 2.0
        paso = margen * 0.55
        c0 = _centro(0.5 - vx * paso, 0.5 - vy * paso, de)
        c1 = _centro(0.5 + vx * paso, 0.5 + vy * paso, a)
    elif movimiento == "paneo":
        escala = _escala(1.10, 1.22, viaje, intensidad)
        de = a = escala
        margen = (1.0 - 1.0 / escala) / 2.0
        paso = margen * 0.92
        c0 = _centro(0.5 - vx * paso, 0.5 - vy * paso, escala)
        c1 = _centro(0.5 + vx * paso, 0.5 + vy * paso, escala)
    elif movimiento == "alejar":
        a = ESCALA_MIN
        de = _escala(1.08, 1.20, viaje, intensidad)
        c0 = _centro(0.5 + vx * 0.02, 0.5 + vy * 0.02, de)
        c1 = [0.5, 0.5]
    elif movimiento == "empuje":
        de = ESCALA_MIN
        a = _escala(1.14, ESCALA_MAX, viaje, intensidad)
        c1 = _centro(0.5 + vx * 0.03, 0.5 + vy * 0.03, a)
        c0 = [0.5, 0.5]
    else:  # acercar
        de = ESCALA_MIN
        a = _escala(1.08, 1.18, viaje, intensidad)
        c1 = _centro(0.5 + vx * 0.02, 0.5 + vy * 0.02, a)
        c0 = [0.5, 0.5]

    return {
        "tipo": ("out" if a < de else "hold" if abs(a - de) < 0.001 and c0 == c1
                 else "in"),
        "de": de, "a": a,
        "centro": c0, "centro_fin": c1,
        "ancla": None,
        "curva": curva,
        "movimiento": movimiento,
        "direccion": direccion if movimiento in ("deriva", "paneo", "empuje",
                                                 "acercar", "alejar") else "",
    }


def _pesos_camara(perfil, senales):
    """Los pesos del temperamento, empujados por lo que dice el plano."""
    pesos = {k: float(v) for k, v in (perfil.get("camara") or {}).items()}
    beat = senales.get("beat") or ""
    intensidad = int(senales.get("intensidad") or 3)
    if beat in ("revelacion", "dato") or senales.get("numero") or senales.get("pregunta"):
        pesos["acercar"] = pesos.get("acercar", 0) + 4
        pesos["empuje"] = pesos.get("empuje", 0) + (3 if intensidad >= 4 else 1)
    if beat == "contexto" or senales.get("abre"):
        pesos["alejar"] = pesos.get("alejar", 0) + 4
    if beat in ("emocion", "cierre") or intensidad <= 2 or senales.get("pausa"):
        pesos["estatico"] = pesos.get("estatico", 0) + 4
        pesos["deriva"] = pesos.get("deriva", 0) + 3
    if beat == "gancho":
        pesos["empuje"] = pesos.get("empuje", 0) + 2
        pesos["paneo"] = pesos.get("paneo", 0) + 2
    if beat == "giro":
        pesos["paneo"] = pesos.get("paneo", 0) + 2
        pesos["empuje"] = pesos.get("empuje", 0) + 1
    # Una cartela SOBRE imagen sigue siendo un plano: la camara se mueve.
    # La de fondo negro ya se queda quieta mas arriba, en `planear`.
    if senales.get("sobre"):
        pesos["estatico"] = 0
    return pesos


def _movimiento_de_apertura(gancho, cierre, es_primero, es_ultimo, perfil):
    """El primer y el ultimo plano no se sortean del todo: cuentan el gancho."""
    if es_primero:
        return {
            "cifra": "empuje", "pregunta": "acercar", "contraste": "paneo",
            "escena": "deriva", "in_medias_res": "paneo",
        }.get(gancho) or "acercar"
    if es_ultimo:
        return {
            "imagen": "estatico", "pregunta": "alejar", "cifra": "acercar",
            "eco": "deriva", "sentencia": "estatico",
        }.get(cierre) or "estatico"
    return ""


# ---------------------------------------------------------- transiciones

def _bolsa_transiciones(perfil, ranura, pedidas):
    """Que transiciones pueden ocupar esta ranura, con el peso del perfil.

    Si el video tiene una lista elegida a mano, se respeta: no se cuela una
    que alguien apago. Si no la tiene, el temperamento puede usar las que el
    catalogo trae apagadas (glitch, remolino, quemado...), que es de donde
    sale que dos videos no compartan el mismo juego de cortes.
    """
    familias = perfil.get("familias") or {}
    if pedidas:
        candidatas = [t for t in pedidas if t in transiciones.CATALOGO and t != "corte"]
    else:
        candidatas = []
        for nombre, ficha in transiciones.CATALOGO.items():
            if nombre == "corte":
                continue
            if float(familias.get(ficha["familia"]) or 0) <= 0:
                continue
            candidatas.append(nombre)
    if not candidatas:
        candidatas = [t for t in transiciones.POR_DEFECTO if t != "corte"]
    pesos = {}
    for nombre in candidatas:
        ficha = transiciones.CATALOGO[nombre]
        fuerza = int(ficha["fuerza"])
        if ranura == "acento" and fuerza < 2:
            continue
        if ranura != "acento" and not (1 <= fuerza <= 2):
            continue
        pesos[nombre] = float(familias.get(ficha["familia"]) or 1)
    if not pesos:
        pesos = {"fundido": 1}
    return pesos


def _quiere_acento(indice, senales, perfil, anterior_fue):
    """Un acento en el cambio de seccion, en el giro, o cada N cortes fuertes."""
    if indice == 0:
        return False
    if anterior_fue and not senales.get("abre"):
        return False
    if senales.get("abre") or senales.get("beat") == "giro":
        return True
    if int(senales.get("intensidad") or 3) >= 5 and senales.get("beat") in (
            "revelacion", "dato"):
        return True
    cada = max(2, int(perfil.get("acento_cada") or 5))
    return senales.get("corte") == "fuerte" and indice % cada == 0


def _duracion_de(nombre, plano_s, base):
    ficha = transiciones.CATALOGO.get(nombre) or {}
    duracion = float(base) * float(ficha.get("factor") or 1)
    if plano_s > 0:
        duracion = min(duracion, plano_s * transiciones.FRACCION_MAXIMA)
    return round(max(0.0, duracion), 3)


# ---------------------------------------------------------------- el plan

def _senales_plano(escena, indice, total, etiquetas, bloques_por_id):
    bid = str((escena.get("origen") or {}).get("bloque") or "")
    etiqueta = (etiquetas or {}).get(bid) or {}
    bloque = (bloques_por_id or {}).get(bid) or {}
    texto = escena.get("narracion") or bloque.get("texto") or ""
    dicho = _hablado(texto)
    beat = etiqueta.get("beat") or ""
    try:
        intensidad = int(etiqueta.get("intensidad") or 0)
    except (TypeError, ValueError):
        intensidad = 0
    if not beat:
        # sin etiqueta de bloque, se lee el propio plano
        if indice == 0:
            beat, intensidad = "gancho", 4
        elif indice == total - 1:
            beat, intensidad = "cierre", 2
        elif "?" in dicho and _NUMERO.search(dicho):
            beat, intensidad = "revelacion", 5
        elif _NUMERO.search(dicho):
            beat, intensidad = "dato", 4
        elif "?" in dicho:
            beat, intensidad = "revelacion", 4
        else:
            beat, intensidad = "desarrollo", 3
    return {
        "beat": beat,
        "intensidad": max(1, min(5, intensidad or 3)),
        "abre": bool(bloque.get("abre_seccion")) and int(
            (escena.get("origen") or {}).get("indice") or 0) == 0,
        "pregunta": "?" in dicho,
        "numero": bool(_NUMERO.search(dicho)),
        "emocion": bool(_EMOCION.search(str(texto))),
        "pausa": bool(_PAUSA.search(str(texto))) or escena.get("corte") == "fuerte",
        "corte": escena.get("corte") or "",
        "capitulo": bool(escena.get("capitulo")),
    }


def planear(escenas, semilla, temperamento, etiquetas=None, gancho="",
            cierre="", transiciones_pedidas=None, duracion_base=0.4,
            bloques=None):
    """El plan de montaje de estas escenas. Puro: mismos datos, mismo plan."""
    perfil_id = elegir_temperamento(semilla, temperamento, vetado="")
    # el vetado de perfil se aplica ANTES, en `decidir`: aqui el nombre ya esta
    perfil = TEMPERAMENTOS[perfil_id]
    bloques_por_id = {}
    for bloque in bloques or []:
        if isinstance(bloque, dict) and bloque.get("id"):
            bloques_por_id[str(bloque["id"])] = bloque
    pedidas = [str(t) for t in (transiciones_pedidas or []) if t]
    base = float(duracion_base or 0.4)
    planos = {}
    familias = []
    anterior_mov = ""
    anterior_dir = ""
    anterior_acento = False
    cuenta_mov = {}
    cuenta_trans = {}
    escenas = list(escenas or [])
    total = len(escenas)
    for indice, escena in enumerate(escenas):
        sid = str(escena.get("id") or "")
        if not sid:
            continue
        senales = _senales_plano(escena, indice, total, etiquetas, bloques_por_id)
        if senales["capitulo"]:
            senales["abre"] = True
        try:
            senales["sobre"] = bool(cartelas.sobre_imagen(escena))
        except Exception:
            senales["sobre"] = False
        sigue = bool(escena.get("sigue_a")) or (
            bool((escena.get("cartela") or {}).get("sigue_a"))
            if isinstance(escena.get("cartela"), dict) else False)
        cartela_seca = False
        try:
            cartela_seca = bool(cartelas.sin_imagen(escena))
        except Exception:
            cartela_seca = False

        if cartela_seca or indice == 0 and False:
            movimiento = "estatico"
        elif sigue and anterior_mov:
            movimiento = anterior_mov
        else:
            fijo = _movimiento_de_apertura(
                gancho, cierre, indice == 0, indice == total - 1, perfil)
            if fijo:
                movimiento = fijo
            elif cartela_seca:
                movimiento = "estatico"
            else:
                movimiento = _sortear(
                    _pesos_camara(perfil, senales), semilla, sid, "camara",
                    vetado=anterior_mov)
        if cartela_seca:
            movimiento = "estatico"
        direccion = ""
        if movimiento in ("paneo", "deriva"):
            direccion = _sortear(DIRECCIONES, semilla, sid, "direccion",
                                 vetado=anterior_dir)
        zoom = construir_zoom(movimiento, senales["intensidad"], perfil["viaje"],
                              semilla, sid, direccion, perfil.get("curva") or "")
        # Un plano que continua a otro no encadena: no hay corte.
        if indice == 0 or sigue:
            ranura, nombre, duracion = "corte", "corte", 0.0
        else:
            acento = _quiere_acento(indice, senales, perfil, anterior_acento)
            ranura = "acento" if acento else "suave"
            pesos = _bolsa_transiciones(perfil, ranura, pedidas)
            # no repetir familia en la ventana que ya usa el catalogo
            recientes = set(familias[-transiciones.VENTANA_FAMILIA:])
            nombre = ""
            for _ in range(6):
                candidato = _sortear(pesos, semilla, sid, "transicion", ranura, _)
                familia = (transiciones.CATALOGO.get(candidato) or {}).get("familia")
                if familia not in recientes or len(pesos) == 1:
                    nombre = candidato
                    break
            nombre = nombre or "fundido"
            plano_s = float(escena.get("duracion") or (
                float(escena.get("t_out") or 0) - float(escena.get("t_in") or 0)))
            duracion = _duracion_de(nombre, plano_s, base)
            familias.append((transiciones.CATALOGO.get(nombre) or {}).get("familia"))
            cuenta_trans[nombre] = cuenta_trans.get(nombre, 0) + 1
        anterior_acento = ranura == "acento"
        anterior_mov = movimiento
        anterior_dir = direccion or anterior_dir
        cuenta_mov[movimiento] = cuenta_mov.get(movimiento, 0) + 1
        planos[sid] = {
            "movimiento": movimiento,
            "direccion": zoom.get("direccion") or "",
            "curva": zoom["curva"],
            "zoom": {k: zoom[k] for k in
                     ("tipo", "de", "a", "centro", "centro_fin", "ancla",
                      "curva", "movimiento")},
            "ranura": ranura,
            "transicion": nombre,
            "duracion": duracion,
            "beat": senales["beat"],
            "intensidad": senales["intensidad"],
        }
    dominante = ""
    if cuenta_mov:
        dominante = sorted(cuenta_mov, key=lambda k: (-cuenta_mov[k], k))[0]
    energia = float(perfil["energia"])
    # la intensidad media del guion empuja la musica, sin salirse del perfil
    if planos:
        media = sum(p["intensidad"] for p in planos.values()) / len(planos)
        energia = max(0.0, min(1.0, energia + (media - 3) * 0.06))
    return {
        "semilla": int(semilla),
        "temperamento": perfil_id,
        "temperamento_nombre": perfil["nombre"],
        "forzado": bool(normalizar_temperamento(temperamento)),
        "gancho": normalizar_gancho(gancho),
        "cierre": normalizar_cierre(cierre),
        "dominante": dominante,
        "mezcla": cuenta_trans,
        "energia": round(energia, 3),
        "musica_db": round(-4.5 + 7.5 * energia, 2),
        "planos": planos,
    }


def cabe(zoom):
    """Si este zoom se queda dentro del cuadro y del tope de pixeles."""
    if not isinstance(zoom, dict):
        return False
    for extremo in (zoom.get("de"), zoom.get("a")):
        try:
            escala = float(extremo)
        except (TypeError, ValueError):
            return False
        if escala < ESCALA_MIN - 1e-6 or escala > ESCALA_MAX + 1e-6:
            return False
    for clave, escala in (("centro", zoom.get("de")), ("centro_fin", zoom.get("a"))):
        centro = zoom.get(clave) or [0.5, 0.5]
        if not isinstance(centro, (list, tuple)) or len(centro) < 2:
            return False
        escala = float(escala or 1)
        medio = 0.5 / escala
        for eje in centro[:2]:
            if float(eje) < medio - 1e-3 or float(eje) > 1.0 - medio + 1e-3:
                return False
    return True


def cortes_de(escenas, params_render, semilla_plan=0, documento=None):
    """Lo que el render encadena. Sin plan de montaje, el reparto de siempre.

    Asi un video ya montado, que no tiene `montaje.json`, sigue saliendo igual.
    """
    planos = (documento or {}).get("planos") or {}
    if not planos:
        return transiciones.resolver(escenas, params_render, semilla=semilla_plan)
    salida = {}
    for escena in escenas or []:
        sid = escena.get("id")
        if not sid:
            continue
        ficha = planos.get(sid) or {}
        nombre = str(ficha.get("transicion") or "corte")
        if nombre not in transiciones.CATALOGO or nombre == "corte":
            salida[sid] = {"tipo": "corte", "shader": None, "duracion": 0.0,
                           "ranura": "corte"}
            continue
        salida[sid] = {
            "tipo": nombre,
            "shader": transiciones.frag_de(nombre),
            "duracion": float(ficha.get("duracion") or 0.0),
            "ranura": ficha.get("ranura") or "suave",
        }
    return salida


def resumen_publico(plan):
    """Lo que cabe en una ficha, sin el plano a plano (eso vive en el fichero)."""
    if not plan:
        return {}
    return {
        "semilla": plan.get("semilla"),
        "temperamento": plan.get("temperamento"),
        "temperamento_nombre": plan.get("temperamento_nombre"),
        "forzado": bool(plan.get("forzado")),
        "gancho": plan.get("gancho") or "",
        "cierre": plan.get("cierre") or "",
        "dominante": plan.get("dominante") or "",
        "mezcla": plan.get("mezcla") or {},
        "energia": plan.get("energia"),
        "musica_db": plan.get("musica_db"),
        "origen_ritmo": plan.get("origen_ritmo") or "",
        "planos": len(plan.get("planos") or {}),
    }


# ------------------------------------------------------- desde un proyecto

def _bloques_de(proyecto):
    try:
        from . import comun
    except ImportError:
        import comun
    datos = comun.leer_salida(proyecto, "guion", "guion.json", obligatorio=False) or {}
    lista = datos.get("guion") or datos.get("bloques") or []
    return [b for b in lista if isinstance(b, dict)]


def _brief_de(proyecto):
    try:
        from . import comun
    except ImportError:
        import comun
    return comun.leer_salida(proyecto, "brief", "brief.json", obligatorio=False) or {}


def _transiciones_pedidas(proyecto):
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if raiz not in __import__("sys").path:
        __import__("sys").path.insert(0, raiz)
    try:
        from nucleo.estado import Estado
        params = Estado(proyecto).params("render") or {}
    except Exception:
        return []
    return list(params.get("transiciones") or [])


def _duracion_base(proyecto):
    try:
        from nucleo.estado import Estado
        params = Estado(proyecto).params("render") or {}
        return float(params.get("duracion_transicion") or 0.4)
    except Exception:
        return 0.4


def decidir(proyecto, escenas, params, cache_dir="", sin_modelo=False,
            previo=None):
    """El plan de este video, reutilizando el ya sorteado si la semilla no cambio."""
    pid = str(getattr(proyecto, "id", "") or "")
    semilla = semilla_de(params, pid)
    forzado = normalizar_temperamento((params or {}).get("temperamento"))
    previo = previo if isinstance(previo, dict) else {}
    misma = (int(previo.get("semilla") or -1) == int(semilla)
             and (not forzado or previo.get("temperamento") == forzado))
    bloques = _bloques_de(proyecto)
    etiquetas, origen = etiquetar_bloques(
        bloques, os.path.join(cache_dir, "ritmo.json") if cache_dir else "",
        sin_modelo=sin_modelo or misma)
    brief = _brief_de(proyecto)
    # Lo escrito en el brief manda (se eligio al redactar). Si no hay, se
    # sortea aqui para que el montaje del arranque tenga de que tirar igual.
    memoria = ultimo(pid)
    gancho, cierre = elegir_apertura(
        semilla,
        gancho=(brief or {}).get("gancho") or "",
        cierre=(brief or {}).get("cierre") or "",
        vetar_gancho="" if (brief or {}).get("gancho") else memoria.get("gancho"),
        vetar_cierre="" if (brief or {}).get("cierre") else memoria.get("cierre"))
    if misma and previo.get("temperamento") in TEMPERAMENTOS:
        temperamento = previo["temperamento"]
        # el vetado ya se aplico cuando se sorteo: no se vuelve a mirar
        vetado = ""
    else:
        vetado = "" if forzado else str(memoria.get("temperamento") or "")
        temperamento = elegir_temperamento(semilla, forzado, vetado=vetado)
    plan = planear(
        escenas, semilla, temperamento if not forzado else forzado,
        etiquetas=etiquetas, gancho=gancho, cierre=cierre,
        transiciones_pedidas=_transiciones_pedidas(proyecto),
        duracion_base=_duracion_base(proyecto), bloques=bloques)
    # `planear` vuelve a sortear el perfil si no ve un forzado. Se lo hemos
    # pasado ya elegido, asi que el nombre es el de arriba.
    plan["temperamento"] = temperamento
    plan["temperamento_nombre"] = TEMPERAMENTOS[temperamento]["nombre"]
    plan["forzado"] = bool(forzado)
    plan["origen_ritmo"] = origen
    # La energia es la del perfil ELEGIDO, no la de un sorteo intermedio.
    perfil = TEMPERAMENTOS[temperamento]
    if plan.get("planos"):
        media = sum(p["intensidad"] for p in plan["planos"].values()) / len(plan["planos"])
        energia = max(0.0, min(1.0, float(perfil["energia"]) + (media - 3) * 0.06))
    else:
        energia = float(perfil["energia"])
    plan["energia"] = round(energia, 3)
    plan["musica_db"] = round(-4.5 + 7.5 * energia, 2)
    # Rehacer el plan con la misma semilla no cambia la curva de camara, pero
    # `planear` si ha vuelto a sortear movimientos. Si la semilla no cambio,
    # se conservan los planos ya pagados en tiempo de render (son gratis, pero
    # tienen que ser los mismos).
    if misma and previo.get("planos"):
        plan["planos"] = previo["planos"]
        plan["dominante"] = previo.get("dominante") or plan["dominante"]
        plan["mezcla"] = previo.get("mezcla") or plan["mezcla"]
        plan["gancho"] = previo.get("gancho") or plan["gancho"]
        plan["cierre"] = previo.get("cierre") or plan["cierre"]
    return plan


def abrir_previo(ruta_dir):
    """El montaje que dejo la version que se esta rehaciendo, si esta."""
    if not ruta_dir:
        return {}
    ruta = os.path.join(ruta_dir, "montaje.json")
    if not os.path.exists(ruta):
        return {}
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def quiere_subtitulos(params):
    """Si este video lleva subtitulos.

    La clave en los params del proyecto manda (y es la que entra en la firma).
    Si no esta, manda el ajuste global. Ausente en los dos sitios = si, que es
    lo que hacian todos los videos de antes.
    """
    if isinstance(params, dict) and "subtitulos" in params:
        return bool(params.get("subtitulos"))
    try:
        from . import ajustes
    except ImportError:
        import ajustes
    return bool(ajustes.leer().get("subtitulos", True))
