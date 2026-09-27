"""El montaje, distinto en cada video y sin tocar lo que se paga.

No llama a ningun modelo: ESTUDIO_SIMULAR deja el ritmo en las reglas. La
memoria del canal se redirige a un temporal para no escribir la de verdad.

    python pasos/prueba_montaje.py
"""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="montaje_prueba_")
os.environ["ESTUDIO_SIMULAR"] = "1"
os.environ["ESTUDIO_MEMORIA"] = os.path.join(_TMP, "memoria.json")
os.environ["ESTUDIO_AJUSTES"] = os.path.join(_TMP, "ajustes.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "motores", "render_video"))

import montaje  # noqa: E402
import movimiento  # noqa: E402
import p7_callouts  # noqa: E402
import transiciones  # noqa: E402

FALLOS = []


def comprobar(condicion, texto):
    if condicion:
        print(f"  ok   {texto}")
    else:
        FALLOS.append(texto)
        print(f"  FALLO  {texto}")


def _escenas():
    textos = [
        "el puerto amanece vacio",
        "que es lo que nadie contaba?",
        "son cuatro mil kilometros de costa",
        "aqui cambia el relato",
        "y entonces se quedo callado...",
        "el contexto del astillero",
        "una cifra cierra la cuenta: doce",
        "y el mar se queda como estaba",
    ]
    escenas, bloques = [], []
    for i, texto in enumerate(textos):
        bid = f"B{i + 1:03d}"
        bloques.append({"id": bid, "texto": texto, "abre_seccion": i == 3})
        escenas.append({
            "id": f"S{i + 1:03d}",
            "t_in": i * 4.0,
            "t_out": (i + 1) * 4.0,
            "duracion": 4.0,
            "narracion": texto,
            "corte": "fuerte" if i % 3 == 0 else "suave",
            "transicion": "acento" if i % 3 == 0 else "suave",
            "origen": {"bloque": bid, "indice": 0},
        })
    return escenas, bloques


def _secuencia(plan):
    return [(p["movimiento"], p["transicion"], p["curva"])
            for p in plan["planos"].values()]


def prueba_semilla_aparte():
    print("\n[1] la semilla de montaje no es la de las imagenes")
    comprobar(montaje.semilla_de({"semilla": 7}, "abc")
              == montaje.semilla_de({}, "abc"),
              "la semilla de assets no mueve la de montaje")
    comprobar(montaje.semilla_de({"semilla_montaje": 99}, "abc") == 99,
              "la guardada si manda")
    comprobar(montaje.semilla_de({}, "uno") != montaje.semilla_de({}, "dos"),
              "dos videos sin semilla guardada no comparten montaje")
    nueva = montaje.semilla_nueva(99, "abc")
    comprobar(nueva != 99, "re-sortear da otra")


def prueba_planes_distintos():
    print("\n[2] el mismo guion, dos montajes")
    escenas, bloques = _escenas()
    uno = montaje.planear(escenas, 11, "dinamico", bloques=bloques,
                          etiquetas=montaje.etiquetas_por_reglas(bloques))
    otro = montaje.planear(escenas, 29, "contemplativo", bloques=bloques,
                           etiquetas=montaje.etiquetas_por_reglas(bloques))
    igual = montaje.planear(escenas, 11, "dinamico", bloques=bloques,
                            etiquetas=montaje.etiquetas_por_reglas(bloques))
    comprobar(uno == igual, "la misma semilla da el mismo plan")
    comprobar(_secuencia(uno) != _secuencia(otro),
              "dos semillas no repiten camara ni transiciones")
    comprobar(uno["temperamento"] == "dinamico"
              and otro["temperamento"] == "contemplativo",
              "el temperamento forzado se respeta")
    for nombre, plan in (("dinamico", uno), ("contemplativo", otro)):
        for sid, plano in plan["planos"].items():
            comprobar(montaje.cabe(plano["zoom"]),
                      f"{nombre} {sid} cabe en el cuadro")
            comprobar(plano["zoom"]["de"] <= montaje.ESCALA_MAX
                      and plano["zoom"]["a"] <= montaje.ESCALA_MAX,
                      f"{nombre} {sid} no pasa el tope de pixeles")
    movimientos = {p["movimiento"] for p in uno["planos"].values()}
    comprobar(len(movimientos) >= 2, f"hay mas de un tipo de camara: {movimientos}")


def prueba_reglas_y_guion_propio():
    print("\n[3] el ritmo sin modelo, y el guion propio")
    bloques = [
        {"id": "B1", "texto": "que paso en el puerto?"},
        {"id": "B2", "texto": "el astillero llevaba anos parado"},
        {"id": "B3", "texto": "son cuatro mil kilometros"},
        {"id": "B4", "texto": "y ahora que?"},
        {"id": "B5", "texto": "el mar se queda"},
    ]
    etiquetas = montaje.etiquetas_por_reglas(bloques)
    comprobar(etiquetas["B1"]["beat"] == "gancho", "el primero es el gancho")
    comprobar(etiquetas["B5"]["beat"] == "cierre", "el ultimo es el cierre")
    comprobar(etiquetas["B3"]["beat"] == "dato", "un numero es un dato")
    comprobar(etiquetas["B4"]["beat"] == "revelacion", "una pregunta destapa")
    et, origen = montaje.etiquetar_bloques(bloques)
    comprobar(origen == "reglas", "en simulacion no se llama al modelo")
    comprobar(et["B1"]["beat"] == "gancho", "y las reglas llegan igual")
    vacio = montaje.texto_para_guion(
        {"gancho": "cifra", "cierre": "eco"}, propio=True)
    comprobar(vacio == "", "con guion propio no se reescribe el texto")
    pedido = montaje.texto_para_guion(
        {"gancho": "cifra", "cierre": "eco"}, propio=False)
    comprobar("cifra" in pedido and "eco" in pedido,
              "cuando el estudio escribe el guion, el arranque entra en el encargo")


def prueba_memoria():
    print("\n[4] no se repite el video anterior")
    montaje.recordar("video-a", temperamento="dinamico", dominante="paneo",
                     gancho="cifra", cierre="imagen", mezcla={"fundido": 2})
    ultimo = montaje.ultimo("video-b")
    comprobar(ultimo.get("temperamento") == "dinamico", "el ultimo es el otro video")
    for semilla in range(8):
        elegido = montaje.elegir_temperamento(semilla, vetado="dinamico")
        comprobar(elegido != "dinamico",
                  f"semilla {semilla} no repite el temperamento anterior")
        gancho, cierre = montaje.elegir_apertura(
            semilla, vetar_gancho="cifra", vetar_cierre="imagen")
        comprobar(gancho != "cifra", f"semilla {semilla} no repite el gancho")
        comprobar(cierre != "imagen", f"semilla {semilla} no repite el cierre")
    g, c = montaje.resolver_apertura(
        4, anterior={"gancho": "escena", "cierre": "eco"}, proyecto_id="video-b")
    comprobar(g == "escena" and c == "eco",
              "rehacer el brief no vuelve a sortear el arranque")
    # rehacer el mismo video no lo convierte en el mas reciente
    montaje.recordar("video-viejo", temperamento="sobrio", gancho="pregunta",
                     cierre="sentencia")
    montaje.recordar("video-a", dominante="deriva")
    comprobar(montaje.ultimo("").get("id") == "video-viejo",
              "actualizar un video viejo no lo pasa al final")


def prueba_subtitulos_y_cortes():
    print("\n[5] subtitulos opcionales, y los cortes de antes")
    comprobar(montaje.quiere_subtitulos({"subtitulos": False}) is False,
              "apagados en el proyecto")
    comprobar(montaje.quiere_subtitulos({"subtitulos": True}) is True,
              "encendidos en el proyecto")
    comprobar(montaje.quiere_subtitulos({}) is True,
              "si nadie lo ha dicho, siguen como siempre")
    svg, fichas = p7_callouts.capa_fija_de(
        {"id": "S001", "narracion": "el puerto amanece vacio de verdad"},
        {"subtitulos": False})
    comprobar(fichas == [], "sin subtitulos no hay fichas")
    comprobar("<text" not in svg, "y el svg fijo no dibuja la linea")
    escenas = [
        {"id": "S001", "transicion": "suave", "duracion": 4.0, "t_in": 0, "t_out": 4},
        {"id": "S002", "transicion": "acento", "duracion": 4.0, "t_in": 4, "t_out": 8},
        {"id": "S003", "transicion": "suave", "duracion": 4.0, "t_in": 8, "t_out": 12,
         "sigue_a": "S002"},
    ]
    de_catalogo = transiciones.resolver(escenas, {}, semilla=7)
    de_montaje = montaje.cortes_de(escenas, {}, semilla_plan=7, documento={})
    comprobar(de_catalogo == de_montaje,
              "sin montaje.json el reparto de transiciones es el de siempre")


def prueba_paneo():
    print("\n[6] un paneo mueve la ventana, y la curva viaja")
    plan = {"escenas": [{
        "id": "S001",
        "t_in": 0, "t_out": 4,
        "zoom": {"de": 1.16, "a": 1.16, "centro": [0.32, 0.5],
                 "centro_fin": [0.68, 0.5], "curva": "cine"},
    }]}
    mov = movimiento.calcular(plan, os.path.join(_TMP, "no"),
                              os.path.join(_TMP, "hyper"),
                              os.path.join(_TMP, "sets"))[0]
    comprobar(mov["ventana_ini"] != mov["ventana_fin"],
              "el centro de llegada no es el de salida")
    comprobar(mov.get("curva") == "cine", "la curva llega al render")
    suave = p7_callouts.suavizar(0.25)
    comprobar(abs(suave - (0.25 * 0.25 * (3 - 0.5))) < 1e-9,
              "sin curva sigue siendo el smoothstep")
    comprobar(p7_callouts.suavizar(0.25, "entrada") < suave,
              "la entrada arranca mas lenta que el smoothstep")


def principal():
    try:
        prueba_semilla_aparte()
        prueba_planes_distintos()
        prueba_reglas_y_guion_propio()
        prueba_memoria()
        prueba_subtitulos_y_cortes()
        prueba_paneo()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if FALLOS:
        print(f"FALLARON {len(FALLOS)} comprobaciones:")
        for texto in FALLOS:
            print(f"  - {texto}")
        return 1
    print("MONTAJE OK: todas las comprobaciones pasan")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
