#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M05b - Clasificación ENSO-ONI y agregaciones por fase
=====================================================
Entorno: venv del proyecto.

CLAUDE.md, sección 6, es tajante: "ENSO | No elimina estaciones ni registros.
Solo clasifica". Este módulo etiqueta y agrega; ningún dato sale del análisis
por su fase, y el esquema lo verifica con un invariante (enso.elimina_registros
en verdadero es BLOQUEANTE).

Qué produce y para qué. La decisión de isoyetas de la sección 6 pide
"precipitación total mensual multianual por fase ENSO", de modo que el M06
necesita, por estación, cuánto llueve bajo Niño, bajo Niña y en condiciones
neutrales. Eso es lo que este módulo calcula sobre la serie completa que entrega
el M05.

La clasificación es MENSUAL, no anual. Es el defecto de fondo de la rutina
heredada ENSOONI.py, que asignaba una etiqueta por año calendario: con ese
criterio el Niño de 1997-98, que va de mayo de 1997 a abril de 1998, queda
partido en dos y los meses de cada año reciben una etiqueta promediada que no
corresponde a ninguno. El índice ONI publica temporadas de tres meses, y el mes
central de cada una es lo que permite etiquetar mes a mes.

Se aplica además la definición oficial de episodio: el umbral debe sostenerse al
menos cinco temporadas consecutivas. Sobre el índice completo, exigirlo cambia
la fase de 34 temporadas frente a aplicar el umbral suelto. Sin ese control,
cualquier oscilación breve entraría como episodio y la agregación mezclaría
meses que no pertenecen a ninguno.

Productos:
    data/01_crudos/enso/oni.ascii.txt
    data/02_procesado/enso/clasificacion_oni.csv
    data/02_procesado/enso/precipitacion_por_fase.csv
    data/02_procesado/enso/ciclo_anual_por_fase.csv
    data/02_procesado/enso/M05b_enso.md
    data/02_procesado/M05b_enso.json
    data/05_resultados/graficos/M05b_*.png y .svg

Uso:
    python src/M05b_enso.py
    python src/M05b_enso.py --sin-descarga
    python src/M05b_enso.py --sin-graficas

Códigos de salida:
    0  clasificación producida
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

import numpy as np

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, oni, registro, rutas  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M05b"
DESCRIPCION = "Clasificación ENSO-ONI y agregaciones por fase"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

FASES = (oni.FASE_NINO, oni.FASE_NINA, oni.FASE_NEUTRAL)

# Mínimo de años con dato que debe tener una pareja estación-fase para que su
# total multianual se publique sin advertencia. Por debajo, la media anual
# descansa en muy pocos años y no representa la fase.
MINIMO_ANIOS_POR_FASE = 5


@dataclass
class ResultadoM05b:
    temporadas: int = 0
    por_fase: dict[str, int] = field(default_factory=dict)
    estaciones: int = 0
    periodos: int = 0
    sin_clasificar: int = 0
    totales: list[dict[str, Any]] = field(default_factory=list)
    ciclo: list[dict[str, Any]] = field(default_factory=list)
    clasificacion: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def leer_serie_mensual(
    ruta: Path, delimitador: str,
) -> tuple[list[str], list[tuple[int, int]], np.ndarray]:
    """
    Lee la serie que entrega el M05, complementada si existe.

    Devuelve los códigos, las claves de periodo y la matriz de valores.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}. Ejecutar el M05 antes que este módulo."
        )
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    if not filas:
        raise ErrorFormato(f"{ruta.name} está vacío.")
    codigos = [c for c in filas[0] if c not in ("anio", "mes")]
    claves: list[tuple[int, int]] = []
    valores = np.full((len(filas), len(codigos)), np.nan)
    for indice, fila in enumerate(filas):
        claves.append((int(fila["anio"]), int(fila["mes"])))
        for columna, codigo in enumerate(codigos):
            texto = (fila.get(codigo) or "").strip()
            if texto:
                valores[indice, columna] = float(texto)
    return codigos, claves, valores


def fase_por_periodo(clasificacion: Sequence[dict]) -> dict[tuple[int, int], str]:
    """Índice de (año, mes) a fase, para cruzar con la serie de precipitación."""
    return {(f["anio"], f["mes"]): f["fase"] for f in clasificacion}


def ciclo_anual_por_fase(
    codigos: Sequence[str],
    claves: Sequence[tuple[int, int]],
    valores: np.ndarray,
    fases: dict[tuple[int, int], str],
) -> list[dict[str, Any]]:
    """
    Media de cada mes calendario dentro de cada fase, por estación.

    Es la base de todo lo demás. Se promedia por mes y no sobre el conjunto
    porque las fases no se reparten por igual entre meses del año: un Niño que
    cayó sobre dos temporadas húmedas y una seca daría, sin separar por mes, una
    media inflada que no describe ni la temporada húmeda ni la seca.
    """
    filas: list[dict[str, Any]] = []
    for columna, codigo in enumerate(codigos):
        for fase in FASES:
            for mes in range(1, 13):
                muestras = [
                    valores[indice, columna]
                    for indice, clave in enumerate(claves)
                    if clave[1] == mes and fases.get(clave) == fase
                    and np.isfinite(valores[indice, columna])
                ]
                if not muestras:
                    continue
                filas.append({
                    "codigo": codigo, "fase": fase, "mes": mes,
                    "media_mm": round(float(np.mean(muestras)), 2),
                    "desviacion_mm": (round(float(np.std(muestras, ddof=1)), 2)
                                      if len(muestras) > 1 else None),
                    "n_meses": len(muestras),
                })
    return filas


def totales_por_fase(
    ciclo: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Total anual multianual de cada estación bajo cada fase.

    Se suman las doce medias mensuales en lugar de promediar los totales
    anuales. La razón es que muy pocos años caen ENTEROS dentro de una fase: los
    episodios empiezan y terminan a mitad de año, de modo que promediar totales
    anuales descartaría casi toda la muestra. Sumar las medias mensuales usa
    cada mes disponible y produce el total que cabría esperar de un año
    completo bajo esa fase.

    Se reporta cuántos meses sustentan cada suma y si faltan meses del año: un
    total al que le falten meses NO es comparable con otro completo, y la
    isoyeta del M06 los trataría igual.
    """
    agrupado: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fila in ciclo:
        agrupado.setdefault((fila["codigo"], fila["fase"]), []).append(fila)

    filas: list[dict[str, Any]] = []
    for (codigo, fase), meses in sorted(agrupado.items()):
        presentes = {m["mes"] for m in meses}
        total = float(sum(m["media_mm"] for m in meses))
        muestras = int(sum(m["n_meses"] for m in meses))
        filas.append({
            "codigo": codigo,
            "fase": fase,
            "total_anual_mm": round(total, 1),
            "meses_del_anio": len(presentes),
            "completo": len(presentes) == 12,
            "n_muestras": muestras,
            "anios_equivalentes": round(muestras / 12.0, 1),
        })
    return filas


def contraste_entre_fases(
    totales: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Diferencia porcentual de cada fase respecto de la neutral, por estación.

    Es la cifra que el informe necesita: cuánto más o menos llueve bajo Niño y
    bajo Niña en cada punto. Solo se calcula cuando ambas sumas cubren los doce
    meses, porque comparar un total completo con otro al que le faltan meses
    daría una diferencia que es del muestreo y no del clima.
    """
    por_estacion: dict[str, dict[str, dict[str, Any]]] = {}
    for fila in totales:
        por_estacion.setdefault(fila["codigo"], {})[fila["fase"]] = fila

    filas: list[dict[str, Any]] = []
    for codigo, fases in sorted(por_estacion.items()):
        neutral = fases.get(oni.FASE_NEUTRAL)
        if neutral is None or not neutral["completo"]:
            continue
        base = neutral["total_anual_mm"]
        if base <= 0:
            continue
        fila: dict[str, Any] = {"codigo": codigo,
                                "neutral_mm": round(base, 1)}
        for fase in (oni.FASE_NINO, oni.FASE_NINA):
            datos = fases.get(fase)
            if datos is None or not datos["completo"]:
                fila[f"{fase}_mm"] = None
                fila[f"{fase}_pct"] = None
                continue
            fila[f"{fase}_mm"] = round(datos["total_anual_mm"], 1)
            fila[f"{fase}_pct"] = round(
                100.0 * (datos["total_anual_mm"] - base) / base, 1)
        filas.append(fila)
    return filas


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    descargar: bool = True,
    con_graficas: bool = True,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Clasifica el índice, cruza con la serie del M05 y agrega por fase."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")
    url = configuracion.obtener("enso.url_oni")
    umbral = float(configuracion.obtener("enso.umbral_anomalia_c"))
    consecutivas = int(configuracion.obtener("enso.temporadas_consecutivas"))
    criterio = str(configuracion.obtener("enso.criterio"))
    crudo = rutas.directorio("crudos_enso", base, crear=True) / "oni.ascii.txt"

    serie = rutas.directorio("procesado_series", base) / \
        "precipitacion_mensual_complementada.csv"
    if not serie.is_file():
        serie = rutas.directorio("procesado_series", base) / \
            "precipitacion_mensual.csv"

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"indice ONI": url if descargar else rutas.relativa(crudo, base),
                 "serie mensual": rutas.relativa(serie, base)},
        parametros={
            "enso.umbral_anomalia_c": umbral,
            "enso.temporadas_consecutivas": consecutivas,
            "enso.criterio": criterio,
            "enso.elimina_registros":
                configuracion.obtener("enso.elimina_registros"),
        },
    )

    resultado = ResultadoM05b()

    # --- Índice ONI ----------------------------------------------------------
    with registro.bloque(logger, "Índice ONI de la NOAA"):
        if descargar:
            try:
                oni.descargar(url, crudo)
                logger.info("Descargado de %s", url)
            except ErrorRutas as exc:
                if not crudo.is_file():
                    raise
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, "enso.descarga",
                    f"no se pudo consultar el servicio ({exc}). Se usa la copia "
                    f"local de {crudo.name}, que puede estar desactualizada.",
                ))
        registros = oni.interpretar(oni.leer(crudo))
        resultado.temporadas = len(registros)
        resultado.clasificacion = oni.clasificar(
            registros, umbral=umbral, consecutivas=consecutivas,
            exigir_consecutivas=(criterio == "consecutivo"),
        )
        resultado.por_fase = oni.resumen_por_fase(resultado.clasificacion)
        logger.info("%s temporada(s) de %d a %d | %s",
                    f"{len(registros):,}", registros[0].anio,
                    registros[-1].anio,
                    ", ".join(f"{k}: {v}" for k, v in resultado.por_fase.items()))

        cambian = sum(1 for f in resultado.clasificacion
                      if f["fase"] != f["fase_por_umbral"])
        if criterio == "consecutivo":
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "enso.criterio",
                f"se exige que el umbral se sostenga {consecutivas} temporada(s) "
                f"consecutivas, que es la definición oficial. Frente a aplicar "
                f"el umbral suelto, {cambian} temporada(s) cambian de fase: sin "
                "ese control, cualquier oscilación breve entraría como episodio.",
            ))
        else:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "enso.criterio",
                f"el criterio declarado es {criterio!r}: se clasifica por umbral "
                "simple y el resultado NO corresponde a la definición de "
                f"episodio de la NOAA. {cambian} temporada(s) difieren de ella.",
            ))

    # --- Cruce con la serie --------------------------------------------------
    with registro.bloque(logger, "Agregación por fase"):
        codigos, claves, valores = leer_serie_mensual(serie, delimitador)
        resultado.estaciones = len(codigos)
        resultado.periodos = len(claves)
        fases = fase_por_periodo(resultado.clasificacion)

        sin_fase = [c for c in claves if c not in fases]
        resultado.sin_clasificar = len(sin_fase)
        if sin_fase:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "enso.sin_clasificar",
                f"{len(sin_fase)} periodo(s) de la serie no tienen fase en el "
                f"índice ONI (de {sin_fase[0]} a {sin_fase[-1]}). Sus valores "
                "quedan fuera de la agregación por fase, pero SIGUEN en la serie: "
                "el ENSO no elimina registros.",
            ))

        resultado.ciclo = ciclo_anual_por_fase(codigos, claves, valores, fases)
        resultado.totales = totales_por_fase(resultado.ciclo)
        logger.info("%d estación(es) x %d periodo(s); %d fila(s) de ciclo anual",
                    len(codigos), len(claves), len(resultado.ciclo))

        incompletos = [t for t in resultado.totales if not t["completo"]]
        if incompletos:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "enso.total_incompleto",
                f"{len(incompletos)} pareja(s) estación-fase cuyo total anual no "
                "cubre los doce meses. No son comparables con los completos y el "
                "M06 los trataría igual: se marcan con 'completo' en falso.",
            ))
        escasos = [t for t in resultado.totales
                   if t["anios_equivalentes"] < MINIMO_ANIOS_POR_FASE]
        if escasos:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "enso.muestra_escasa",
                f"{len(escasos)} pareja(s) estación-fase con menos de "
                f"{MINIMO_ANIOS_POR_FASE} años equivalentes de muestra. Su media "
                "descansa en muy pocos episodios y no representa la fase.",
            ))

    # --- Productos -----------------------------------------------------------
    with registro.bloque(logger, "Escritura de productos"):
        _escribir_productos(configuracion, base, resultado, delimitador, logger)

    if con_graficas:
        with registro.bloque(logger, "Gráficas"):
            _figuras(configuracion, base, resultado, logger)

    resultado.hallazgos.extend(_resumir(resultado, configuracion))
    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Vuelca una tabla de diccionarios homogéneos."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    filas = list(filas)
    if not filas:
        destino.write_text("", encoding="utf-8-sig")
        return
    campos: list[str] = []
    for fila in filas:
        for clave in fila:
            if clave not in campos:
                campos.append(clave)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(manejador, fieldnames=campos,
                                  delimiter=delimitador, restval="")
        escritor.writeheader()
        escritor.writerows(filas)


def _escribir_productos(configuracion, base, resultado, delimitador,
                        logger) -> None:
    """Escribe clasificación, agregaciones e informe."""
    directorio = rutas.directorio("procesado_enso", base, crear=True)

    for nombre, contenido in (
        ("clasificacion_oni.csv", resultado.clasificacion),
        ("ciclo_anual_por_fase.csv", resultado.ciclo),
        ("precipitacion_por_fase.csv", resultado.totales),
        ("contraste_entre_fases.csv", contraste_entre_fases(resultado.totales)),
    ):
        destino = directorio / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))

    informe = directorio / "M05b_enso.md"
    _escribir_informe(informe, resultado, configuracion)
    resultado.productos.append(rutas.relativa(informe, base))
    logger.info("Clasificación de %d temporada(s) y %d total(es) por fase",
                len(resultado.clasificacion), len(resultado.totales))


def _tabla_markdown(filas, columnas) -> list[str]:
    lineas = ["| " + " | ".join(columnas) + " |",
              "|" + "|".join("---" for _ in columnas) + "|"]
    for fila in filas:
        lineas.append("| " + " | ".join(str(fila.get(c, "")) for c in columnas) + " |")
    return lineas


def _escribir_informe(destino, resultado, configuracion) -> None:
    """Informe en Markdown, en la línea de las rutinas heredadas."""
    contraste = contraste_entre_fases(resultado.totales)
    lineas = [
        "# M05b - Clasificacion ENSO-ONI y agregaciones por fase",
        "",
        f"* Temporadas del indice: {resultado.temporadas:,}",
        f"* Umbral: +/- {configuracion.obtener('enso.umbral_anomalia_c')} C",
        f"* Temporadas consecutivas exigidas: "
        f"{configuracion.obtener('enso.temporadas_consecutivas')}",
        f"* Criterio: {configuracion.obtener('enso.criterio')}",
        f"* Estaciones: {resultado.estaciones} | periodos: {resultado.periodos}",
        "",
        "El ENSO **no elimina** estaciones ni registros: solo clasifica",
        "(CLAUDE.md, seccion 6). El esquema lo verifica con un invariante.",
        "",
        "## Clasificacion",
        "",
        "La clasificacion es MENSUAL y no anual. El indice ONI publica",
        "temporadas de tres meses, y el mes central de cada una permite",
        "etiquetar mes a mes. La rutina heredada asignaba una etiqueta por anio",
        "calendario, con lo que un episodio como el Nino de 1997-98, que va de",
        "mayo de 1997 a abril de 1998, quedaba partido en dos.",
        "",
    ]
    lineas += _tabla_markdown(
        [{"fase": k, "temporadas": v, "porcentaje":
          f"{100.0 * v / max(1, resultado.temporadas):.1f}%"}
         for k, v in resultado.por_fase.items()],
        ["fase", "temporadas", "porcentaje"])
    lineas += [
        "",
        "## Precipitacion por fase",
        "",
        "El total anual de cada fase se obtiene sumando las doce medias",
        "mensuales, no promediando totales anuales: muy pocos anios caen",
        "ENTEROS dentro de una fase, porque los episodios empiezan y terminan a",
        "mitad de anio, y promediar totales anuales descartaria casi toda la",
        "muestra.",
        "",
    ]
    if contraste:
        valores_nino = [f["nino_pct"] for f in contraste
                        if f.get("nino_pct") is not None]
        valores_nina = [f["nina_pct"] for f in contraste
                        if f.get("nina_pct") is not None]
        if valores_nino:
            lineas.append(
                f"* Bajo Nino, la precipitacion anual cambia entre "
                f"{min(valores_nino):+.0f}% y {max(valores_nino):+.0f}% "
                f"respecto de la neutral (mediana "
                f"{sorted(valores_nino)[len(valores_nino) // 2]:+.0f}%).")
        if valores_nina:
            lineas.append(
                f"* Bajo Nina, entre {min(valores_nina):+.0f}% y "
                f"{max(valores_nina):+.0f}% (mediana "
                f"{sorted(valores_nina)[len(valores_nina) // 2]:+.0f}%).")
        lineas += [
            "",
            "El detalle por estacion esta en `contraste_entre_fases.csv`, y los",
            "totales que el M06 interpola en `precipitacion_por_fase.csv`.",
            "",
        ]
    lineas += [
        "## Figuras",
        "",
        "En `data/05_resultados/graficos`, en PNG y SVG.",
        "",
        "* `M05b_indice_oni`: serie del indice con las fases sombreadas.",
        "* `M05b_ciclo_por_fase`: regimen medio de cada fase, promediado sobre",
        "  las estaciones, con la neutral de referencia.",
        "* `M05b_contraste`: cambio porcentual por estacion respecto de la",
        "  neutral, bajo cada fase.",
        "",
    ]
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")


# =============================================================================
# Gráficas
# =============================================================================
def _figuras(configuracion, base, resultado, logger) -> None:
    """Emite las figuras del módulo."""
    try:
        import graficos
    except ImportError as exc:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudieron generar las figuras: {exc}.",
        ))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(configuracion.obtener("graficos.directorio"), base)
    color_de = {oni.FASE_NINO: "#c00000", oni.FASE_NINA: "#1f4e79",
                oni.FASE_NEUTRAL: "#9a9a9a"}

    # --- Serie del índice con las fases sombreadas ---------------------------
    orden = sorted(resultado.clasificacion, key=lambda f: (f["anio"], f["mes"]))
    tiempo = [f["anio"] + (f["mes"] - 0.5) / 12.0 for f in orden]
    anomalia = [f["anomalia"] for f in orden]
    umbral = float(configuracion.obtener("enso.umbral_anomalia_c"))
    with graficos.figura(
        estilo, titulo="Índice ONI y episodios clasificados",
        etiqueta_x="año", etiqueta_y="anomalía (°C)",
    ) as (fig, ax):
        ax.plot(tiempo, anomalia, color="#333333", linewidth=0.8, zorder=3)
        ax.axhline(0.0, color=graficos.GRIS_CONTEXTO, linewidth=0.6)
        for signo in (umbral, -umbral):
            ax.axhline(signo, color=graficos.GRIS_CONTEXTO, linewidth=0.6,
                       linestyle="--")
        for fase in (oni.FASE_NINO, oni.FASE_NINA):
            mascara = [f["fase"] == fase for f in orden]
            ax.fill_between(tiempo, 0, anomalia, where=mascara,
                            color=color_de[fase], alpha=0.55, zorder=2,
                            interpolate=False)
        graficos.leyenda_manual(ax, [
            ("Niño", color_de[oni.FASE_NINO]),
            ("Niña", color_de[oni.FASE_NINA]),
        ], estilo)
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M05b_indice_oni",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Ciclo anual medio por fase ------------------------------------------
    with graficos.figura(
        estilo, titulo="Régimen medio por fase ENSO, promedio de las estaciones",
        etiqueta_x="mes", etiqueta_y="precipitación media (mm)",
    ) as (fig, ax):
        for fase in FASES:
            medias = []
            for mes in range(1, 13):
                muestras = [f["media_mm"] for f in resultado.ciclo
                            if f["fase"] == fase and f["mes"] == mes]
                medias.append(float(np.mean(muestras)) if muestras else np.nan)
            ax.plot(range(1, 13), medias, color=color_de[fase], linewidth=1.8,
                    marker="o", markersize=3.5, label=fase)
        ax.set_xticks(range(1, 13))
        ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M05b_ciclo_por_fase",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Contraste por estación ----------------------------------------------
    contraste = [f for f in contraste_entre_fases(resultado.totales)
                 if f.get("nino_pct") is not None or f.get("nina_pct") is not None]
    if contraste:
        contraste.sort(key=lambda f: (f.get("nino_pct") if f.get("nino_pct")
                                      is not None else 0.0))
        etiquetas = [f["codigo"] for f in contraste]
        posiciones = np.arange(len(contraste))
        with graficos.figura(
            estilo,
            titulo="Cambio de la precipitación anual respecto de la fase neutral",
            etiqueta_x="cambio (%)",
            alto_cm=graficos.alto_para_filas(len(contraste), estilo),
        ) as (fig, ax):
            for fase, desplazamiento in ((oni.FASE_NINO, -0.2),
                                         (oni.FASE_NINA, 0.2)):
                valores = [f.get(f"{fase}_pct") or 0.0 for f in contraste]
                ax.barh(posiciones + desplazamiento, valores, height=0.4,
                        color=color_de[fase], label=fase)
            ax.axvline(0.0, color=graficos.GRIS_CONTEXTO, linewidth=0.8)
            ax.set_yticks(posiciones)
            ax.set_yticklabels(etiquetas,
                               fontsize=max(3.0, estilo.tamano_fuente - 3))
            ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
            fig.tight_layout()
            for ruta in graficos.guardar(fig, directorio / "M05b_contraste",
                                         estilo):
                resultado.productos.append(rutas.relativa(ruta, base))

    logger.info("Figuras escritas en %s", rutas.relativa(directorio, base))


# =============================================================================
# Cierre
# =============================================================================
def _resumir(resultado, configuracion) -> list[Hallazgo]:
    """Informativos de síntesis."""
    hallazgos = [Hallazgo(
        INFORMATIVO, "enso.clasificacion",
        f"{resultado.temporadas:,} temporada(s) clasificadas: "
        + ", ".join(f"{k} {v}" for k, v in resultado.por_fase.items())
        + f". Se cruzaron con {resultado.estaciones} estacion(es) y "
        f"{resultado.periodos} periodo(s).",
    )]
    contraste = contraste_entre_fases(resultado.totales)
    valores = [f["nino_pct"] for f in contraste if f.get("nino_pct") is not None]
    if valores:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "enso.contraste",
            f"bajo Nino la precipitacion anual cambia entre {min(valores):+.0f}% "
            f"y {max(valores):+.0f}% respecto de la neutral, sobre "
            f"{len(valores)} estacion(es) con ambos totales completos.",
        ))
    if configuracion.obtener("enso.elimina_registros"):
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "enso.elimina_registros",
            "enso.elimina_registros esta en verdadero. CLAUDE.md, seccion 6, "
            "cierra que el ENSO no elimina estaciones ni registros: solo "
            "clasifica.",
        ))
    return hallazgos


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

    if ruta_json is None:
        ruta_json = rutas.directorio("procesado", base, crear=True) / \
            "M05b_enso.json"
    reporte = {
        "modulo": MODULO,
        "temporadas": resultado.temporadas,
        "por_fase": resultado.por_fase,
        "estaciones": resultado.estaciones,
        "periodos": resultado.periodos,
        "sin_clasificar": resultado.sin_clasificar,
        "totales_por_fase": resultado.totales,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=2),
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


# =============================================================================
# Interfaz de línea de comandos
# =============================================================================
def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(
        prog="M05b_enso.py",
        description="Clasificacion ENSO-ONI y agregaciones por fase.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--sin-descarga", action="store_true",
                            dest="sin_descarga",
                            help="Usa la copia local del indice ONI.")
    analizador.add_argument("--sin-graficas", action="store_true",
                            dest="sin_graficas")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida,
            descargar=not argumentos.sin_descarga,
            con_graficas=not argumentos.sin_graficas,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorRutas, ErrorConfiguracion, ErrorFormato) as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorHidrologia as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR


if __name__ == "__main__":
    sys.exit(main())
