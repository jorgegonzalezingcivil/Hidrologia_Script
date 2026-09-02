#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Estilo y primitivas de graficación, compartidos por los módulos de análisis
===========================================================================
Entorno: venv del proyecto.

Por qué existe. CLAUDE.md no asigna la generación de gráficas a ningún módulo:
el bloque 'informe' de config.yaml define cómo el M15 las numera y rotula, y el
bloque 'cartografia' corresponde al M16, que son mapas. La generación quedaba sin
dueño. Se resuelve aquí, con el mismo criterio que separa src/comun de src/sig:

    src/comun/    solo librería estándar y PyYAML   ambos entornos
    src/sig.py    depende de QGIS                    módulos SIG
    src/graficos.py  depende de matplotlib           módulos de análisis

Este archivo NO puede importarse desde src/comun. El Python de QGIS comparte ese
paquete y no tiene por qué disponer de matplotlib.

Un módulo SIG SÍ puede importarlo cuando su intérprete lo provea: el de QGIS
4.2.0 trae matplotlib 3.10.9. Eso es deseable, porque es lo que hace que las
figuras del estudio compartan un solo aspecto vengan del entorno que vengan. La
importación debe hacerse dentro de la función y degradar con una advertencia si
falta, para que la ausencia de matplotlib no impida producir el resto.

Qué resuelve. Un único punto de estilo. Si cada módulo llamara a matplotlib por
su cuenta, las figuras del informe saldrían con tipografías, tamaños y colores
distintos, y esa inconsistencia es visible en un entregable de consultoría. Aquí
viven la paleta, los tamaños, la rejilla y los formatos de salida, todos leídos
de config.yaml, y ningún módulo fija ninguno por su cuenta.

Qué NO resuelve. La numeración por capítulo y el rótulo ('Gráfico 4-2') son del
M15, que selecciona las figuras y las coloca en el documento. Los módulos de
análisis emiten la figura con un nombre estable; el informe decide su lugar.

Las figuras se escriben en los formatos declarados. El vectorial sirve al
informe, que puede escalarlo sin pérdida; el mapa de bits sirve a la revisión
rápida y a los visores que no interpretan SVG.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import matplotlib
import numpy as np

# Backend sin ventana. Debe fijarse ANTES de importar pyplot: los módulos corren
# sin sesión gráfica y cualquier backend interactivo fallaría o abriría ventanas
# durante una ejecución desatendida.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Polygon as ParchePoligono  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

__all__ = [
    "Estilo",
    "figura",
    "guardar",
    "lineas",
    "barras_horizontales",
    "histograma",
    "rampa",
    "dispersion_sobre_area",
    "marco_geografico",
    "coropleta",
    "barra_de_color",
    "barras_de_rango",
    "mapa_calor",
    "matriz_faltantes",
    "cajas_por_grupo",
    "curva_doble_masa",
    "transformador",
    "rotular_en_miles",
    "CM_POR_PULGADA",
    "directorio_tema",
    "estilo_individual",
    "ErrorGraficos",
]

class ErrorGraficos(RuntimeError):
    """Falla de graficación que el módulo debe reportar, no ocultar."""


CM_POR_PULGADA = 2.54

# Color de los elementos de contexto: rejilla, marcos, referencias. No sale de la
# paleta porque no identifica ninguna serie.
GRIS_CONTEXTO = "#9a9a9a"


@dataclass(frozen=True)
class Estilo:
    """
    Parámetros de presentación, todos procedentes de config.yaml.

    Ningún módulo debe construir un Estilo a mano fuera de las pruebas: la
    doctrina (CLAUDE.md, sección 2) prohíbe los parámetros embebidos, y el
    aspecto de las figuras del informe es un parámetro como cualquier otro.
    """

    dpi: int = 300
    ancho_cm: float = 16.0
    alto_cm: float = 10.0
    tamano_fuente: float = 9.0
    tipografia: str = "DejaVu Sans"
    paleta: tuple[str, ...] = ("#1f4e79",)
    formatos: tuple[str, ...] = ("png",)
    rejilla: bool = True
    rampa: str = "Blues"

    @classmethod
    def desde_config(cls, configuracion: Any) -> "Estilo":
        """Construye el estilo declarado en el bloque 'graficos'."""
        return cls(
            dpi=int(configuracion.obtener("graficos.dpi")),
            ancho_cm=float(configuracion.obtener("graficos.ancho_cm")),
            alto_cm=float(configuracion.obtener("graficos.alto_cm")),
            tamano_fuente=float(configuracion.obtener("graficos.tamano_fuente")),
            tipografia=str(configuracion.obtener("graficos.tipografia")),
            paleta=tuple(configuracion.obtener("graficos.paleta")),
            formatos=tuple(configuracion.obtener("graficos.formatos")),
            rejilla=bool(configuracion.obtener("graficos.rejilla")),
            rampa=str(configuracion.obtener("graficos.rampa")),
        )

    @property
    def tamano_pulgadas(self) -> tuple[float, float]:
        return (self.ancho_cm / CM_POR_PULGADA, self.alto_cm / CM_POR_PULGADA)

    def color(self, indice: int) -> str:
        """Color de la serie n, dando la vuelta a la paleta si hace falta."""
        if not self.paleta:
            return GRIS_CONTEXTO
        return self.paleta[indice % len(self.paleta)]


def directorio_tema(raiz: Path, tema: str) -> Path:
    """
    Carpeta de un tema dentro del arbol de figuras por estacion.

    Se agrupa por TEMA y no por modulo porque asi es como se redacta: quien
    escribe el capitulo de consistencia busca todas las curvas de doble masa
    juntas, no las del M05 mezcladas por tipo.
    """
    destino = Path(raiz) / tema
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def estilo_individual(estilo: Estilo, ancho_cm: float = 0.0,
                      alto_cm: float = 0.0) -> Estilo:
    """
    Variante del estilo para una figura de una sola estacion.

    Conserva paleta, tipografia y formatos; solo cambia el tamano. Una figura
    por estacion ocupa media pagina, no el ancho util completo, y con el tamano
    de las agregadas saldria con las marcas diminutas.
    """
    from dataclasses import replace
    return replace(
        estilo,
        ancho_cm=ancho_cm or estilo.ancho_cm * 0.7,
        alto_cm=alto_cm or estilo.alto_cm * 0.8,
    )


def rampa(cuantos: int, estilo: Estilo, invertir: bool = False) -> list[str]:
    """
    Colores para categorías ORDINALES, de claro a oscuro.

    La paleta de identificación no sirve aquí. Sus colores son categóricos, sin
    orden entre ellos, y aplicados a categorías ordenadas producen una lectura
    falsa: en la figura de cobertura, el primer y el último grupo salían ambos
    en tonos de azul y se confundían pese a ser los extremos opuestos.
    """
    if cuantos <= 0:
        return []
    mapa = plt.get_cmap(estilo.rampa)
    # Se recorta el extremo claro: por debajo de 0.35 los puntos desaparecen
    # sobre fondo blanco.
    posiciones = [0.35 + 0.65 * (i / max(1, cuantos - 1)) for i in range(cuantos)]
    if invertir:
        posiciones.reverse()
    return [matplotlib.colors.to_hex(mapa(p)) for p in posiciones]


def transformador(crs_origen: str, crs_destino: str):
    """
    Devuelve una función que reproyecta (x, y) de un sistema a otro.

    La reproyección es SIEMPRE explícita (CLAUDE.md, sección 5): quien dibuja
    declara de dónde y adónde, y nada se convierte por defecto. Se apoya en
    pyproj, que es la misma PROJ que usa QGIS, en lugar de programar la
    proyección a mano: una fórmula de Gauss-Krüger escrita para la ocasión no
    es defendible ante interventoría.

    Si origen y destino coinciden, devuelve la identidad y no consulta PROJ.
    """
    if not crs_destino or crs_origen == crs_destino:
        return lambda x, y: (x, y)
    try:
        from pyproj import Transformer
    except ImportError as exc:  # pragma: no cover
        raise ErrorGraficos(
            f"se pidió reproyectar de {crs_origen} a {crs_destino} y pyproj no "
            f"está instalado ({exc}). Ver requirements.txt."
        ) from exc
    # always_xy fija el orden a (este, norte) y (longitud, latitud), que es el
    # que usa el resto del proyecto. Sin él, algunos sistemas devuelven la
    # latitud primero y las figuras salen giradas noventa grados.
    conversor = Transformer.from_crs(crs_origen, crs_destino, always_xy=True)
    return lambda x, y: conversor.transform(x, y)


def rotular_en_miles(ax: Any, decimales: int = 0, maximo_marcas: int = 0) -> None:
    """
    Separador de miles en ambos ejes.

    Una coordenada plana ronda el millón de metros, y sin separador el rótulo
    se lee mal justo donde importa distinguir la cifra.
    """
    def como_miles(valor, _posicion):
        return f"{valor:,.{decimales}f}".replace(",", " ")

    ax.xaxis.set_major_formatter(FuncFormatter(como_miles))
    ax.yaxis.set_major_formatter(FuncFormatter(como_miles))
    if maximo_marcas:
        # Una coordenada plana ocupa siete cifras: en un panel estrecho, las
        # marcas se solapan y el rotulo deja de leerse.
        ax.xaxis.set_major_locator(MaxNLocator(nbins=maximo_marcas))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=maximo_marcas))


def barras_de_rango(
    ax: Any,
    etiquetas: Sequence[str],
    rangos: Sequence[tuple[float, float] | None],
    tramos: Sequence[Sequence[tuple[float, float]]],
    estilo: Estilo,
    alto_barra: float = 0.68,
    color_rango: str | None = None,
) -> None:
    """
    Dibuja, por fila, el rango declarado y los tramos con dato dentro de él.

    Reproduce el Gráfico 3-1 del informe de referencia, que titula 'Longitud
    Teórica' porque se construye con las fechas de instalación y suspensión del
    catálogo. Aquí se superpone además el dato REAL: el contorno es lo que la
    estación debería cubrir y el relleno es lo que efectivamente cubre. La
    diferencia entre ambos es el diagnóstico, y en el gráfico original no se ve.
    """
    tono_rango = color_rango or GRIS_CONTEXTO
    tono_dato = estilo.color(0)
    for fila, rango in enumerate(rangos):
        if rango is None:
            continue
        inicio, fin = rango
        ax.broken_barh(
            [(inicio, max(fin - inicio, 0.5))],
            (fila - alto_barra / 2, alto_barra),
            facecolors="none", edgecolors=tono_rango, linewidth=0.7, zorder=2,
        )
    for fila, piezas in enumerate(tramos):
        if piezas:
            ax.broken_barh(list(piezas), (fila - alto_barra / 2, alto_barra),
                           facecolors=tono_dato, edgecolor="none", zorder=3)
    ax.set_yticks(range(len(etiquetas)))
    ax.set_yticklabels(etiquetas, fontsize=max(3.0, estilo.tamano_fuente - 3))
    ax.set_ylim(-1, len(etiquetas))
    ax.invert_yaxis()


def _aplicar(ax: Any, estilo: Estilo, titulo: str, x: str, y: str) -> None:
    """Aspecto común de todos los ejes."""
    if titulo:
        ax.set_title(titulo, fontsize=estilo.tamano_fuente + 1)
    if x:
        ax.set_xlabel(x, fontsize=estilo.tamano_fuente)
    if y:
        ax.set_ylabel(y, fontsize=estilo.tamano_fuente)
    ax.tick_params(labelsize=estilo.tamano_fuente - 1)
    if estilo.rejilla:
        ax.grid(True, color=GRIS_CONTEXTO, linewidth=0.4, alpha=0.5)
        ax.set_axisbelow(True)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)


@contextmanager
def figura(
    estilo: Estilo,
    titulo: str = "",
    etiqueta_x: str = "",
    etiqueta_y: str = "",
    filas: int = 1,
    columnas: int = 1,
    alto_cm: float | None = None,
) -> Iterator[tuple[Any, Any]]:
    """
    Prepara una figura con el estilo declarado y la cierra al terminar.

    Cerrarla importa: un módulo que produce decenas de figuras sin liberar sus
    recursos agota la memoria del proceso, que es el defecto que arrastraba la
    rutina heredada.

    Entrega la figura y los ejes. Con una sola celda entrega el eje directamente;
    con varias, el arreglo que devuelve matplotlib.
    """
    alto = estilo.alto_cm if alto_cm is None else alto_cm
    previo = dict(plt.rcParams)
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": [estilo.tipografia, "DejaVu Sans"],
        "font.size": estilo.tamano_fuente,
        "figure.dpi": estilo.dpi,
        "savefig.bbox": "tight",
    })
    fig, ejes = plt.subplots(
        filas, columnas, figsize=(estilo.ancho_cm / CM_POR_PULGADA,
                                  alto / CM_POR_PULGADA),
        squeeze=False,
    )
    try:
        if filas == 1 and columnas == 1:
            _aplicar(ejes[0][0], estilo, titulo, etiqueta_x, etiqueta_y)
            yield fig, ejes[0][0]
        else:
            if titulo:
                fig.suptitle(titulo, fontsize=estilo.tamano_fuente + 2)
            yield fig, ejes
    finally:
        plt.close(fig)
        plt.rcParams.update(previo)


def guardar(fig: Any, destino_sin_extension: Path, estilo: Estilo) -> list[Path]:
    """
    Escribe la figura en cada formato declarado y devuelve las rutas escritas.

    El destino se da sin extensión porque el conjunto de formatos es una
    decisión de configuración, no del módulo que dibuja.

    La escritura es REPRODUCIBLE: dos ejecuciones con los mismos datos producen
    archivos byte a byte idénticos. Por defecto matplotlib incrusta en el SVG la
    fecha de creación y unos identificadores aleatorios, de modo que cada
    corrida marcaba como modificadas las cuatrocientas y pico figuras del
    entregable aunque ninguna hubiera cambiado. Con varias personas ejecutando,
    ese ruido esconde el cambio real y termina confirmándose sin revisar.
    """
    destino_sin_extension.parent.mkdir(parents=True, exist_ok=True)
    escritas: list[Path] = []
    for formato in estilo.formatos:
        ruta = destino_sin_extension.with_suffix(f".{formato}")
        argumentos: dict[str, Any] = {}
        if formato == "svg":
            # La sal fija los identificadores internos; el nombre del archivo
            # la hace distinta entre figuras y estable entre ejecuciones.
            plt.rcParams["svg.hashsalt"] = destino_sin_extension.name
            argumentos["metadata"] = {"Date": None}
        elif formato == "pdf":
            argumentos["metadata"] = {"CreationDate": None}
        fig.savefig(ruta, format=formato, dpi=estilo.dpi, bbox_inches="tight",
                    **argumentos)
        escritas.append(ruta)
    return escritas


# =============================================================================
# Primitivas
# =============================================================================
def lineas(
    ax: Any,
    series: dict[str, tuple[Sequence[float], Sequence[float]]],
    estilo: Estilo,
    marcador: str = "o",
    discontinuas: Iterable[str] = (),
    leyenda: bool = True,
) -> None:
    """
    Traza varias series sobre un mismo eje.

    Las nombradas en 'discontinuas' se dibujan con trazo partido y el mismo
    color que su homóloga continua, para representar una variante de la misma
    magnitud (por ejemplo, el subconjunto con años consecutivos) sin duplicar
    colores ni sugerir que se trata de otra serie.
    """
    partidas = set(discontinuas)
    color_de: dict[str, str] = {}
    indice = 0
    for nombre in series:
        base = nombre.split(" (")[0]
        if base not in color_de:
            color_de[base] = estilo.color(indice)
            indice += 1
    for nombre, (x, y) in series.items():
        base = nombre.split(" (")[0]
        ax.plot(
            x, y,
            color=color_de[base],
            linestyle="--" if nombre in partidas else "-",
            marker="" if nombre in partidas else marcador,
            markersize=3.5, linewidth=1.4, label=nombre,
        )
    # 'leyenda' es aqui el parametro booleano y tapa a la funcion del mismo
    # nombre, de modo que se llama por el alias.
    if leyenda and series:
        _colocar_leyenda(ax, estilo)


def barras_horizontales(
    ax: Any,
    etiquetas: Sequence[str],
    tramos: Sequence[Sequence[tuple[float, float]]],
    estilo: Estilo,
    color: str | None = None,
    alto_barra: float = 0.72,
) -> None:
    """
    Dibuja, por fila, los tramos ocupados sobre un eje continuo.

    Cada tramo es una pareja (inicio, longitud). Sirve para representar
    disponibilidad: los huecos quedan a la vista sin necesidad de leerlos en una
    tabla, que es justamente lo que distingue una serie continua de una partida.
    """
    tono = color or estilo.color(0)
    for fila, piezas in enumerate(tramos):
        if piezas:
            ax.broken_barh(list(piezas), (fila - alto_barra / 2, alto_barra),
                           facecolors=tono, edgecolor="none")
    ax.set_yticks(range(len(etiquetas)))
    ax.set_yticklabels(etiquetas, fontsize=max(3.0, estilo.tamano_fuente - 3))
    ax.set_ylim(-1, len(etiquetas))
    ax.invert_yaxis()


def histograma(
    ax: Any,
    grupos: dict[str, Sequence[float]],
    estilo: Estilo,
    casillas: int = 20,
    referencia: float | None = None,
    etiqueta_referencia: str = "",
    escala_log: bool = False,
) -> None:
    """
    Superpone la distribución de uno o varios grupos.

    La referencia vertical marca el umbral adoptado, de modo que la figura
    muestre a la vez la distribución medida y el corte aplicado sobre ella.
    """
    for indice, (nombre, valores) in enumerate(grupos.items()):
        if not len(valores):
            continue
        ax.hist(
            list(valores), bins=casillas, alpha=0.55,
            color=estilo.color(indice), label=f"{nombre} (n={len(valores):,})",
            edgecolor="white", linewidth=0.4,
        )
    if referencia is not None:
        ax.axvline(referencia, color="#c00000", linestyle="--", linewidth=1.3)
        if etiqueta_referencia:
            ax.annotate(
                etiqueta_referencia, xy=(referencia, 1), xycoords=("data", "axes fraction"),
                xytext=(4, -10), textcoords="offset points",
                fontsize=estilo.tamano_fuente - 1, color="#c00000",
            )
    if escala_log:
        # Sin ella, la masa de años completos aplasta la cola de años
        # parciales, que es justamente lo que el umbral recorta.
        ax.set_yscale("log")
    if grupos:
        leyenda(ax, estilo)


def marco_geografico(
    ax: Any,
    estilo: Estilo,
    corrientes: Sequence[Sequence[tuple[float, float]]] = (),
    destacadas: Sequence[Sequence[tuple[float, float]]] = (),
    punto: tuple[float, float] | None = None,
    cuenca: dict | None = None,
    etiqueta_cuenca: str = "superficie que drena al punto",
    etiqueta_punto: str = "punto de descarga",
    etiqueta_destacadas: str = "drena al punto",
    etiqueta_corrientes: str = "red de drenaje",
) -> None:
    """
    Dibuja el contexto geográfico sobre un mapa: corrientes y punto de salida.

    Un mapa de estaciones sin la red ni el punto obliga a quien lo revisa a
    situar los puntos de memoria. Con la red se ve de inmediato si un hueco de
    cobertura cae sobre una cabecera que aporta o sobre terreno que no drena
    al punto, que es la diferencia que importa.

    Las corrientes DESTACADAS son las que drenan al punto de salida. Se separan
    de las demás porque el área de influencia es un rectángulo con holgura y
    contiene red que no aporta: sin esa distinción, el mapa sugeriría una
    cuenca mayor que la real.

    Todo llega ya en las coordenadas de la figura. Esta función no reproyecta:
    quien dibuja declara el origen y el destino (CLAUDE.md, sección 5).
    """
    # La superficie va PRIMERO, debajo de todo: es contexto, no dato.
    if cuenca is not None and cuenca.get("mascara") is not None:
        mascara = cuenca["mascara"]
        alto, ancho = mascara.shape
        equis = cuenca["x0"] + cuenca["paso_m"] * np.arange(ancho)
        griegas = cuenca["y0"] + cuenca["paso_m"] * np.arange(alto)
        ax.contourf(equis, griegas, mascara.astype(float), levels=[0.5, 1.5],
                    colors=["#e8eef4"], zorder=0)
        ax.contour(equis, griegas, mascara.astype(float), levels=[0.5],
                   colors=["#7a97b5"], linewidths=1.4, zorder=3)
        ax.plot([], [], color="#7a97b5", linewidth=1.4, label=etiqueta_cuenca)

    for indice, grupo in enumerate((corrientes, destacadas)):
        if not grupo:
            continue
        destacada = indice == 1
        primera = True
        for linea in grupo:
            if len(linea) < 2:
                continue
            ax.plot([p[0] for p in linea], [p[1] for p in linea],
                    color="#4a7ba7" if destacada else "#b9c8d6",
                    linewidth=1.1 if destacada else 0.5,
                    zorder=2 if destacada else 1,
                    label=(etiqueta_destacadas if destacada
                           else etiqueta_corrientes) if primera else None)
            primera = False

    if punto is not None:
        ax.plot([punto[0]], [punto[1]], marker="v", markersize=9,
                color="#c00000", markeredgecolor="white", markeredgewidth=0.8,
                linestyle="none", zorder=6, label=etiqueta_punto)


def coropleta(
    ax: Any,
    poligonos: Sequence[Sequence[Sequence[tuple[float, float]]]],
    valores: Sequence[float | None],
    estilo: Estilo,
    etiqueta: str = "",
    rampa_color: str = "",
    borde: str = "#ffffff",
) -> Any:
    """
    Rellena cada polígono según su valor y devuelve el mapeador para la barra.

    Es la representación que el informe de referencia usa para el número de
    curva y las pendientes: una tabla de ciento veinticinco filas no deja ver
    si los valores altos se agrupan en la cabecera o se reparten, y esa es
    justamente la pregunta que un revisor hace primero.

    Los polígonos SIN VALOR se dibujan en gris y no en el extremo de la rampa.
    Pintarlos como si fueran el mínimo los haría indistinguibles de un valor
    bajo real, que es la clase de confusión que este repositorio evita.

    Todo llega en las coordenadas de la figura: esta función no reproyecta.
    """
    from matplotlib.collections import PolyCollection

    numericos = [v for v in valores if v is not None]
    if not numericos:
        raise ErrorGraficos("ningún polígono trae valor con el que colorear.")
    minimo, maximo = min(numericos), max(numericos)
    if maximo == minimo:
        maximo = minimo + 1e-9

    caras, alturas, sin_valor = [], [], []
    for anillos, valor in zip(poligonos, valores):
        for anillo in anillos:
            if len(anillo) < 3:
                continue
            # SE RELLENA POR SENTIDO DE GIRO, no por posición. Una entidad de
            # varias piezas trae varios anillos EXTERIORES, y quedarse con el
            # primero deja las demás sin pintar: sobre esta capa son 26 de 151
            # anillos, que salían como huecos blancos en el mapa. El formato
            # distingue el contorno del hueco solo por el sentido, exterior
            # horario, y esa es la regla que hay que leer.
            area = sum(uno[0] * otro[1] - otro[0] * uno[1]
                       for uno, otro in zip(anillo, list(anillo[1:])
                                            + [anillo[0]])) / 2.0
            if area > 0:  # antihorario: es un hueco
                continue
            if valor is None:
                sin_valor.append([(x, y) for x, y in anillo])
            else:
                caras.append([(x, y) for x, y in anillo])
                alturas.append(float(valor))

    if sin_valor:
        ax.add_collection(PolyCollection(
            sin_valor, facecolors="#d9d9d9", edgecolors=borde,
            linewidths=0.2, zorder=1))
        ax.plot([], [], marker="s", linestyle="none", color="#d9d9d9",
                markersize=7, label="sin valor")

    coleccion = PolyCollection(caras, array=np.asarray(alturas),
                               cmap=rampa_color or estilo.rampa,
                               edgecolors=borde, linewidths=0.2, zorder=2)
    coleccion.set_clim(minimo, maximo)
    ax.add_collection(coleccion)
    ax.autoscale_view()
    ax.set_aspect("equal")
    if etiqueta:
        coleccion.set_label(etiqueta)
    return coleccion


def barra_de_color(fig: Any, ax: Any, mapeador: Any, estilo: Estilo,
                   etiqueta: str) -> Any:
    """Barra de color junto al mapa, con su magnitud rotulada."""
    barra = fig.colorbar(mapeador, ax=ax, fraction=0.035, pad=0.02)
    barra.set_label(etiqueta, fontsize=estilo.tamano_fuente)
    barra.ax.tick_params(labelsize=estilo.tamano_fuente - 1)
    return barra


def dispersion_sobre_area(
    ax: Any,
    poligonos: Sequence[Sequence[Sequence[tuple[float, float]]]],
    grupos: dict[str, tuple[Sequence[float], Sequence[float]]],
    estilo: Estilo,
    tamanos: dict[str, float] | None = None,
    ordinal: bool = False,
) -> None:
    """
    Sitúa puntos sobre el contorno de un área.

    El área se dibuja solo como contorno: el interés está en dónde quedan los
    puntos y qué zonas se vacían, no en el relleno. La escala se fuerza a
    equivalente para que la figura no deforme distancias, cosa que en una figura
    de cobertura induciría a error sobre los huecos.
    """
    for poligono in poligonos:
        for indice, anillo in enumerate(poligono):
            if len(anillo) < 3:
                continue
            ax.add_patch(ParchePoligono(
                [(float(x), float(y)) for x, y in anillo],
                closed=True, fill=False,
                edgecolor=GRIS_CONTEXTO if indice == 0 else "#cccccc",
                linewidth=1.0, zorder=1,
            ))
    colores = (rampa(len(grupos), estilo, invertir=True) if ordinal
               else [estilo.color(i) for i in range(len(grupos))])
    for indice, (nombre, (x, y)) in enumerate(grupos.items()):
        ax.scatter(
            list(x), list(y), s=(tamanos or {}).get(nombre, 16.0),
            color=colores[indice], label=nombre,
            edgecolor="white", linewidth=0.4, zorder=3 + indice,
        )
    ax.set_aspect("equal", adjustable="datalim")
    ax.autoscale_view()
    if grupos:
        ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False,
                  loc="best", markerscale=1.2)


def mapa_calor(
    ax: Any,
    matriz: Sequence[Sequence[float]],
    etiquetas: Sequence[str],
    estilo: Estilo,
    minimo: float = -1.0,
    maximo: float = 1.0,
    rampa: str | None = None,
    barra: bool = True,
) -> Any:
    """
    Matriz de correlaciones como mapa de calor.

    Conserva la figura que producía la rutina heredada EDA.py. Su utilidad no es
    leer un valor concreto sino ver la estructura: bloques de estaciones que se
    parecen entre sí y filas oscuras que delatan a la que no se parece a nadie,
    que es la candidata a quedar sin vecinas.

    La escala se fija de forma explícita entre 'minimo' y 'maximo' y no se deja
    al rango de los datos: si cada figura se autoescala, dos estudios no se
    pueden comparar y un mapa de correlaciones bajas parece uno de altas.
    """
    datos = np.asarray(matriz, dtype=float)
    imagen = ax.imshow(datos, cmap=rampa or estilo.rampa, vmin=minimo,
                       vmax=maximo, aspect="auto", interpolation="nearest")
    ax.set_xticks(range(len(etiquetas)))
    ax.set_yticks(range(len(etiquetas)))
    tamano = max(3.0, estilo.tamano_fuente - 4)
    ax.set_xticklabels(etiquetas, rotation=90, fontsize=tamano)
    ax.set_yticklabels(etiquetas, fontsize=tamano)
    ax.grid(False)
    if barra:
        barra_color = ax.figure.colorbar(imagen, ax=ax, fraction=0.046, pad=0.04)
        barra_color.ax.tick_params(labelsize=estilo.tamano_fuente - 2)
    return imagen


def matriz_faltantes(
    ax: Any,
    presente: Sequence[Sequence[bool]],
    etiquetas_columna: Sequence[str],
    estilo: Estilo,
    etiquetas_fila: Sequence[str] | None = None,
    paso_fila: int = 60,
) -> None:
    """
    Diagrama de datos faltantes, en la línea del que producía Impute.py.

    Cada columna es una estación y cada fila un periodo. El hueco se ve como
    banda clara, de modo que se distingue de un vistazo la estación con huecos
    dispersos de la que tiene un tramo entero sin registro: son problemas
    distintos y admiten soluciones distintas.
    """
    datos = np.asarray(presente, dtype=float)
    ax.imshow(datos, cmap="Greys", aspect="auto", interpolation="nearest",
              vmin=0.0, vmax=1.0)
    ax.set_xticks(range(len(etiquetas_columna)))
    ax.set_xticklabels(etiquetas_columna, rotation=90,
                       fontsize=max(3.0, estilo.tamano_fuente - 4))
    if etiquetas_fila is not None:
        posiciones = list(range(0, len(etiquetas_fila), max(1, paso_fila)))
        ax.set_yticks(posiciones)
        ax.set_yticklabels([etiquetas_fila[i] for i in posiciones],
                           fontsize=estilo.tamano_fuente - 2)
    ax.grid(False)


def cajas_por_grupo(
    ax: Any,
    grupos: dict[str, Sequence[float]],
    estilo: Estilo,
    marcados: dict[str, Sequence[float]] | None = None,
) -> None:
    """
    Diagrama de cajas por grupo, con los valores señalados superpuestos.

    Conserva el boxplot de EDA.py y le añade lo que aquel no mostraba: qué
    puntos quedaron marcados como anómalos. Ver la caja sin los marcados obliga
    a creer el conteo; verlos encima permite juzgar si son error o cola natural
    de la distribución.
    """
    nombres = list(grupos)
    datos = [list(grupos[n]) for n in nombres]
    cajas = ax.boxplot(datos, patch_artist=True, showfliers=False,
                       widths=0.6, tick_labels=nombres)
    for parche in cajas["boxes"]:
        parche.set_facecolor(estilo.color(0))
        parche.set_alpha(0.35)
        parche.set_edgecolor(estilo.color(0))
    for pieza in ("whiskers", "caps", "medians"):
        for linea in cajas[pieza]:
            linea.set_color(estilo.color(0))
    if marcados:
        for indice, nombre in enumerate(nombres, start=1):
            valores = list(marcados.get(nombre, ()))
            if not valores:
                continue
            ax.plot([indice] * len(valores), valores, linestyle="none",
                    marker="o", markersize=3.0, color="#c00000", alpha=0.7,
                    zorder=5)
    ax.tick_params(labelsize=estilo.tamano_fuente - 1)


def curva_doble_masa(
    ax: Any,
    acumulado_patron: Sequence[float],
    acumulado_estacion: Sequence[float],
    estilo: Estilo,
    indice_quiebre: int | None = None,
    razon: float | None = None,
) -> None:
    """
    Curva de doble masa de una estación contra el patrón de sus vecinas.

    Se dibuja además la recta de referencia que pasa por el origen con la
    pendiente del primer tramo: la separación de la curva respecto de esa recta
    es el quiebre, y sin ella el ojo tiende a ver recta donde hay codo.

    El quiebre se marca con su año para que la figura sea legible sin volver a
    la tabla.
    """
    x = np.asarray(acumulado_patron, dtype=float)
    y = np.asarray(acumulado_estacion, dtype=float)
    ax.plot(x, y, color=estilo.color(0), linewidth=1.3, marker="", zorder=3)
    if x.size > 1 and x[-1] > 0:
        if indice_quiebre is not None and 0 < indice_quiebre < x.size:
            pendiente = y[indice_quiebre] / x[indice_quiebre] if x[indice_quiebre] else 0.0
        else:
            pendiente = y[-1] / x[-1]
        ax.plot([0, x[-1]], [0, pendiente * x[-1]], color=GRIS_CONTEXTO,
                linewidth=0.9, linestyle="--", zorder=2)
    if indice_quiebre is not None and 0 <= indice_quiebre < x.size:
        ax.plot([x[indice_quiebre]], [y[indice_quiebre]], marker="o",
                markersize=5.0, color="#c00000", zorder=4)
        if razon is not None:
            ax.annotate(f"x{razon:.2f}", xy=(x[indice_quiebre], y[indice_quiebre]),
                        xytext=(6, -10), textcoords="offset points",
                        fontsize=estilo.tamano_fuente - 2, color="#c00000")


def leyenda(ax: Any, estilo: Estilo, manijas: Sequence[Any] | None = None,
            etiquetas: Sequence[str] | None = None, titulo: str = "",
            columnas: int | None = None) -> Any:
    """
    Leyenda DEBAJO del eje, fuera del area de dibujo.

    POR QUE NO SE DEJA EN AUTOMATICO. El modo 'best' de matplotlib busca el
    hueco mirando lineas, parches y colecciones, pero NO mira los textos que se
    anotan sobre la figura. En estas graficas se rotulan estaciones, subcuencas
    y periodos de retorno, de modo que 'best' encuentra un hueco en los datos y
    aterriza justo encima de los rotulos. Ese es el defecto que el consultor
    encontro repetido en el informe.

    POR QUE AL COSTADO Y NO DEBAJO. Debajo del eje tampoco hay sitio: muchas de
    estas figuras llevan una nota al pie con la procedencia del dato y los
    parametros adoptados, y esa nota se dibuja DESPUES de la leyenda, de modo
    que no hay forma de detectarla a tiempo para esquivarla. Se comprobo sobre
    la curva de duracion del M19: la leyenda aterrizaba encima de la nota.

    El costado derecho es el unico espacio que ninguna otra cosa ocupa, y la
    figura se guarda con bbox_inches='tight', asi que lo que sobresale ensancha
    el lienzo en lugar de recortarse.
    """
    if manijas is None:
        manijas, etiquetas_reales = ax.get_legend_handles_labels()
    else:
        etiquetas_reales = list(etiquetas or
                                [getattr(m, "get_label", lambda: "")()
                                 for m in manijas])
    if not manijas:
        return None
    if columnas is None:
        # Una columna, salvo que sean tantas entradas que la leyenda quede mas
        # alta que el propio dibujo.
        columnas = 2 if len(manijas) > 12 else 1
    return ax.legend(
        manijas, etiquetas_reales,
        loc="upper left", bbox_to_anchor=(1.01, 1.0),
        ncol=columnas, frameon=False, borderaxespad=0.0,
        fontsize=estilo.tamano_fuente - 1,
        title=titulo or None,
    )


# Alias para las funciones que reciben un parametro llamado 'leyenda', que de
# otro modo tapa a la funcion dentro de su cuerpo.
_colocar_leyenda = leyenda


def leyenda_manual(
    ax: Any, entradas: Sequence[tuple[str, str]], estilo: Estilo,
) -> None:
    """Leyenda construida a mano, para figuras cuyas marcas no la generan."""
    manijas = [Line2D([0], [0], color=color, linewidth=2.0, label=texto)
               for texto, color in entradas]
    leyenda(ax, estilo, manijas, [texto for texto, _ in entradas])


def tramos_consecutivos(valores: Iterable[int]) -> list[tuple[float, float]]:
    """
    Convierte un conjunto de años en tramos (inicio, longitud).

    Es la traducción entre la medida que produce el análisis, que es un conjunto
    de años sueltos, y lo que la barra necesita dibujar.
    """
    ordenados = sorted(set(int(v) for v in valores))
    if not ordenados:
        return []
    tramos: list[tuple[float, float]] = []
    inicio = previo = ordenados[0]
    for actual in ordenados[1:]:
        if actual != previo + 1:
            tramos.append((float(inicio), float(previo - inicio + 1)))
            inicio = actual
        previo = actual
    tramos.append((float(inicio), float(previo - inicio + 1)))
    return tramos


def alto_para_filas(filas: int, estilo: Estilo,
                    cm_por_fila: float = 0.30,
                    minimo_cm: float = 6.0) -> float:
    """Alto de una figura de barras, que debe crecer con el número de filas."""
    return max(minimo_cm, math.ceil(filas * cm_por_fila) + 2.0)
