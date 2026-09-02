#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M18b - Infiltración por el modelo de Schosinsky y Losilla
=========================================================
Entorno: venv del proyecto.

TERCER TÉRMINO DEL BALANCE. El M18 cerró P = ETR + E; aquí se reparte esa
escorrentía separando la fracción que se infiltra y alimenta el agua
subterránea de la que escurre por la superficie.

    C = Kfc + Kp + Kv          coeficiente de infiltración
    I = C · (P - Ret)          lámina infiltrada, mm/mes

Los tres sumandos son fracciones que se infiltran por efecto de la TEXTURA del
suelo, de la PENDIENTE y de la COBERTURA vegetal. La retención del follaje se
descuenta antes: esa parte de la lluvia no llega al suelo.

LOS PARÁMETROS SALEN DEL SUELO DE ESTE ESTUDIO. La infiltración básica se deriva
del grupo hidrológico que el M10 determinó sobre la capa de suelos, no de un
valor tomado de un estudio de otra zona. Esa es la diferencia con la práctica
habitual, y es la que permite que el módulo sirva para otra cuenca.

LA HOMOLOGACIÓN DE COBERTURA ES UNA DECISIÓN CON MARGEN, y por eso vive en
data/referencia con su criterio escrito por cada clase, no en el código. El
modelo tiene cinco clases de cobertura y el estudio diez: alguien tiene que
decidir el mapeo, y el informe debe poder explicarlo.

DOS COTAS QUE EL MODELO IMPONE. El coeficiente no puede pasar de uno, porque no
se infiltra más agua de la que llega al suelo; y la lámina infiltrada no puede
superar la que quedó tras la retención. Las dos se aplican y se reportan cuando
muerden, porque un coeficiente que satura sistemáticamente indica que la
combinación de suelo, pendiente y cobertura salió del rango del modelo.

Productos:
    data/02_procesado/infiltracion/coeficientes_por_subcuenca.csv
    data/02_procesado/infiltracion/infiltracion_mensual.csv
    data/05_resultados/excel/M18b_infiltracion.xlsx
    data/05_resultados/graficos/M18b_*.png y .svg
    data/02_procesado/M18b_infiltracion.json

Uso:
    python src/M18b_infiltracion.py

Códigos de salida:
    0  correcto
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M18b"
DESCRIPCION = "Infiltración por el modelo de Schosinsky y Losilla"

# Límites de validez de la relación entre infiltración básica y Kfc, en mm/día.
# Fuera de ellos la fórmula logarítmica no aplica y el modelo define el valor.
FC_MINIMO = 16.0
FC_MAXIMO = 1568.0

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3


@dataclass
class ResultadoM18b:
    coeficientes: list[dict[str, Any]] = field(default_factory=list)
    mensual: list[dict[str, Any]] = field(default_factory=list)
    resumen: dict[str, Any] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def kfc_por_infiltracion_basica(fc_mm_dia: float) -> dict[str, Any]:
    """
    Fracción que se infiltra por efecto de la TEXTURA del suelo.

        fc < 16          Kfc = 0,0148 · fc / 16
        16 <= fc <= 1568 Kfc = 0,267 · ln(fc) - 0,000154 · fc - 0,723
        fc > 1568        Kfc = 1

    LOS TRES TRAMOS SON DEL MODELO, no un apaño. La relación logarítmica se
    ajustó entre 16 y 1568 mm/día; por debajo el modelo la sustituye por una
    recta que pasa por el origen, y por encima satura, porque un suelo no puede
    infiltrar más de lo que le llega.

    Se devuelve si el valor quedó fuera del rango ajustado, para que quien llame
    lo reporte: un suelo en esa zona está descrito por la extensión del modelo y
    no por su ajuste.

    SE DEVUELVEN EL CALCULADO Y EL ADOPTADO. La tabla del informe los pide en
    dos columnas, y con razón: son iguales salvo cuando la fórmula se sale de
    [0, 1] o cuando el tramo saturado impone 1, y es justo ahí donde el
    consultor tiene que poder decir que el valor se recortó y por qué.

    Excepciones
    -----------
    ErrorHidrologia
        Si la infiltración básica no es positiva.
    """
    if fc_mm_dia <= 0:
        raise ErrorHidrologia(
            f"la infiltracion basica vale {fc_mm_dia} mm/dia y debe ser "
            "positiva: un suelo que no infiltra nada no tiene Kfc.")
    if fc_mm_dia < FC_MINIMO:
        calculado = 0.0148 * fc_mm_dia / FC_MINIMO
        return {"kfc": round(calculado, 5), "kfc_calculado": round(calculado, 5),
                "fuera_de_rango": True, "tramo": "bajo"}
    if fc_mm_dia > FC_MAXIMO:
        calculado = 0.267 * math.log(fc_mm_dia) - 0.000154 * fc_mm_dia - 0.723
        return {"kfc": 1.0, "kfc_calculado": round(calculado, 5),
                "fuera_de_rango": True, "tramo": "alto"}
    calculado = 0.267 * math.log(fc_mm_dia) - 0.000154 * fc_mm_dia - 0.723
    return {"kfc": round(max(0.0, min(1.0, calculado)), 5),
            "kfc_calculado": round(calculado, 5),
            "fuera_de_rango": False, "tramo": "ajustado"}


def kp_por_pendiente(pendiente: float,
                     tabla: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """
    Fracción que se infiltra por efecto de la PENDIENTE.

    A menos pendiente, más tiempo de contacto del agua con el suelo y más
    infiltración. La tabla es doctrina y vive en data/referencia.

    Excepciones
    -----------
    ErrorFormato
        Si ninguna clase cubre esa pendiente. No se extrapola: la tabla define
        clases cerradas y una pendiente fuera de todas ellas señala un dato
        erróneo, no una clase que falte.
    """
    if pendiente < 0:
        raise ErrorHidrologia(
            f"la pendiente {pendiente} es negativa.")
    for fila in tabla:
        try:
            desde, hasta = float(fila["desde"]), float(fila["hasta"])
        except (KeyError, TypeError, ValueError):
            continue
        if desde <= pendiente < hasta:
            return {"kp": float(fila["valor"]), "clase": fila.get("clave", "")}
    raise ErrorFormato(
        f"ninguna clase de la tabla cubre la pendiente {pendiente:.4f}.")


def coeficiente_de_infiltracion(kfc: float, kp: float, kv: float) -> dict[str, Any]:
    """
    Suma los tres efectos y acota el resultado a la unidad.

    NO SE PUEDE INFILTRAR MÁS AGUA DE LA QUE LLEGA AL SUELO. La suma de los tres
    puede pasar de uno cuando coinciden suelo permeable, terreno llano y
    cobertura densa; el modelo satura ahí, y se reporta cuando ocurre porque
    saturar de forma sistemática indica que la combinación salió de su rango.
    """
    suma = kfc + kp + kv
    return {"c": round(min(1.0, suma), 5), "suma_sin_acotar": round(suma, 5),
            "saturado": suma > 1.0}


def retencion_de_follaje(precipitacion_mm: float, coeficiente: float,
                         lluvia_minima_mm: float = 5.0) -> float:
    """
    Lámina que la vegetación retiene y devuelve sin llegar al suelo.

    POR DEBAJO DE UNA LLUVIA MÍNIMA SE RETIENE TODO: no alcanza a mojar el
    follaje lo bastante para que gotee. Es lo que hace que un mes muy seco no
    aporte nada de infiltración, y no una fracción pequeña de nada.
    """
    if precipitacion_mm <= 0:
        return 0.0
    if precipitacion_mm < lluvia_minima_mm:
        return precipitacion_mm
    return min(precipitacion_mm, coeficiente * precipitacion_mm)


def infiltracion_mensual(precipitacion_mm: float, coeficiente_c: float,
                         retencion_mm: float) -> dict[str, Any]:
    """
    Lámina infiltrada de un mes, y lo que queda para la escorrentía superficial.

        I = C · (P - Ret)

    LA INFILTRACIÓN NO PUEDE SUPERAR LO QUE LLEGÓ AL SUELO, y con C acotado a
    uno eso se cumple por construcción; se comprueba igualmente porque es la
    clase de invariante que un cambio futuro rompe sin darse cuenta.

    Excepciones
    -----------
    ErrorHidrologia
        Si la retención supera la precipitación: sería devolver a la atmósfera
        agua que no cayó.
    """
    if precipitacion_mm < 0 or retencion_mm < 0:
        raise ErrorHidrologia(
            f"no se admiten laminas negativas: P={precipitacion_mm}, "
            f"Ret={retencion_mm} mm.")
    if retencion_mm > precipitacion_mm + 1e-9:
        raise ErrorHidrologia(
            f"la retencion de {retencion_mm} mm supera la precipitacion de "
            f"{precipitacion_mm} mm.")
    disponible = precipitacion_mm - retencion_mm
    infiltrada = min(coeficiente_c * disponible, disponible)
    return {
        "precipitacion_mm": round(precipitacion_mm, 3),
        "retencion_mm": round(retencion_mm, 3),
        "disponible_mm": round(disponible, 3),
        "infiltracion_mm": round(infiltrada, 3),
        "escorrentia_superficial_mm": round(disponible - infiltrada, 3),
    }


# =============================================================================
# Ejecución
# =============================================================================
def leer_doctrina(ruta: Path, delimitador: str) -> dict[str, Any]:
    """
    Lee la tabla de Schosinsky y la reparte por tipo de coeficiente.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si falta alguno de los tres bloques que el modelo necesita.
    """
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la tabla de Schosinsky en {ruta}.")
    por_tipo: dict[str, list[dict]] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            por_tipo.setdefault(str(fila.get("tipo", "")).strip(),
                                []).append(fila)
    faltan = [t for t in ("fc_por_grupo", "kp_pendiente", "kv_cobertura",
                          "retencion") if t not in por_tipo]
    if faltan:
        raise ErrorFormato(f"{ruta.name} no trae los bloques {faltan}.")
    return por_tipo


def leer_homologacion(ruta: Path, delimitador: str) -> dict[str, dict]:
    """
    Homologación de las coberturas del estudio a las clases del modelo.

    ES UNA DECISIÓN CON MARGEN y vive fuera del código con su criterio escrito
    por cada clase: el modelo tiene cinco coberturas y un estudio puede traer
    diez o cincuenta, y alguien tiene que decidir el mapeo.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    """
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la homologacion en {ruta}.")
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        return {str(f["cobertura"]).strip(): f
                for f in csv.DictReader(manejador, delimiter=delimitador)}


def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Calcula el coeficiente de infiltración y reparte la lámina mensual."""
    inicio_reloj = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM18b()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M18b_infiltracion.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"morfometria": "data/02_procesado/morfometria/subcuencas.csv"},
        parametros=configuracion.parametros("infiltracion"))

    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    try:
        doctrina = leer_doctrina(rutas.resolver(
            configuracion.obtener("infiltracion.tabla"), base), delimitador)
        homologacion = leer_homologacion(rutas.resolver(
            configuracion.obtener("infiltracion.tabla_cobertura"), base),
            delimitador)
    except (ErrorRutas, ErrorFormato) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "infiltracion.doctrina", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Coeficiente por subcuenca"):
        if not _resolver_coeficientes(configuracion, base, delimitador,
                                      doctrina, homologacion, resultado,
                                      logger):
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Reparto mensual"):
        _resolver_mensual(base, delimitador, doctrina, homologacion, resultado,
                          logger)

    with registro.bloque(logger, "Tablas y figuras"):
        _escribir(configuracion, base, delimitador, resultado, logger)

    resultado.productos = [str(p) for p in resultado.productos]
    return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                   SALIDA_CORRECTA)


def _grupo_de_la_cuenca(base, resultado):
    """
    Grupo hidrológico dominante, del reporte del M10.

    SE USA EL DOMINANTE DE LA CUENCA Y NO EL DE CADA SUBCUENCA, porque el M10
    publica el reparto para el conjunto y no por unidad. Es una simplificación
    real y se declara: en una cuenca con suelos contrastados entre cabecera y
    parte baja, el coeficiente de infiltración saldría igual en todas cuando no
    debería.
    """
    ruta = rutas.directorio("procesado", base) / "M10_morfometria.json"
    if not ruta.is_file():
        return None, []
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, []
    suelos = datos.get("suelos", {})
    return suelos.get("grupo_dominante"), suelos.get("reparto", [])


def _resolver_coeficientes(configuracion, base, delimitador, doctrina,
                           homologacion, resultado, logger) -> bool:
    """Cruza suelo, pendiente y cobertura en cada subcuenca."""
    ruta = base / "data/02_procesado/morfometria/subcuencas.csv"
    if not ruta.is_file():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "infiltracion.sin_subcuencas",
            f"no se encuentra {ruta.name}: lo escribe el M10."))
        return False
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        subcuencas = list(csv.DictReader(manejador, delimiter=delimitador))

    grupo, reparto = _grupo_de_la_cuenca(base, resultado)
    if grupo is None:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "infiltracion.sin_grupo",
            "el reporte del M10 no trae el grupo hidrologico dominante: sin el "
            "no hay infiltracion basica de donde partir."))
        return False

    fc_por_grupo = {f["clave"]: float(f["valor"])
                    for f in doctrina["fc_por_grupo"]}
    if grupo not in fc_por_grupo:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "infiltracion.grupo_desconocido",
            f"el grupo {grupo!r} no esta en la tabla, que trae "
            f"{sorted(fc_por_grupo)}."))
        return False

    fc = fc_por_grupo[grupo]
    kfc = kfc_por_infiltracion_basica(fc)
    kv_por_clase = {f["clave"]: float(f["valor"])
                    for f in doctrina["kv_cobertura"]}

    sin_cobertura, saturadas = [], []
    for fila in subcuencas:
        try:
            pendiente = float(fila["pendiente_cuenca"])
            area = float(fila["area_km2"])
        except (KeyError, TypeError, ValueError):
            continue
        cobertura = str(fila.get("cobertura_dominante", "")).strip()
        ficha = homologacion.get(cobertura)
        if ficha is None:
            sin_cobertura.append(cobertura)
            continue
        try:
            kp = kp_por_pendiente(pendiente, doctrina["kp_pendiente"])
        except (ErrorFormato, ErrorHidrologia) as error:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "infiltracion.pendiente",
                f"{fila.get('subcuenca')}: {error}"))
            continue
        kv = float(ficha.get("kv") or kv_por_clase.get(
            ficha["clase_schosinsky"], 0.0))
        coeficiente = coeficiente_de_infiltracion(kfc["kfc"], kp["kp"], kv)
        if coeficiente["saturado"]:
            saturadas.append(fila.get("subcuenca"))
        resultado.coeficientes.append({
            "subcuenca": fila.get("subcuenca", ""),
            "area_km2": round(area, 4),
            "grupo_hidrologico": grupo,
            "fc_mm_dia": fc,
            # EN CM/HORA, que es como la tabla del informe titula la primera
            # columna. La doctrina de Schosinsky la tabula en mm/dia y ambas
            # son el mismo dato: 10 mm por cm y 24 horas por dia.
            "fc_cm_hr": round(fc / 10.0 / 24.0, 4),
            "kfc": kfc["kfc"],
            # EL CALCULADO Y EL ADOPTADO EN DOS COLUMNAS, que es lo que el
            # informe pide. Solo difieren cuando la formula se sale de [0, 1] o
            # cuando el tramo saturado impone 1, y es ahi donde hay que poder
            # decir que el valor se recorto.
            "kfc_calculado": kfc.get("kfc_calculado", kfc["kfc"]),
            "pendiente": round(pendiente, 4),
            "clase_pendiente": kp["clase"],
            "kp": kp["kp"],
            "cobertura": cobertura,
            "clase_cobertura": ficha["clase_schosinsky"],
            "kv": kv,
            "c": coeficiente["c"],
            "saturado": coeficiente["saturado"],
        })

    if not resultado.coeficientes:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "infiltracion.sin_coeficientes",
            "ninguna subcuenca recibio coeficiente."))
        return False

    valores = [f["c"] for f in resultado.coeficientes]
    area = sum(f["area_km2"] for f in resultado.coeficientes)
    resultado.resumen["c_medio"] = round(sum(
        f["c"] * f["area_km2"] for f in resultado.coeficientes) / area, 4)
    resultado.resumen["grupo_hidrologico"] = grupo
    resultado.resumen["fc_mm_dia"] = fc
    resultado.resumen["area_km2"] = round(area, 3)
    logger.info("Coeficiente de %.3f a %.3f, medio ponderado %.3f",
                min(valores), max(valores), resultado.resumen["c_medio"])

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "infiltracion.coeficientes",
        f"{len(resultado.coeficientes)} subcuenca(s) con coeficiente de "
        f"infiltracion entre {min(valores):.3f} y {max(valores):.3f}, medio "
        f"ponderado {resultado.resumen['c_medio']:.3f}. Sale de Kfc "
        f"{kfc['kfc']:.3f} con el grupo hidrologico {grupo} del estudio "
        f"(fc {fc:.0f} mm/dia), mas Kp por pendiente y Kv por cobertura. LOS "
        "PARAMETROS SON DEL SUELO DE ESTA CUENCA, no de un estudio de otra zona.",
    ))
    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "infiltracion.grupo_unico",
        f"se aplica el grupo hidrologico DOMINANTE de la cuenca ({grupo}) a "
        f"todas las subcuencas, porque el M10 publica el reparto para el "
        f"conjunto y no por unidad. Reparto medido: "
        + "; ".join(f"{r['grupo']} {r['porcentaje']:.1f} %" for r in reparto)
        + ". En una cuenca con suelos contrastados entre cabecera y parte baja "
        "el coeficiente saldria igual en todas cuando no deberia; aqui el "
        "dominante cubre la mayor parte del area y el efecto es menor.",
    ))
    if kfc["fuera_de_rango"]:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "infiltracion.kfc_fuera_de_rango",
            f"la infiltracion basica de {fc:.0f} mm/dia queda fuera del rango "
            f"en que se ajusto la relacion de Kfc (16 a 1568 mm/dia): el valor "
            f"procede de la extension del modelo ({kfc['tramo']}) y no de su "
            "ajuste."))
    if sin_cobertura:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "infiltracion.cobertura_sin_homologar",
            f"{len(sin_cobertura)} subcuenca(s) con cobertura sin homologar: "
            f"{sorted(set(sin_cobertura))}. Anadirlas a la tabla."))
    if saturadas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "infiltracion.coeficiente_saturado",
            f"{len(saturadas)} subcuenca(s) con la suma de los tres "
            "coeficientes por encima de uno, acotada al limite. Saturar de "
            "forma sistematica indica que la combinacion de suelo, pendiente y "
            "cobertura salio del rango del modelo."))
    return True


def _resolver_mensual(base, delimitador, doctrina, homologacion, resultado,
                      logger) -> None:
    """
    Reparte la lámina mensual de la cuenca entre infiltración y escorrentía.

    LA LLUVIA ES LA DEL M18, no una nueva: interpolarla otra vez aqui daria dos
    series de precipitacion en el mismo estudio y con dos valores distintos.
    """
    ruta = base / "data/02_procesado/precipitacion/precipitacion_mensual_cuenca.csv"
    if not ruta.is_file():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "infiltracion.sin_lluvia",
            f"no se encuentra {ruta.name}: lo escribe el M18. Sin la serie "
            "mensual no hay reparto que hacer."))
        return
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        mensual = list(csv.DictReader(manejador, delimiter=delimitador))

    retencion = {f["clave"]: float(f["valor"]) for f in doctrina["retencion"]}
    minima = retencion.get("lluvia_minima_mm", 5.0)
    # La cobertura que gobierna es la de mayor area, y con ella su coeficiente
    # de follaje: bosque retiene mas que el resto.
    por_clase: dict[str, float] = {}
    for fila in resultado.coeficientes:
        por_clase[fila["clase_cobertura"]] = por_clase.get(
            fila["clase_cobertura"], 0.0) + fila["area_km2"]
    dominante = max(por_clase, key=por_clase.get) if por_clase else ""
    cfo = retencion.get("bosque" if dominante == "bosques"
                        else "otras_coberturas", 0.12)
    coeficiente = resultado.resumen.get("c_medio", 0.0)

    for fila in mensual:
        try:
            mes = int(fila["mes"])
            lluvia = float(fila["p_mm"])
        except (KeyError, TypeError, ValueError):
            continue
        retenida = retencion_de_follaje(lluvia, cfo, minima)
        reparto = infiltracion_mensual(lluvia, coeficiente, retenida)
        reparto["mes"] = mes
        reparto["coeficiente_c"] = coeficiente
        reparto["cfo"] = cfo
        resultado.mensual.append(reparto)

    if not resultado.mensual:
        return
    total = sum(f["infiltracion_mm"] for f in resultado.mensual)
    lluvia_anual = sum(f["precipitacion_mm"] for f in resultado.mensual)
    resultado.resumen.update({
        "infiltracion_anual_mm": round(total, 1),
        "retencion_anual_mm": round(
            sum(f["retencion_mm"] for f in resultado.mensual), 1),
        "escorrentia_superficial_anual_mm": round(
            sum(f["escorrentia_superficial_mm"] for f in resultado.mensual), 1),
        "precipitacion_anual_mm": round(lluvia_anual, 1),
        "cobertura_dominante": dominante,
        "cfo": cfo,
    })
    logger.info("Infiltracion anual %.0f mm de %.0f mm de lluvia (%.0f %%)",
                total, lluvia_anual, 100.0 * total / lluvia_anual)

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "infiltracion.mensual",
        f"de {lluvia_anual:.0f} mm de lluvia anual, "
        f"{resultado.resumen['retencion_anual_mm']:.0f} quedan retenidos en el "
        f"follaje, {total:.0f} se infiltran ({100.0 * total / lluvia_anual:.0f} "
        f"por ciento) y "
        f"{resultado.resumen['escorrentia_superficial_anual_mm']:.0f} escurren "
        f"en superficie. Cobertura dominante {dominante!r} con coeficiente de "
        f"follaje {cfo:.2f}. La lluvia es la que el M18 interpolo: no se vuelve "
        "a interpolar aqui, que daria dos series distintas en el mismo estudio.",
    ))
    _contrastar_con_el_balance(base, resultado, delimitador)


def _contrastar_con_el_balance(base, resultado, delimitador) -> None:
    """
    Comprueba que la infiltración no contradiga el balance del M18.

    SON DOS MODELOS DISTINTOS SOBRE LA MISMA LLUVIA, y no miden lo mismo. El M18
    separa lo que se evapora de lo que escurre, en NETO; este separa lo que
    entra al suelo de lo que corre por la superficie, en BRUTO.

    QUE LA INFILTRACIÓN SUPERE LA ESCORRENTÍA DEL BALANCE NO ES UNA
    CONTRADICCIÓN. El agua que entra al suelo no se va toda al acuífero: buena
    parte vuelve a la atmósfera desde el perfil, y esa parte es la que Budyko
    contabiliza como evapotranspiración real. Infiltración bruta por encima de
    escorrentía neta es lo normal en una cuenca húmeda.

    Lo que sí sería imposible es que la infiltración superase la lluvia que
    llegó al suelo, y eso lo impide el propio acotado de C.

    El contraste se reporta igualmente porque acota el reparto: la diferencia
    entre infiltración bruta y escorrentía neta es agua que el suelo recibe y
    devuelve, y su magnitud dice cuánto del término de evapotranspiración pasa
    por el perfil en lugar de interceptarse en el follaje.
    """
    ruta = base / "data/02_procesado/balance/balance_mensual.csv"
    if not ruta.is_file() or not resultado.mensual:
        return
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        balance = {int(f["mes"]): f for f in csv.DictReader(
            manejador, delimiter=delimitador) if f.get("mes")}
    if not balance:
        return

    conflictos = []
    for fila in resultado.mensual:
        ficha = balance.get(fila["mes"])
        if ficha is None:
            continue
        try:
            escurrida = float(ficha["escorrentia_budyko_mm"])
        except (KeyError, TypeError, ValueError):
            continue
        fila["escorrentia_del_balance_mm"] = round(escurrida, 2)
        if fila["infiltracion_mm"] > escurrida + 1e-6:
            conflictos.append(fila["mes"])

    infiltrada = sum(f["infiltracion_mm"] for f in resultado.mensual)
    escurrida = sum(f.get("escorrentia_del_balance_mm", 0.0)
                    for f in resultado.mensual)
    resultado.resumen["escorrentia_del_balance_anual_mm"] = round(escurrida, 1)
    resultado.resumen["exceso_sobre_el_balance_mm"] = round(
        infiltrada - escurrida, 1)
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "infiltracion.contraste_con_el_balance",
        f"la infiltracion bruta anual es de {infiltrada:.0f} mm y la "
        f"escorrentia NETA del balance de Budyko de {escurrida:.0f} mm; la "
        f"primera supera a la segunda en {infiltrada - escurrida:.0f} mm, en "
        f"{len(conflictos)} de {len(resultado.mensual)} meses. NO ES UNA "
        "CONTRADICCION: el agua que entra al suelo no se va toda al acuifero, "
        "y buena parte vuelve a la atmosfera desde el perfil, que es lo que "
        "Budyko contabiliza como evapotranspiracion real. Esa diferencia mide "
        "cuanto del termino de evapotranspiracion pasa por el suelo en lugar "
        "de interceptarse en el follaje. Lo que si seria imposible, que la "
        "infiltracion superase la lluvia que llego al suelo, lo impide el "
        "acotado de C.",
    ))


def _escribir(configuracion, base, delimitador, resultado, logger) -> None:
    """Tablas, libro y figuras del reparto de infiltración."""
    destino = rutas.resolver(configuracion.obtener("infiltracion.salida"), base)
    destino.mkdir(parents=True, exist_ok=True)
    tablas = (("coeficientes_por_subcuenca", resultado.coeficientes),
              ("infiltracion_mensual", resultado.mensual))
    for nombre, filas in tablas:
        ruta = destino / f"{nombre}.csv"
        _escribir_csv(ruta, filas, delimitador)
        resultado.productos.append(rutas.relativa(ruta, base))

    try:
        import excel
        detalle = excel.escribir_libro(
            rutas.directorio("resultados_excel", base, crear=True)
            / "M18b_infiltracion.xlsx",
            [(n, f) for n, f in tablas if f])
        resultado.productos.append(
            rutas.relativa(Path(detalle["archivo"]), base))
    except Exception as error:  # noqa: BLE001 - depende del entorno
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "infiltracion.excel", f"sin libro: {error}"))

    _figuras(configuracion, base, resultado, logger)


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Escribe una tabla con la union de las columnas de todas sus filas."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    filas = list(filas)
    if not filas:
        destino.write_text("", encoding="utf-8-sig")
        return
    columnas: list[str] = []
    for fila in filas:
        for clave in fila:
            if clave not in columnas:
                columnas.append(clave)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(manejador, fieldnames=columnas,
                                  delimiter=delimitador, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(filas)


def _figuras(configuracion, base, resultado, logger) -> None:
    """Reparto mensual, aporte de cada coeficiente y mapa del coeficiente."""
    if not resultado.coeficientes:
        return
    try:
        import graficos
        from comun import shapefile
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente", f"sin figuras: {error}"))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    escritas = 0

    # 1. El reparto mensual de la lluvia en sus tres destinos.
    if resultado.mensual:
        meses = [f["mes"] for f in resultado.mensual]
        retencion = [f["retencion_mm"] for f in resultado.mensual]
        infiltrada = [f["infiltracion_mm"] for f in resultado.mensual]
        superficial = [f["escorrentia_superficial_mm"]
                       for f in resultado.mensual]
        with graficos.figura(
                estilo, titulo="Reparto mensual de la precipitación",
                etiqueta_x="Mes", etiqueta_y="Lámina (mm/mes)") as (fig, ax):
            ax.bar(meses, retencion, color="#7d3c98", label="retención")
            ax.bar(meses, infiltrada, bottom=retencion, color=estilo.color(0),
                   label="infiltración")
            ax.bar(meses, superficial,
                   bottom=[a + b for a, b in zip(retencion, infiltrada)],
                   color=estilo.color(1), label="escorrentía superficial")
            ax.set_xticks(meses)
            graficos.leyenda(ax, estilo)
            fig.text(0.01, -0.04,
                     f"La suma de cada barra es la precipitación del mes. "
                     f"Coeficiente de infiltración {resultado.resumen.get('c_medio'):.3f}, "
                     f"coeficiente de follaje {resultado.resumen.get('cfo')}.",
                     fontsize=estilo.tamano_fuente - 2, color="#555555")
            for ruta in graficos.guardar(
                    fig, directorio / "M18b_reparto_mensual", estilo):
                resultado.productos.append(rutas.relativa(ruta, base))
            escritas += 1

    # 2. Cuanto aporta cada uno de los tres coeficientes. DICE DONDE ESTA LA
    #    PALANCA: si Kfc domina, lo que importa es el ensayo de infiltracion;
    #    si domina Kv, la homologacion de cobertura.
    with graficos.figura(
            estilo, titulo="Aporte de cada coeficiente a la infiltración",
            etiqueta_x="Subcuencas ordenadas por coeficiente total",
            etiqueta_y="Coeficiente") as (fig, ax):
        ordenadas = sorted(resultado.coeficientes, key=lambda f: f["c"])
        posiciones = list(range(len(ordenadas)))
        kfc = [f["kfc"] for f in ordenadas]
        kp = [f["kp"] for f in ordenadas]
        kv = [f["kv"] for f in ordenadas]
        ax.bar(posiciones, kfc, color=estilo.color(0), label="Kfc, textura")
        ax.bar(posiciones, kp, bottom=kfc, color=estilo.color(1),
               label="Kp, pendiente")
        ax.bar(posiciones, kv, bottom=[a + b for a, b in zip(kfc, kp)],
               color=estilo.color(2), label="Kv, cobertura")
        ax.axhline(1.0, color="#b03a2e", linestyle="--", linewidth=1.2,
                   label="límite del modelo")
        graficos.leyenda(ax, estilo)
        ax.set_xticks([])
        for ruta in graficos.guardar(
                fig, directorio / "M18b_aporte_coeficientes", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))
        escritas += 1

    # 3. El coeficiente sobre el mapa.
    candidatas = sorted((base / "data/03_SIG/vector").glob("*ubCuenca*.shp"))
    if candidatas:
        entidades = shapefile.leer_geometrias(candidatas[0])
        if len(entidades) == len(resultado.coeficientes):
            with graficos.figura(
                    estilo, titulo="Coeficiente de infiltración por subcuenca",
                    etiqueta_x="Este (m)",
                    etiqueta_y="Norte (m)") as (fig, ax):
                mapeador = graficos.coropleta(
                    ax, entidades, [f["c"] for f in resultado.coeficientes],
                    estilo)
                graficos.barra_de_color(fig, ax, mapeador, estilo,
                                        "Coeficiente C")
                for ruta in graficos.guardar(
                        fig, directorio / "M18b_mapa_coeficiente", estilo):
                    resultado.productos.append(rutas.relativa(ruta, base))
                escritas += 1
    logger.info("%d figura(s) escritas", escritas)


def _cerrar(logger, resultado, base, ruta_json, inicio, codigo):
    """Emite el reporte, escribe el JSON y cierra el log."""
    orden = {BLOQUEANTE: 0, ADVERTENCIA: 1, INFORMATIVO: 2}
    hallazgos = sorted(resultado.hallazgos,
                       key=lambda h: (orden.get(h.severidad, 9), h.clave))

    logger.info(registro.SEPARADOR)
    for severidad, emitir in ((BLOQUEANTE, logger.error),
                              (ADVERTENCIA, logger.warning),
                              (INFORMATIVO, logger.info)):
        grupo = [h for h in hallazgos if h.severidad == severidad]
        if not grupo:
            continue
        emitir("%s (%d)", severidad, len(grupo))
        for hallazgo in grupo:
            emitir("  %-44s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    if conteo[BLOQUEANTE] and codigo == SALIDA_CORRECTA:
        codigo = SALIDA_BLOQUEANTE
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    reporte = {
        "modulo": MODULO,
        "subcuencas": len(resultado.coeficientes),
        "meses": len(resultado.mensual),
        "resumen": resultado.resumen,
        "productos": resultado.productos,
        "conteo": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=1),
                         encoding="utf-8")

    productos = {f"producto {i}": p
                 for i, p in enumerate(resultado.productos, start=1)}
    productos["reporte JSON"] = rutas.relativa(ruta_json, base)
    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecucion"] = rutas.relativa(archivo_log, base)

    registro.registrar_cierre(
        logger, MODULO, "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO",
        segundos=time.perf_counter() - inicio, productos=productos)
    return codigo, hallazgos


def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(description=DESCRIPCION)
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None)
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json)
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato,
            ErrorHidrologia) as error:
        print(f"{MODULO}: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
