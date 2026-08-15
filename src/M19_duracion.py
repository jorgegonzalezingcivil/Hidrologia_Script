#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M19 - Curva de duración de caudales
===================================
Entorno: venv del proyecto.

CIERRA EL REGIMEN DE CAUDALES MEDIOS. Toma la serie mensual que el M18 produjo
sobre 456 meses y ordena sus caudales por probabilidad de excedencia: cuántos
meses de cada cien superan un caudal dado.

ALCANCE DE HOY. Este módulo entrega la curva de duración y sus percentiles. El
índice de retención y regulación hídrica y el caudal ambiental quedan
pendientes, y el módulo lo declara en su reporte en lugar de dejar el hueco sin
señalar. Los dos se apoyan en esta curva, de modo que el orden es este.

SE LEE LA SERIE REESCALADA, no la cruda. El M18 corrige la serie mensual con el
factor de almacenamiento derivado del balance anual, y es esa la que representa
la oferta: la cruda promedia un 59 % por encima del caudal de largo plazo,
porque Budyko aplicada mes a mes no tiene memoria entre meses.

LA POSICIÓN DE GRAFICACIÓN ES DE WEIBULL, m/(n+1). Dividir entre n haría que el
caudal mayor tuviera probabilidad de excedencia cero y el menor uno, es decir,
afirmaría que el máximo observado no se supera nunca y que el mínimo se supera
siempre. Con n+1 la curva deja margen a ambos lados, que es lo que corresponde a
una muestra de un proceso continuo.

Productos:
    data/02_procesado/regimen/curva_de_duracion.csv
    data/02_procesado/regimen/percentiles.csv
    data/05_resultados/graficos/M19_curva_de_duracion.png y .svg
    data/02_procesado/M19_duracion.json

Uso:
    python src/M19_duracion.py

Códigos de salida:
    0  correcto
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import json
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

MODULO = "M19"
DESCRIPCION = "Curva de duración de caudales"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Percentiles que el análisis de régimen usa. El 95 es el de estiaje con que se
# define el caudal ambiental por el método más extendido; el 50 es la mediana.
PERCENTILES = (5.0, 10.0, 25.0, 50.0, 75.0, 90.0, 95.0, 99.0)


@dataclass
class ResultadoM19:
    curva: list[dict[str, Any]] = field(default_factory=list)
    percentiles: list[dict[str, Any]] = field(default_factory=list)
    resumen: dict[str, Any] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def curva_de_duracion(caudales: Sequence[float]) -> list[dict[str, Any]]:
    """
    Ordena los caudales y les asigna su probabilidad de excedencia.

    La probabilidad se calcula con la posición de Weibull, m/(n+1), sobre el
    orden DESCENDENTE: el caudal mayor recibe la menor probabilidad de ser
    superado y el menor la mayor.

    POR QUE n+1 Y NO n. Dividir entre n daría probabilidad cero al máximo y uno
    al mínimo, es decir, afirmaría que el mayor caudal observado no se supera
    jamás y que el menor se supera siempre. La muestra no autoriza ninguna de
    las dos cosas: con n+1 la curva deja margen a ambos lados.

    Excepciones
    -----------
    ErrorHidrologia
        Si no hay caudales, o si alguno es negativo: un caudal negativo no es
        ruido, es un fallo del módulo que alimenta.
    """
    valores = list(caudales)
    if not valores:
        raise ErrorHidrologia(
            "la serie no trae ningun caudal: sin muestra no hay curva.")
    negativos = [v for v in valores if v < 0]
    if negativos:
        raise ErrorHidrologia(
            f"{len(negativos)} caudal(es) negativos en la serie, el menor "
            f"{min(negativos):.4f} m3/s. Un caudal negativo no es ruido.")

    ordenados = sorted(valores, reverse=True)
    total = len(ordenados)
    return [{
        "orden": posicion,
        "caudal_m3s": round(valor, 5),
        "excedencia_pct": round(100.0 * posicion / (total + 1), 4),
    } for posicion, valor in enumerate(ordenados, start=1)]


def caudal_para_excedencia(curva: Sequence[dict[str, Any]],
                           excedencia_pct: float) -> float:
    """
    Caudal asociado a una probabilidad de excedencia, interpolando en la curva.

    SE INTERPOLA ENTRE LOS DOS PUNTOS QUE LA ENCIERRAN, no se toma el más
    cercano: con 456 meses cada punto vale 0,22 puntos porcentuales, y redondear
    al vecino desplazaría el Q95 justo en la cola, que es donde se lee el caudal
    ambiental.

    Fuera del rango muestreado se devuelve el extremo, que es lo único que la
    muestra autoriza a afirmar.

    Excepciones
    -----------
    ErrorHidrologia
        Si la curva está vacía o la probabilidad sale de (0, 100).
    """
    if not curva:
        raise ErrorHidrologia("la curva esta vacia.")
    if not 0.0 < excedencia_pct < 100.0:
        raise ErrorHidrologia(
            f"la excedencia {excedencia_pct} debe estar entre 0 y 100 "
            "exclusive: los extremos no son alcanzables con una muestra finita.")

    if excedencia_pct <= curva[0]["excedencia_pct"]:
        return curva[0]["caudal_m3s"]
    if excedencia_pct >= curva[-1]["excedencia_pct"]:
        return curva[-1]["caudal_m3s"]
    for anterior, siguiente in zip(curva, curva[1:]):
        if anterior["excedencia_pct"] <= excedencia_pct <= siguiente["excedencia_pct"]:
            tramo = siguiente["excedencia_pct"] - anterior["excedencia_pct"]
            if tramo <= 0:
                return anterior["caudal_m3s"]
            peso = (excedencia_pct - anterior["excedencia_pct"]) / tramo
            return (anterior["caudal_m3s"] * (1 - peso)
                    + siguiente["caudal_m3s"] * peso)
    return curva[-1]["caudal_m3s"]


def resumir_curva(curva: Sequence[dict[str, Any]],
                  percentiles: Sequence[float] = PERCENTILES) -> dict[str, Any]:
    """
    Percentiles de la curva y los descriptores que el régimen necesita.

    EL ÍNDICE DE VARIABILIDAD Q10/Q90 ES LA LECTURA RÁPIDA de si la cuenca es
    regulada o torrencial: valores bajos indican un régimen sostenido y valores
    altos uno de crecidas y estiajes marcados. Con la serie reescalada por el
    factor de almacenamiento, ese índice NO cambia respecto a la serie cruda,
    porque el factor es multiplicativo y afecta por igual a numerador y
    denominador. Es una propiedad útil: la forma del régimen no depende del
    reescalado, solo su nivel.
    """
    if not curva:
        return {}
    caudales = [f["caudal_m3s"] for f in curva]
    valores = {p: caudal_para_excedencia(curva, p) for p in percentiles}
    q10, q90 = valores.get(10.0), valores.get(90.0)
    media = sum(caudales) / len(caudales)
    return {
        "meses": len(curva),
        "caudal_medio_m3s": round(media, 5),
        "caudal_maximo_m3s": round(max(caudales), 5),
        "caudal_minimo_m3s": round(min(caudales), 5),
        "percentiles": {f"Q{p:g}": round(v, 5) for p, v in valores.items()},
        "indice_variabilidad_q10_q90": (round(q10 / q90, 3)
                                        if q90 and q90 > 0 else None),
        "meses_bajo_la_media": sum(1 for c in caudales if c < media),
        "fraccion_bajo_la_media": round(
            sum(1 for c in caudales if c < media) / len(caudales), 4),
    }


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Levanta la curva de duración sobre la serie mensual del M18."""
    inicio_reloj = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM19()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M19_duracion.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"serie": "data/02_procesado/balance/balance_mensual_serie.csv"},
        parametros=configuracion.parametros("caudal_ambiental"))

    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    ruta = base / "data/02_procesado/balance/balance_mensual_serie.csv"
    if not ruta.is_file():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "regimen.sin_serie",
            f"no se encuentra {ruta.name}: lo escribe el M18. Sin la serie "
            "mensual no hay curva de duracion."))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))

    # SE PREFIERE LA COLUMNA REESCALADA. La cruda promedia por encima del caudal
    # de largo plazo porque Budyko aplicada mes a mes no tiene memoria, y es la
    # ajustada la que representa la oferta.
    columna = ("caudal_ajustado_m3s"
               if filas and "caudal_ajustado_m3s" in filas[0]
               else "caudal_budyko_m3s")
    caudales = []
    for fila in filas:
        try:
            caudales.append(float(fila[columna]))
        except (KeyError, TypeError, ValueError):
            continue

    with registro.bloque(logger, "Curva de duracion"):
        try:
            resultado.curva = curva_de_duracion(caudales)
        except ErrorHidrologia as error:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "regimen.curva", str(error)))
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)
        resultado.resumen = resumir_curva(resultado.curva)
        resultado.resumen["columna"] = columna
        resultado.resumen["reescalada"] = columna == "caudal_ajustado_m3s"
        resultado.percentiles = [
            {"percentil": nombre, "excedencia_pct": float(nombre[1:]),
             "caudal_m3s": valor}
            for nombre, valor in resultado.resumen["percentiles"].items()]
        logger.info("%d meses; Q50 %.3f, Q95 %.4f m3/s; Q10/Q90 %s",
                    resultado.resumen["meses"],
                    resultado.resumen["percentiles"].get("Q50", 0),
                    resultado.resumen["percentiles"].get("Q95", 0),
                    resultado.resumen["indice_variabilidad_q10_q90"])

    with registro.bloque(logger, "Tablas y figura"):
        _escribir(configuracion, base, delimitador, resultado, logger)

    _hallazgos(resultado, configuracion)
    resultado.productos = [str(p) for p in resultado.productos]
    return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                   SALIDA_CORRECTA)


def _hallazgos(resultado, configuracion) -> None:
    """Lo medido, y lo que este modulo todavia no hace."""
    r = resultado.resumen
    percentiles = r.get("percentiles", {})
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "regimen.curva",
        f"curva de duracion sobre {r['meses']} meses, con caudal medio "
        f"{r['caudal_medio_m3s']:.3f} m3/s y rango de "
        f"{r['caudal_minimo_m3s']:.4f} a {r['caudal_maximo_m3s']:.3f}. "
        f"Q50 = {percentiles.get('Q50', 0):.3f}, "
        f"Q90 = {percentiles.get('Q90', 0):.4f} y "
        f"Q95 = {percentiles.get('Q95', 0):.4f} m3/s. El "
        f"{100 * r['fraccion_bajo_la_media']:.0f} por ciento de los meses queda "
        "por debajo de la media, lo que es propio de una distribucion sesgada a "
        "la derecha: unos pocos meses muy humedos levantan el promedio.",
    ))
    variabilidad = r.get("indice_variabilidad_q10_q90")
    if variabilidad:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA if variabilidad > 10 else INFORMATIVO,
            "regimen.variabilidad",
            f"indice de variabilidad Q10/Q90 de {variabilidad:.1f}. Es la "
            "lectura rapida del regimen: valores bajos indican un caudal "
            "sostenido y valores altos crecidas y estiajes marcados. NO cambia "
            "con el reescalado, porque el factor es multiplicativo y afecta por "
            "igual a numerador y denominador: la forma del regimen no depende "
            "del ajuste, solo su nivel.",
        ))
    if r.get("reescalada"):
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "regimen.serie_reescalada",
            "la curva se levanta sobre la serie REESCALADA por el factor de "
            "almacenamiento a escala mensual que el M18 derivo del balance "
            "anual. La serie cruda promedia por encima del caudal de largo "
            "plazo porque Budyko aplicada mes a mes no tiene memoria entre "
            "meses, y es la ajustada la que representa la oferta.",
        ))
    else:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "regimen.serie_sin_reescalar",
            "la serie no trae la columna reescalada y la curva se levanta sobre "
            "la cruda, que promedia por encima del caudal de largo plazo."))
    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "regimen.alcance_pendiente",
        "este modulo entrega HOY solo la curva de duracion y sus percentiles. "
        "El indice de retencion y regulacion hidrica y el caudal ambiental "
        f"quedan pendientes; la configuracion ya declara sus criterios "
        f"(umbral de IRH {configuracion.obtener('caudal_ambiental.umbral_irh')}, "
        f"metodo adoptado "
        f"{configuracion.obtener('caudal_ambiental.metodo_adoptado')!r}). Los "
        "dos se apoyan en esta curva, de modo que el orden es este y el hueco "
        "queda senalado en lugar de callado.",
    ))


def _escribir(configuracion, base, delimitador, resultado, logger) -> None:
    """Tablas y la figura de la curva."""
    destino = rutas.directorio("procesado", base, crear=True) / "regimen"
    destino.mkdir(parents=True, exist_ok=True)
    for nombre, filas in (("curva_de_duracion", resultado.curva),
                          ("percentiles", resultado.percentiles)):
        ruta = destino / f"{nombre}.csv"
        _escribir_csv(ruta, filas, delimitador)
        resultado.productos.append(rutas.relativa(ruta, base))

    try:
        import graficos
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente", f"sin figura: {error}"))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    percentiles = resultado.resumen.get("percentiles", {})

    with graficos.figura(
            estilo, titulo="Curva de duración de caudales medios mensuales",
            etiqueta_x="Probabilidad de excedencia (%)",
            etiqueta_y="Caudal (m³/s)") as (fig, ax):
        ax.plot([f["excedencia_pct"] for f in resultado.curva],
                [f["caudal_m3s"] for f in resultado.curva],
                color=estilo.color(0), linewidth=1.8)
        # EL EJE LOGARITMICO ES OBLIGADO: con un rango de cuatro ordenes de
        # magnitud, en escala lineal la cola de estiaje queda pegada al eje y el
        # Q95 no se puede leer, que es justo el valor que el informe usa.
        ax.set_yscale("log")
        for nombre, color in (("Q50", "#555555"), ("Q95", "#b03a2e")):
            valor = percentiles.get(nombre)
            if valor:
                ax.axhline(valor, color=color, linestyle="--", linewidth=1.2,
                           label=f"{nombre} = {valor:.4f} m³/s")
        ax.set_xlim(0, 100)
        ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
        fig.text(0.01, -0.06,
                 f"{resultado.resumen['meses']} meses de la serie reescalada. "
                 f"Posición de graficación de Weibull, m/(n+1). Índice de "
                 f"variabilidad Q10/Q90 = "
                 f"{resultado.resumen.get('indice_variabilidad_q10_q90')}.\n"
                 "Eje vertical logarítmico: en escala lineal la cola de estiaje "
                 "queda pegada al eje y el Q95 no se puede leer.",
                 fontsize=estilo.tamano_fuente - 2, color="#555555")
        for ruta in graficos.guardar(
                fig, directorio / "M19_curva_de_duracion", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))
    logger.info("Figura escrita")


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
        "resumen": resultado.resumen,
        "percentiles": resultado.percentiles,
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
