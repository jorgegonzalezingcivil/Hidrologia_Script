#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M11c - Factor de reducción por área
===================================
Entorno: venv del proyecto.

SE EVALÚA SIEMPRE (CLAUDE.md, sección 6). El factor no es opcional ni depende
de que el resultado guste: se calcula, se declara y el consultor decide si lo
aplica. Un estudio que no dice qué factor le correspondía no puede explicar
después por qué su caudal es el que es.

QUÉ CORRIGE, Y POR QUÉ EL M11 NO LO HIZO YA. Las isoyetas del M08 interpolan
máximos PUNTUALES: en cada punto, el valor que mediría un pluviómetro allí. El
M11 promedió ese campo sobre cada subcuenca, y ese promedio sigue siendo el
promedio de máximos puntuales. No es lo mismo que el máximo de la lluvia media
sobre el área: los máximos de dos puntos separados no ocurren en el mismo
aguacero, y cuanto mayor es el área menos probable es que todos coincidan. Esa
diferencia es la que corrige el ARF, y promediar un campo no la captura.

SOBRE QUÉ ÁREA. Sobre la de la CUENCA COMPLETA, no la de cada subcuenca. El
factor describe cómo se reparte un aguacero real sobre la superficie que recibe
la tormenta de diseño, y esa superficie es la cuenca entera. Aplicarlo subcuenca
a subcuenca daría factores cercanos a uno sobre áreas de un kilómetro cuadrado y
dejaría la lluvia prácticamente sin reducir, que es el error de concepto más
común con este factor.

LA DURACIÓN IMPORTA Y AQUÍ HAY UNA COSTURA. El ARF crece con la duración: a
igual área, tres horas se reducen más que veinticuatro. Este módulo tiene en la
mano P24h, de modo que aplica el factor de 24 h y obtiene una P24h AREAL, que es
una magnitud con sentido propio. El factor de la duración de diseño se calcula
también y se declara, junto con el cociente que falta por aplicar, para que el
M12b lo use tras la desagregación sin volver a reducir lo ya reducido.

Productos:
    data/02_procesado/precipitacion/arf.csv
    data/02_procesado/precipitacion/precipitacion_areal.csv
    data/02_procesado/M11c_arf.json

Uso:
    python src/M11c_arf.py

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

MODULO = "M11c"
DESCRIPCION = "Factor de reducción por área"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Duración a la que corresponde la serie de Pmáx del M07: máximos en 24 horas.
DURACION_DE_LA_SERIE_H = 24.0


@dataclass
class ResultadoM11c:
    area_km2: float = 0.0
    factores: list[dict[str, Any]] = field(default_factory=list)
    adoptado: dict[str, Any] = field(default_factory=dict)
    subcuencas: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def leer_tabla_arf(ruta: Path, delimitador: str) -> list[dict[str, float]]:
    """
    Tabla de factores de reducción, en formato largo: área, duración y factor.

    Es doctrina y vive en data/referencia. Se lee entera con su origen para que
    el informe pueda citar de dónde sale el factor adoptado.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si una fila no trae área, duración y factor legibles, o si el factor
        cae fuera de (0, 1]. Un ARF mayor que uno AMPLIFICARÍA la lluvia.
    """
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la tabla de ARF en {ruta}.")

    filas: list[dict[str, float]] = []
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for numero, fila in enumerate(
                csv.DictReader(manejador, delimiter=delimitador), start=2):
            if not str(fila.get("area_km2", "")).strip():
                continue
            try:
                area = float(fila["area_km2"])
                duracion = float(fila["duracion_h"])
                factor = float(fila["arf"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ErrorFormato(
                    f"{ruta.name}, línea {numero}: no se pudo leer área, "
                    f"duración y factor ({exc}).") from exc
            if not 0.0 < factor <= 1.0:
                raise ErrorFormato(
                    f"{ruta.name}, línea {numero}: el factor {factor} está "
                    "fuera de (0, 1]. Por encima de uno amplificaría la "
                    "lluvia en lugar de reducirla.")
            filas.append({"area_km2": area, "duracion_h": duracion,
                          "arf": factor,
                          "origen": str(fila.get("origen", "")).strip()})
    if not filas:
        raise ErrorFormato(f"{ruta.name} no contiene ninguna fila.")
    return filas


def interpolar_arf(
    tabla: Sequence[dict[str, float]], area_km2: float, duracion_h: float,
) -> dict[str, Any]:
    """
    Factor para un área y una duración, interpolando la tabla.

    EN EL ÁREA SE INTERPOLA EN LOGARITMO, porque así están trazadas las curvas
    del ARF: entre 100 y 200 km2 el factor cae lo mismo que entre 200 y 400, no
    lo mismo que entre 200 y 300. Interpolar linealmente sobre un eje que se
    dibujó logarítmico introduce un sesgo sistemático hacia factores altos, es
    decir hacia el lado inseguro.

    En la duración se interpola linealmente, que es como se tabula.

    Fuera del rango de la tabla NO se extrapola: se toma el extremo y se declara.
    Extrapolar una curva empírica más allá de donde se midió es exactamente lo
    que la matriz de tiempos de concentración impide en el M10, y no hay razón
    para admitirlo aquí.
    """
    if area_km2 <= 0 or duracion_h <= 0:
        raise ErrorHidrologia(
            f"área ({area_km2}) y duración ({duracion_h}) deben ser positivas.")

    areas = sorted({f["area_km2"] for f in tabla})
    duraciones = sorted({f["duracion_h"] for f in tabla})
    fuera = []
    if area_km2 < areas[0] or area_km2 > areas[-1]:
        fuera.append(f"área {area_km2:.1f} km2 fuera del rango tabulado "
                     f"{areas[0]:.0f} a {areas[-1]:.0f} km2")
    if duracion_h < duraciones[0] or duracion_h > duraciones[-1]:
        fuera.append(f"duración {duracion_h:.1f} h fuera del rango tabulado "
                     f"{duraciones[0]:.1f} a {duraciones[-1]:.1f} h")

    area = min(max(area_km2, areas[0]), areas[-1])
    duracion = min(max(duracion_h, duraciones[0]), duraciones[-1])

    por_clave = {(f["area_km2"], f["duracion_h"]): f["arf"] for f in tabla}
    area_baja, area_alta = _vecinos(areas, area)
    duracion_baja, duracion_alta = _vecinos(duraciones, duracion)

    faltan = [(a, d) for a in (area_baja, area_alta)
              for d in (duracion_baja, duracion_alta) if (a, d) not in por_clave]
    if faltan:
        raise ErrorFormato(
            f"la tabla no es rectangular: faltan las combinaciones {faltan}. "
            "Sin ellas no se puede interpolar sin inventar valores.")

    peso_area = 0.0
    if area_alta != area_baja:
        peso_area = ((math.log(area) - math.log(area_baja))
                     / (math.log(area_alta) - math.log(area_baja)))
    peso_duracion = 0.0
    if duracion_alta != duracion_baja:
        peso_duracion = ((duracion - duracion_baja)
                         / (duracion_alta - duracion_baja))

    inferior = (por_clave[(area_baja, duracion_baja)] * (1 - peso_duracion)
                + por_clave[(area_baja, duracion_alta)] * peso_duracion)
    superior = (por_clave[(area_alta, duracion_baja)] * (1 - peso_duracion)
                + por_clave[(area_alta, duracion_alta)] * peso_duracion)
    factor = inferior * (1 - peso_area) + superior * peso_area

    return {
        "area_km2": round(area_km2, 3),
        "duracion_h": duracion_h,
        "arf": round(factor, 4),
        "area_usada_km2": round(area, 3),
        "duracion_usada_h": duracion,
        "fuera_de_tabla": "; ".join(fuera),
        "origen": next((f.get("origen", "") for f in tabla
                        if f.get("origen")), ""),
    }


def _vecinos(valores: Sequence[float], objetivo: float) -> tuple[float, float]:
    """Los dos valores tabulados que encierran al objetivo."""
    bajo = max((v for v in valores if v <= objetivo), default=valores[0])
    alto = min((v for v in valores if v >= objetivo), default=valores[-1])
    return bajo, alto


def arf_analitico(area_km2: float, duracion_h: float) -> float:
    """
    Verificación analítica, independiente de la tabla.

    Expresión exponencial del US Weather Bureau, en la forma que reproduce la
    literatura hidrológica:

        ARF = 1 - exp(-1,1 * D^0,25) + exp(-1,1 * D^0,25 - 0,01 * A)

    con D en horas y A en km2. Cumple lo que debe cumplir: vale 1 con área nula
    y decrece con el área.

    NO ES LA TABLA NI PRETENDE SUSTITUIRLA. Está para que las dos cifras se vean
    juntas: si se separan mucho, el consultor tiene que mirar antes de adoptar.
    Coincidir tampoco demuestra que la tabla sea la correcta, solo que dos
    métodos de la misma familia dan lo mismo.
    """
    if area_km2 <= 0 or duracion_h <= 0:
        raise ErrorHidrologia("área y duración deben ser positivas.")
    termino = 1.1 * duracion_h ** 0.25
    return 1.0 - math.exp(-termino) + math.exp(-termino - 0.01 * area_km2)


def aplicar_factor(
    subcuencas: Sequence[dict[str, Any]], factor: float, columnas: Sequence[str],
) -> list[dict[str, Any]]:
    """
    Aplica el factor a cada columna de precipitación, conservando la original.

    Se conservan las dos, la puntual y la areal. Sustituir en el sitio dejaría
    una tabla en la que no se puede comprobar qué se aplicó ni deshacerlo.
    """
    salida = []
    for subcuenca in subcuencas:
        fila = dict(subcuenca)
        for columna in columnas:
            valor = subcuenca.get(columna)
            if valor is None:
                continue
            try:
                fila[columna.replace("_mm", "_areal_mm")] = round(
                    float(valor) * factor, 2)
            except (TypeError, ValueError):
                continue
        fila["arf"] = round(factor, 4)
        salida.append(fila)
    return salida


def leer_precipitacion(ruta: Path, delimitador: str) -> list[dict[str, Any]]:
    """Lee la tabla que dejó el M11, con una fila por subcuenca."""
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}: ejecutar antes el M11, que es quien "
            "promedia los campos por subcuenca.")
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    if not filas:
        raise ErrorFormato(f"{ruta.name} no contiene ninguna subcuenca.")
    return filas


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Evalúa el factor de reducción y lo aplica según la política declarada."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM11c()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M11c_arf.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "precipitacion por subcuenca":
                "data/02_procesado/precipitacion/precipitacion_por_subcuenca.csv",
            "tabla ARF": configuracion.obtener("arf.tabla"),
        },
        parametros=configuracion.parametros("arf"))

    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    directorio = rutas.directorio("procesado", base) / "precipitacion"
    duracion_diseno = float(configuracion.obtener("tormenta.duracion_h"))

    try:
        subcuencas = leer_precipitacion(
            directorio / "precipitacion_por_subcuenca.csv", delimitador)
        tabla = leer_tabla_arf(
            rutas.resolver(configuracion.obtener("arf.tabla"), base),
            delimitador)
    except (ErrorFormato, ErrorRutas) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "arf.insumos", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    area = sum(float(s.get("area_km2") or 0.0) for s in subcuencas)
    resultado.area_km2 = round(area, 3)
    logger.info("%d subcuenca(s), area total %.2f km2", len(subcuencas), area)

    # --- Factores ------------------------------------------------------------
    with registro.bloque(logger, "Factor de reduccion"):
        for duracion, etiqueta in ((DURACION_DE_LA_SERIE_H, "serie de Pmax"),
                                   (duracion_diseno, "tormenta de diseno")):
            try:
                factor = interpolar_arf(tabla, area, duracion)
            except (ErrorFormato, ErrorHidrologia) as error:
                resultado.hallazgos.append(Hallazgo(
                    BLOQUEANTE, "arf.interpolacion", str(error)))
                return _cerrar(logger, resultado, base, ruta_json, inicio,
                               SALIDA_BLOQUEANTE)
            factor["para"] = etiqueta
            if bool(configuracion.obtener("arf.verificacion_analitica")):
                analitico = arf_analitico(area, duracion)
                factor["arf_analitico"] = round(analitico, 4)
                factor["diferencia_pct"] = round(
                    100.0 * (factor["arf"] - analitico) / analitico, 2)
            resultado.factores.append(factor)
            logger.info("%-18s duracion %5.1f h -> ARF %.4f%s",
                        etiqueta, duracion, factor["arf"],
                        f" (analitico {factor['arf_analitico']:.4f})"
                        if "arf_analitico" in factor else "")

        _resolver_factores(resultado, duracion_diseno, logger)

    # --- Aplicacion ----------------------------------------------------------
    with registro.bloque(logger, "Aplicacion"):
        _resolver_aplicacion(configuracion, resultado, subcuencas, logger)

    _escribir_productos(base, resultado, delimitador, logger)
    return _cerrar(logger, resultado, base, ruta_json, inicio, SALIDA_CORRECTA)


def _resolver_factores(resultado, duracion_diseno, logger) -> None:
    """Declara los dos factores, su contraste analítico y la costura pendiente."""
    de_serie = resultado.factores[0]
    de_diseno = resultado.factores[1]

    fuera = [f for f in resultado.factores if f["fuera_de_tabla"]]
    if fuera:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "arf.fuera_de_tabla",
            "se pidio el factor fuera del rango tabulado: "
            + "; ".join(f["fuera_de_tabla"] for f in fuera)
            + ". NO se extrapola: se tomo el extremo de la tabla. Extrapolar "
            "una curva empirica mas alla de donde se midio es lo mismo que la "
            "matriz de tiempos de concentracion impide en el M10.",
        ))

    discrepantes = [f for f in resultado.factores
                    if abs(f.get("diferencia_pct") or 0.0) > 10.0]
    if discrepantes:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "arf.discrepancia",
            "la tabla y la verificacion analitica no coinciden: "
            + "; ".join(
                f"{f['para']} ({f['duracion_h']:.0f} h) tabla {f['arf']:.3f} "
                f"frente a {f['arf_analitico']:.3f}, "
                f"{f['diferencia_pct']:+.1f} %" for f in discrepantes)
            + ". Las dos son de la misma familia empirica, de modo que una "
            "diferencia asi no es ruido: significa que una de las dos no "
            "corresponde a esta region. Mirar antes de adoptar.",
        ))

    residual = (de_diseno["arf"] / de_serie["arf"]) if de_serie["arf"] else None
    resultado.adoptado = {
        "arf_serie_24h": de_serie["arf"],
        "arf_diseno": de_diseno["arf"],
        "duracion_diseno_h": duracion_diseno,
        "residual_para_el_m12b": round(residual, 4) if residual else None,
    }
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "arf.factores",
        f"sobre {resultado.area_km2:.1f} km2 de cuenca: ARF de "
        f"{de_serie['arf']:.3f} a 24 h, que es la duracion de la serie de Pmax, "
        f"y {de_diseno['arf']:.3f} a {duracion_diseno:.0f} h, que es la de la "
        f"tormenta de diseno. Se aplica el primero, porque es el que "
        "corresponde al dato que hay en la mano, y queda un residual de "
        f"{residual:.4f} que el M12b debe aplicar TRAS la desagregacion. "
        "Aplicar aqui el de la tormenta reduciria una P24h con el factor de "
        "otra duracion; no aplicar el residual despues dejaria la lluvia de "
        "diseno sin la reduccion que le toca.",
    ))
    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "arf.residual_pendiente",
        f"queda un factor residual de {residual:.4f} sin aplicar. El M12b debe "
        "aplicarlo a la precipitacion desagregada a la duracion de diseno. Si "
        "no lo hace, la lluvia de diseno queda reducida con el factor de 24 h y "
        f"no con el de {duracion_diseno:.0f} h, es decir un "
        f"{100.0 * (1 - residual):.1f} % por encima de lo que corresponde, del "
        "lado inseguro.",
    ))


def _resolver_aplicacion(configuracion, resultado, subcuencas, logger) -> None:
    """Aplica o no el factor, según la política declarada, y lo deja escrito."""
    politica = str(configuracion.obtener("arf.aplicar")).strip().lower()
    factor = resultado.adoptado["arf_serie_24h"]
    columnas = [c for c in subcuencas[0] if c.startswith("p_T")
                and c.endswith("_mm")]

    if not columnas:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "arf.sin_columnas",
            "la tabla del M11 no trae ninguna columna de precipitacion "
            "'p_T*_mm' que reducir.",
        ))
        return

    if politica == "forzar_no":
        resultado.subcuencas = [dict(s, arf=1.0) for s in subcuencas]
        resultado.adoptado["aplicado"] = False
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "arf.no_aplicado",
            f"por decision declarada ('forzar_no') NO se aplica el factor, que "
            f"habria sido {factor:.3f}. La precipitacion de diseno queda un "
            f"{100.0 * (1 / factor - 1):.1f} % por encima de la que resultaria "
            "de aplicarlo. Es una decision del consultor y debe justificarse en "
            "el informe, no basta con declararla en la configuracion.",
        ))
        return

    resultado.subcuencas = aplicar_factor(subcuencas, factor, columnas)
    resultado.adoptado["aplicado"] = True
    resultado.adoptado["politica"] = politica
    logger.info("Factor %.4f aplicado a %d columna(s) de %d subcuenca(s)",
                factor, len(columnas), len(resultado.subcuencas))
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "arf.aplicado",
        f"factor {factor:.3f} aplicado a {len(columnas)} periodo(s) de retorno "
        f"en {len(resultado.subcuencas)} subcuenca(s), politica {politica!r}. "
        "Se conservan las dos columnas, la puntual y la areal: sustituir en el "
        "sitio dejaria una tabla en la que no se puede comprobar que se aplico "
        "ni deshacerlo.",
    ))


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Escribe una tabla con las columnas de todas sus filas."""
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


def _escribir_productos(base, resultado, delimitador, logger) -> None:
    """Escribe la tabla de factores y la de precipitación areal."""
    directorio = rutas.directorio("procesado", base, crear=True) / "precipitacion"
    for nombre, contenido in (("arf.csv", resultado.factores),
                              ("precipitacion_areal.csv", resultado.subcuencas)):
        destino = directorio / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))
    logger.info("Productos escritos en %s", rutas.relativa(directorio, base))


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
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    reporte = {
        "modulo": MODULO,
        "area_km2": resultado.area_km2,
        "factores": resultado.factores,
        "adoptado": resultado.adoptado,
        "productos": resultado.productos,
        "resumen": conteo,
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
    sys.exit(main())
