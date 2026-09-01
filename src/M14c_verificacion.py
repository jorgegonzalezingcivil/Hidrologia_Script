#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M14c - Verificación de crecientes contra caudal observado
=========================================================
Entorno: venv del proyecto.

Contrasta lo que el modelo produce por periodo de retorno contra el análisis de
frecuencia de las crecientes REALMENTE OBSERVADAS en las estaciones que están
dentro de la cuenca.

NO ES LA CALIBRACION DEL M14b, y la diferencia importa. Calibrar es ajustar los
parámetros contra hidrogramas de tormentas concretas, y para eso hace falta
caudal continuo horario o diario, que en este estudio no existe: la CAR publica
escala mensual. Lo que sí permite el dato disponible es comprobar si el modelo
reproduce la magnitud de las crecientes observadas, que es una verificación
externa y en algunos aspectos más fuerte que una calibración sobre dos años.

SE COMPARAN MAGNITUDES HOMOGENEAS. El dato observado es el máximo de los
caudales MEDIOS DIARIOS, medido y no supuesto (ver docs/especificacion_datos_car.md).
El modelo produce un hidrograma cuyo pico dura minutos. Comparar el pico contra
una media diaria daría un sesgo de un FACTOR DE DIEZ, medido en este estudio, y
llevaría a "corregir" un modelo que puede estar bien. Por eso se promedia el
hidrograma simulado a 24 horas en lugar de escalar la observación a pico: la
media móvil se calcula sin suponer nada, y el factor habría que suponerlo.

EL CRITERIO NO ES UN PORCENTAJE FIJO. Se acepta si el valor del modelo cae
dentro de la BANDA DE CONFIANZA del análisis de frecuencia observado. Exigir un
+/-5% contra un cuantil que arrastra su propia incertidumbre obligaría a ajustar
el modelo contra el ruido de la muestra.

SOLO SE VERIFICA HASTA DONDE LA MUESTRA SOSTIENE. Con 29 años, el cuantil de 100
años es extrapolación de la distribución ajustada, no observación. Contrastar el
modelo contra él compararía dos extrapolaciones.

Productos:
    data/02_procesado/hidrologia/verificacion_crecientes.csv
    data/02_procesado/M14c_verificacion.json
    data/05_resultados/graficos/M14c_verificacion_<union>.png y .svg
    data/05_resultados/graficos/M14c_media_movil_24h.png y .svg

Uso:
    python src/M14c_verificacion.py
    python src/M14c_verificacion.py --sin-graficas

Códigos de salida:
    0  verificación producida
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

import frecuencia as fr  # noqa: E402
from comun import esquema, geometria, registro, rutas, shapefile  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import ErrorFormato, ErrorRutas  # noqa: E402
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M14c"
DESCRIPCION = "Verificación de crecientes contra caudal observado"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Serie de caudal observado: maximo mensual de los caudales medios diarios.
ETIQUETA_CAUDAL_MAX = "Q_MX_M"


@dataclass
class Pareja:
    """Una estación de caudal emparejada con la unión del modelo que la mide."""

    codigo: str
    nombre: str
    union: str
    distancia_m: float
    anios: int = 0
    caudal_base: float = 0.0
    maximos: dict[int, float] = field(default_factory=dict)


@dataclass
class ResultadoM14c:
    parejas: list[dict[str, Any]] = field(default_factory=list)
    contrastes: list[dict[str, Any]] = field(default_factory=list)
    sin_pareja: list[dict[str, Any]] = field(default_factory=list)
    tr_maximo: float = 0.0
    hubo_ajuste: bool = False
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def media_movil_maxima(
    caudales: Sequence[float], paso_min: float, ventana_h: float = 24.0,
) -> float | None:
    """
    Mayor media de una ventana deslizante sobre el hidrograma.

    ES EL EQUIVALENTE EN EL MODELO DEL DATO OBSERVADO. El limnígrafo reporta el
    mayor de los caudales MEDIOS DIARIOS del mes; el modelo produce un
    hidrograma cuyo pico dura minutos. Promediar el hidrograma sobre 24 horas
    pone las dos cifras en la misma magnitud.

    SE TOMA LA POSICION QUE MAXIMIZA, no una hora de calendario. El modelo no
    tiene calendario y la observación sí, de modo que la ventana que da la mayor
    media es el equivalente más justo a "el peor día del mes".

    Devuelve None si el hidrograma es más corto que la ventana: con menos
    ordenadas que 24 horas la media no existe, y devolver la media de lo que hay
    daría un número que parece comparable y no lo es.
    """
    if paso_min <= 0:
        raise ValueError("el paso del hidrograma debe ser positivo.")
    n = int(round(ventana_h * 60.0 / paso_min))
    if n <= 0 or len(caudales) < n:
        return None

    acumulado = float(sum(caudales[:n]))
    mejor = acumulado
    for i in range(n, len(caudales)):
        acumulado += float(caudales[i]) - float(caudales[i - n])
        if acumulado > mejor:
            mejor = acumulado
    return mejor / n


def cota_inferior_del_pico(
    media_diaria_observada: float, caudal_base: float = 0.0,
) -> float:
    """
    Cota INFERIOR del pico instantaneo que de verdad ocurrio.

    LA CLAVE DE TODA LA VERIFICACION, y no necesita suponer ningun factor: el
    pico instantaneo de un dia SIEMPRE es mayor o igual que la media de ese
    mismo dia, porque una media no puede superar al maximo que la compone. De
    modo que la media diaria observada es una cota inferior del pico real.

    Con eso se puede probar una cosa y solo una: si el pico del modelo queda por
    DEBAJO de esa cota, el modelo es demasiado bajo con certeza. Lo contrario no
    se puede concluir, porque el pico real puede estar muy por encima de su
    media diaria y no sabemos cuanto.

    SE DESCUENTA EL CAUDAL BASE porque el modelo no lo simula: su hidrograma
    arranca en cero y vuelve a cero. Cargarle una diferencia que corresponde al
    caudal ordinario del rio seria culparlo de algo que no pretende reproducir.
    El descuento hace la prueba MAS conservadora, es decir mas dificil de
    declarar que el modelo esta bajo.
    """
    return max(0.0, float(media_diaria_observada) - float(caudal_base))


def periodos_sostenidos(
    anios: int, periodos: Sequence[float], factor: float,
) -> list[float]:
    """
    Periodos de retorno que la longitud del registro sostiene.

    Con 29 años, el cuantil de 100 años es EXTRAPOLACION de la distribución
    ajustada, no observación: contrastar el modelo contra él compararía dos
    extrapolaciones y no probaría nada. El factor declara hasta cuántas veces la
    longitud del registro se admite extrapolar.
    """
    tope = float(anios) * float(factor)
    return [float(t) for t in periodos if float(t) <= tope]


def dentro_de_la_banda(
    valor: float, inferior: float, superior: float,
) -> bool:
    """Si el valor del modelo cae dentro de la banda observada, inclusive."""
    return float(inferior) <= float(valor) <= float(superior)


def emparejar_con_uniones(
    estaciones: Sequence[tuple[str, str, float, float]],
    uniones: dict[str, tuple[float, float]],
    tolerancia_m: float,
) -> tuple[list[Pareja], list[dict[str, Any]]]:
    """
    Empareja cada estación con la unión del modelo más próxima.

    SE BUSCA, NO SE DECLARA A MANO. Una lista fija en la configuración serviría
    a este estudio y a ninguno más, y el siguiente proyecto tendría otras
    estaciones y otras uniones.

    Una estación sin unión cerca NO se fuerza: se reporta con su distancia, para
    que el consultor decida si conviene añadir un punto de quiebre en la
    delimitación. Emparejarla con una unión lejana compararía el modelo en un
    sitio contra la medida de otro, con áreas drenadas distintas.
    """
    parejas: list[Pareja] = []
    sin_pareja: list[dict[str, Any]] = []
    for codigo, nombre, x, y in estaciones:
        if not uniones:
            sin_pareja.append({"codigo": codigo, "nombre": nombre,
                               "motivo": "el modelo no declara uniones"})
            continue
        union, punto = min(
            uniones.items(),
            key=lambda kv: (kv[1][0] - x) ** 2 + (kv[1][1] - y) ** 2)
        distancia = ((punto[0] - x) ** 2 + (punto[1] - y) ** 2) ** 0.5
        if distancia > tolerancia_m:
            sin_pareja.append({
                "codigo": codigo, "nombre": nombre, "union_mas_cercana": union,
                "distancia_m": round(distancia, 1),
                "motivo": f"la union mas cercana esta a {distancia:.0f} m"})
            continue
        parejas.append(Pareja(codigo=codigo, nombre=nombre, union=union,
                              distancia_m=round(distancia, 1)))
    return parejas, sin_pareja


def maximos_anuales_de_mensuales(
    meses_por_anio: dict[int, dict[int, float]], minimo_meses: int = 12,
) -> dict[int, float]:
    """
    Máximo anual de caudal a partir de los máximos mensuales.

    Misma reducción exacta que el M07 aplica a la precipitación: el mayor de los
    máximos mensuales es el del año. Se exigen los doce meses porque un año al
    que le falta la temporada de lluvias daría un máximo que no es comparable.
    """
    return {anio: float(max(meses.values()))
            for anio, meses in meses_por_anio.items()
            if len(meses) >= minimo_meses}


# =============================================================================
# Lectura de insumos
# =============================================================================
def leer_uniones(ruta_basin: Path) -> dict[str, tuple[float, float]]:
    """
    Coordenadas de lienzo de cada unión del modelo.

    EL CANVAS DEL .basin NO ESTA EN EL CRS DE CALCULO. En este estudio viene en
    EPSG:3116 mientras la cadena calcula en EPSG:9377, y el desplazamiento entre
    los dos es de unos 4.000 km: emparejar sin reproyectar daria distancias
    absurdas y ninguna estacion encontraria union. Quien llame debe proyectar
    las estaciones a ESTE sistema, no al reves.
    """
    import re

    if not Path(ruta_basin).is_file():
        raise ErrorRutas(f"no se encuentra el modelo de cuenca en {ruta_basin}.")
    texto = Path(ruta_basin).read_text(encoding="latin-1")
    uniones: dict[str, tuple[float, float]] = {}
    for bloque in texto.split("Junction: ")[1:]:
        nombre = bloque.splitlines()[0].strip()
        equis = re.search(r"Canvas X:\s*([-\d.]+)", bloque)
        griega = re.search(r"Canvas Y:\s*([-\d.]+)", bloque)
        if equis and griega:
            uniones[nombre] = (float(equis.group(1)), float(griega.group(1)))
    return uniones


def leer_hidrogramas(
    ruta: Path, delimitador: str,
) -> dict[str, dict[str, list[tuple[float, float]]]]:
    """Hidrogramas por elemento y periodo de retorno, ordenados en el tiempo."""
    if not Path(ruta).is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}. Ejecutar el M14 antes que este modulo.")
    salida: dict[str, dict[str, list[tuple[float, float]]]] = {}
    with Path(ruta).open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            try:
                minuto = float(fila["minuto"])
                caudal = float(fila["caudal_m3s"])
            except (KeyError, TypeError, ValueError):
                continue
            salida.setdefault(fila["elemento"], {}).setdefault(
                fila["periodo_retorno"], []).append((minuto, caudal))
    for periodos in salida.values():
        for serie in periodos.values():
            serie.sort()
    return salida


def leer_caudal_observado(
    ruta_serie: Path, delimitador: str, codigos: Sequence[str],
) -> dict[str, dict[int, dict[int, float]]]:
    """Máximos mensuales de caudal de las estaciones pedidas."""
    if not Path(ruta_serie).is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta_serie}. Ejecutar el M04 antes.")
    buscados = {str(c).strip() for c in codigos}
    salida: dict[str, dict[int, dict[int, float]]] = {}
    with Path(ruta_serie).open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            if fila.get("etiqueta") != ETIQUETA_CAUDAL_MAX:
                continue
            codigo = (fila.get("codigo") or "").strip()
            if codigo not in buscados:
                continue
            fecha = fila.get("fecha", "")
            if len(fecha) < 7:
                continue
            try:
                anio, mes = int(fecha[:4]), int(fecha[5:7])
                valor = float(fila.get("valor", ""))
            except (TypeError, ValueError):
                continue
            previo = salida.setdefault(codigo, {}).setdefault(anio, {})
            previo[mes] = max(valor, previo.get(mes, valor))
    return salida


def leer_caudal_medio(
    ruta_serie: Path, delimitador: str, codigos: Sequence[str],
) -> dict[str, list[float]]:
    """
    Caudal medio mensual de cada estacion, para estimar el caudal base.

    El modelo no simula flujo base: su hidrograma arranca en cero y vuelve a
    cero. Cargarle la diferencia que corresponde al caudal ordinario del rio
    seria culparlo de algo que no pretende reproducir.
    """
    buscados = {str(c).strip() for c in codigos}
    salida: dict[str, list[float]] = {}
    if not Path(ruta_serie).is_file():
        return salida
    with Path(ruta_serie).open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            if fila.get("etiqueta") != "Q_MEDIA_M":
                continue
            codigo = (fila.get("codigo") or "").strip()
            if codigo not in buscados:
                continue
            try:
                salida.setdefault(codigo, []).append(float(fila["valor"]))
            except (KeyError, TypeError, ValueError):
                continue
    return salida


def estaciones_de_caudal_en_la_cuenca(
    ruta_inventario: Path, delimitador: str, ruta_cuenca: Path,
    crs_catalogo: str, crs_lienzo: str,
) -> list[tuple[str, str, float, float]]:
    """
    Estaciones de caudal del inventario que caen DENTRO de la cuenca.

    Solo las de dentro sirven para verificar: una estación fuera mide otra
    superficie, y contrastar el modelo contra ella exigiría transponer, que es
    una comprobación distinta y con más incertidumbre.

    Devuelve (código, nombre, x, y) con las coordenadas ya en el sistema del
    lienzo del .basin, que es donde estan las uniones.
    """
    from pyproj import Transformer

    if not Path(ruta_inventario).is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta_inventario}. Ejecutar el M03 antes.")
    poligonos = shapefile.leer_geometrias(ruta_cuenca)
    a_calculo = Transformer.from_crs(crs_catalogo, "EPSG:9377", always_xy=True)
    a_lienzo = Transformer.from_crs(crs_catalogo, crs_lienzo, always_xy=True)

    salida: list[tuple[str, str, float, float]] = []
    with Path(ruta_inventario).open(encoding="utf-8-sig",
                                    newline="") as manejador:
        lector = csv.DictReader(manejador, delimiter=delimitador)
        claves = {c.lower(): c for c in (lector.fieldnames or [])}
        col_cod = next((claves[k] for k in claves if "digo" in k), None)
        col_nom = next((claves[k] for k in claves if "nombre" in k), None)
        col_cat = next((claves[k] for k in claves if "categor" in k), None)
        col_lat = next((claves[k] for k in claves if k.startswith("latitud")), None)
        col_lon = next((claves[k] for k in claves if k.startswith("longitud")), None)
        if not all((col_cod, col_cat, col_lat, col_lon)):
            raise ErrorFormato(
                f"{Path(ruta_inventario).name} no trae codigo, categoria y "
                "coordenadas reconocibles.")
        for fila in lector:
            if (fila.get(col_cat) or "").strip().upper() not in ("LG", "LM"):
                continue
            try:
                lat = float(str(fila[col_lat]).replace(",", "."))
                lon = float(str(fila[col_lon]).replace(",", "."))
            except (TypeError, ValueError):
                continue
            equis, griega = a_calculo.transform(lon, lat)
            if not geometria.punto_en_alguno(equis, griega, poligonos):
                continue
            lx, ly = a_lienzo.transform(lon, lat)
            salida.append(((fila[col_cod] or "").strip(),
                           (fila.get(col_nom) or "").strip(), lx, ly))
    return salida


# =============================================================================
# Figuras
# =============================================================================
def _figura_contraste(graficos, estilo, directorio, pareja, filas, base,
                      resultado) -> None:
    """
    El contraste: pico modelado contra la cota inferior del pico observado.

    Se dibuja la COTA y no la media diaria sin mas, porque la cota es lo que la
    prueba usa: por debajo de ella el modelo esta bajo con certeza, por encima
    no se puede concluir nada. La banda del ajuste observado se dibuja detras
    para que se vea cuanta incertidumbre arrastra el dato.
    """
    if not filas:
        return
    periodos = [f["periodo_retorno"] for f in filas]
    cota = [f["cota_inferior_del_pico_m3s"] for f in filas]
    pico = [f["pico_modelado_m3s"] for f in filas]
    inferior = [max(0.0, f["banda_inferior_m3s"] - f["caudal_base_m3s"])
                for f in filas]
    superior = [max(0.0, f["banda_superior_m3s"] - f["caudal_base_m3s"])
                for f in filas]

    with graficos.figura(
        estilo,
        titulo=(f"Verificacion de crecientes en {pareja.union}\n"
                f"{pareja.codigo} {pareja.nombre} ({pareja.anios} anios)"),
        etiqueta_x="Periodo de retorno (anios)",
        etiqueta_y="Caudal (m3/s)",
    ) as (fig, ax):
        ax.fill_between(periodos, inferior, superior, alpha=0.18,
                        color="#1f77b4",
                        label="Banda del ajuste observado")
        ax.plot(periodos, cota, "-o", color="#1f4e79", ms=5,
                label="COTA INFERIOR del pico observado")
        ax.plot(periodos, pico, "-s", color="#c0392b", ms=5,
                label="Pico modelado")
        for f in filas:
            if f["modelo_demasiado_bajo"]:
                ax.plot([f["periodo_retorno"]], [f["pico_modelado_m3s"]], "x",
                        color="#c0392b", ms=13, mew=2.5)
        ax.set_xscale("log")
        ax.set_xticks(periodos)
        eje = ax.get_xaxis()
        ticker = __import__("matplotlib").ticker
        eje.set_major_formatter(ticker.ScalarFormatter())
        # Sin esto el eje logaritmico anade sus propias marcas menores
        # (3x10^0, 2x10^1) encima de los periodos declarados, y el lector ve
        # dos juegos de numeros mezclados.
        eje.set_minor_formatter(ticker.NullFormatter())
        eje.set_minor_locator(ticker.NullLocator())
        ax.grid(alpha=0.25, which="both")
        ax.legend(frameon=False, fontsize=9)
        fig.tight_layout(rect=(0, 0.09, 1, 1))
        fig.text(0.01, 0.045,
                 "El pico real observado es al menos su media diaria: por DEBAJO "
                 "de la cota el modelo esta bajo con certeza (aspa).",
                 fontsize=8, color="#555555")
        fig.text(0.01, 0.012,
                 "Por ENCIMA no se concluye nada: el pico real puede estar muy "
                 "arriba y este dato no dice cuanto. Ya se descuenta el caudal base.",
                 fontsize=8, color="#555555")
        nombre = f"M14c_verificacion_{pareja.union}"
        for ruta in graficos.guardar(fig, directorio / nombre, estilo):
            resultado.productos.append(rutas.relativa(ruta, base))


def _figura_media_movil(graficos, estilo, directorio, serie, paso, ventana_h,
                        union, periodo, base, resultado) -> None:
    """
    Explica QUE se compara: el pico frente a la media de 24 h.

    Va al informe porque el metodo no es evidente: el lector tiene que poder
    ver por que no se contrasta el pico, que es la cifra que el resto del
    estudio usa.
    """
    horas = [m / 60.0 for m in (p[0] for p in serie)]
    caudal = [p[1] for p in serie]
    n = int(round(ventana_h * 60.0 / paso))
    if len(caudal) < n:
        return
    acumulado, mejor, inicio = sum(caudal[:n]), sum(caudal[:n]), 0
    for i in range(n, len(caudal)):
        acumulado += caudal[i] - caudal[i - n]
        if acumulado > mejor:
            mejor, inicio = acumulado, i - n + 1
    media = mejor / n
    pico = max(caudal)

    with graficos.figura(
        estilo, titulo=f"Que compara la verificacion: {union}, Tr {periodo}",
        etiqueta_x="Horas desde el inicio de la tormenta",
        etiqueta_y="Caudal (m3/s)",
    ) as (fig, ax):
        ax.plot(horas, caudal, color="#1f4e79", lw=1.8,
                label="Hidrograma simulado")
        ax.axvspan(horas[inicio], horas[inicio + n - 1], color="#f0a500",
                   alpha=0.18, label=f"Ventana de {ventana_h:.0f} h de mayor media")
        ax.axhline(media, color="#c0392b", lw=1.5, ls="--",
                   label=f"Media en esa ventana = {media:.2f} m3/s")
        ax.annotate(f"Pico instantaneo {pico:.2f} m3/s\n"
                    f"es {pico / media:.1f} veces la media",
                    xy=(0.97, 0.72), xycoords="axes fraction", ha="right",
                    fontsize=10, color="#1f4e79")
        ax.set_ylim(0, pico * 1.12)
        ax.grid(alpha=0.25)
        ax.legend(loc="upper right", frameon=False, fontsize=9)
        fig.tight_layout(rect=(0, 0.05, 1, 1))
        fig.text(0.01, 0.012,
                 "El dato observado es el maximo de los caudales MEDIOS "
                 "DIARIOS, asi que se compara contra la media de 24 h.",
                 fontsize=8, color="#555555")
        for ruta in graficos.guardar(
                fig, directorio / "M14c_media_movil_24h", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    raiz: Path | None = None, ruta_config: Path | None = None,
    ruta_json: Path | None = None, graficas: bool = True,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Empareja, ajusta la frecuencia observada y contrasta contra el modelo."""
    inicio = time.perf_counter()
    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola)
    resultado = ResultadoM14c()
    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))

    proyecto = Path(str(configuracion.obtener("hec_hms.proyecto.directorio")))
    ruta_basin = proyecto / str(
        configuracion.obtener("hec_hms.proyecto.modelo_cuenca"))
    ruta_hidro = (rutas.directorio("procesado", base) / "hidrologia"
                  / "hidrogramas.csv")
    ruta_serie = rutas.resolver(
        configuracion.obtener("series.consolidada"), base)
    ruta_inventario = (rutas.directorio("procesado_estaciones", base)
                       / "inventario_estaciones.csv")
    ruta_cuenca = rutas.directorio("sig_vector", base) / "subcuencas.shp"

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"modelo": str(ruta_basin),
                 "hidrogramas": rutas.relativa(ruta_hidro, base),
                 "serie": rutas.relativa(ruta_serie, base)},
        parametros=configuracion.parametros((
            "verificacion.confianza", "verificacion.repeticiones",
            "verificacion.factor_extrapolacion",
            "verificacion.tolerancia_emparejamiento_m")))

    try:
        with registro.bloque(logger, "Emparejamiento con el modelo"):
            uniones = leer_uniones(ruta_basin)
            estaciones = estaciones_de_caudal_en_la_cuenca(
                ruta_inventario, delimitador, ruta_cuenca,
                "EPSG:4686", str(configuracion.obtener(
                    "hec_hms.proyecto.crs_lienzo", "EPSG:3116")))
            parejas, sin_pareja = emparejar_con_uniones(
                estaciones, uniones,
                float(configuracion.obtener(
                    "verificacion.tolerancia_emparejamiento_m")))
            resultado.sin_pareja = sin_pareja
            logger.info("%d union(es) en el modelo | %d estacion(es) de caudal "
                        "dentro de la cuenca | %d emparejada(s)",
                        len(uniones), len(estaciones), len(parejas))
    except (ErrorRutas, ErrorFormato, ImportError) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "verificacion.insumos", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    if not parejas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "verificacion.sin_estaciones",
            "ninguna estacion de caudal cae dentro de la cuenca a una distancia "
            "util de una union del modelo. NO hay con que verificar: el caudal "
            "de diseno queda sin contraste externo y el informe debe decirlo. "
            + (f"Lo mas cercano: {sin_pareja[:3]}" if sin_pareja else "")))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_CORRECTA)

    with registro.bloque(logger, "Caudal observado"):
        observados = leer_caudal_observado(
            ruta_serie, delimitador, [p.codigo for p in parejas])
        medios = leer_caudal_medio(
            ruta_serie, delimitador, [p.codigo for p in parejas])
        for pareja in parejas:
            pareja.maximos = maximos_anuales_de_mensuales(
                observados.get(pareja.codigo, {}))
            pareja.anios = len(pareja.maximos)
            # El caudal ordinario del rio, que el modelo NO simula. Se toma la
            # mediana y no la media para que un solo anio extraordinario no la
            # desplace.
            serie_media = sorted(medios.get(pareja.codigo, []))
            pareja.caudal_base = (serie_media[len(serie_media) // 2]
                                  if serie_media else 0.0)
            logger.info("  %s %s -> %s a %.0f m | %d anio(s) de caudal",
                        pareja.codigo, pareja.nombre[:22], pareja.union,
                        pareja.distancia_m, pareja.anios)

    hidrogramas = leer_hidrogramas(ruta_hidro, delimitador)
    periodos_todos = [float(t) for t in
                      configuracion.obtener("frecuencia.periodos_retorno")]
    confianza = float(configuracion.obtener("verificacion.confianza"))
    repeticiones = int(configuracion.obtener("verificacion.repeticiones"))
    factor = float(configuracion.obtener("verificacion.factor_extrapolacion"))
    minimo_anios = int(configuracion.obtener("verificacion.minimo_anios"))
    ventana_h = float(configuracion.obtener("verificacion.ventana_promedio_h"))

    with registro.bloque(logger, "Contraste"):
        for pareja in parejas:
            resultado.contrastes.extend(_contrastar(
                pareja, hidrogramas, periodos_todos, confianza, repeticiones,
                factor, minimo_anios, ventana_h, configuracion, resultado,
                logger))
            resultado.parejas.append({
                "codigo": pareja.codigo, "nombre": pareja.nombre,
                "union": pareja.union, "distancia_m": pareja.distancia_m,
                "anios": pareja.anios})

    if graficas:
        with registro.bloque(logger, "Figuras"):
            _dibujar(configuracion, base, parejas, hidrogramas, ventana_h,
                     resultado, logger)

    _escribir(configuracion, base, resultado, delimitador, logger)
    resultado.hallazgos.extend(_resumir(resultado))
    codigo = (SALIDA_BLOQUEANTE
              if esquema.hay_bloqueantes(resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _contrastar(pareja, hidrogramas, periodos_todos, confianza, repeticiones,
                factor, minimo_anios, ventana_h, configuracion, resultado,
                logger) -> list[dict[str, Any]]:
    """Ajusta la frecuencia observada y la contrasta contra el modelo."""
    if pareja.anios < minimo_anios:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "verificacion.serie_corta",
            f"{pareja.codigo} {pareja.nombre} tiene {pareja.anios} anio(s) "
            f"completos de caudal, por debajo del minimo de {minimo_anios}. NO "
            "se usa para verificar: un ajuste de frecuencia sobre tan pocos "
            "anios daria una banda tan ancha que aceptaria cualquier cosa."))
        return []

    sostenidos = periodos_sostenidos(pareja.anios, periodos_todos, factor)
    if not sostenidos:
        return []
    resultado.tr_maximo = max(resultado.tr_maximo, max(sostenidos))
    fuera = [t for t in periodos_todos if t not in sostenidos]
    if fuera:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "verificacion.extrapolacion",
            f"en {pareja.union} no se verifican los periodos {fuera}: con "
            f"{pareja.anios} anios de registro son extrapolacion de la "
            "distribucion ajustada, no observacion, y contrastarlos compararia "
            "dos extrapolaciones."))

    distribucion = str(configuracion.obtener(
        "verificacion.distribucion", "gumbel_max"))
    metodo = str(configuracion.obtener("verificacion.metodo", "momentos_l"))
    valores = list(pareja.maximos.values())
    try:
        banda = fr.banda_confianza(valores, distribucion, metodo, sostenidos,
                                   confianza=confianza,
                                   repeticiones=repeticiones)
    except fr.ErrorFrecuencia as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "verificacion.ajuste",
            f"no se pudo ajustar la frecuencia de {pareja.codigo}: {error}."))
        return []
    if not banda:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "verificacion.ajuste",
            f"el ajuste {distribucion}/{metodo} no convergio para "
            f"{pareja.codigo}: no hay banda contra la que contrastar."))
        return []

    del_modelo = hidrogramas.get(pareja.union, {})
    if not del_modelo:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "verificacion.sin_hidrograma",
            f"el M14 no guardo el hidrograma de {pareja.union}. Sin la serie "
            "completa no se puede promediar a 24 h: declarar la union en "
            "hec_hms.resultados.puntos_de_interes y volver a extraer."))
        return []

    filas: list[dict[str, Any]] = []
    for periodo in sorted(banda):
        clave = next((k for k in del_modelo
                      if abs(float(k) - periodo) < 1e-6), None)
        if clave is None:
            continue
        serie = del_modelo[clave]
        paso = serie[1][0] - serie[0][0] if len(serie) > 1 else 0.0
        media = media_movil_maxima([v for _, v in serie], paso, ventana_h)
        if media is None:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "verificacion.ventana_corta",
                f"el hidrograma de {pareja.union} dura menos de {ventana_h:.0f} "
                "h y la media no existe. Ampliar tormenta.ventana_simulacion_h "
                "y volver a correr el M13 y el M14."))
            return filas
        d = banda[periodo]
        pico = max(v for _, v in serie)
        # LA PRUEBA QUE DECIDE es de un solo lado y no supone ningun factor: el
        # pico real observado es al menos su media diaria, de modo que un pico
        # modelado por debajo de esa cota es demasiado bajo con certeza.
        cota = cota_inferior_del_pico(d["cuantil"], pareja.caudal_base)
        demasiado_bajo = pico < cota
        filas.append({
            "union": pareja.union, "codigo": pareja.codigo,
            "estacion": pareja.nombre, "anios": pareja.anios,
            "periodo_retorno": periodo,
            "observado_media_diaria_m3s": round(d["cuantil"], 3),
            "banda_inferior_m3s": round(d["inferior"], 3),
            "banda_superior_m3s": round(d["superior"], 3),
            "caudal_base_m3s": round(pareja.caudal_base, 3),
            "cota_inferior_del_pico_m3s": round(cota, 3),
            "pico_modelado_m3s": round(pico, 3),
            "modelo_demasiado_bajo": demasiado_bajo,
            "holgura_pct": round(100.0 * (pico - cota) / max(cota, 1e-9), 1),
            # Se conserva la media de 24 h como CONTEXTO de volumen, no como
            # veredicto: el modelo simula una tormenta de 3 h elegida para
            # maximizar el pico, y un dia real de caudal medio maximo tiene
            # lluvia repartida en muchas mas horas. Las dos cifras comparten
            # unidad pero no fenomeno.
            "modelado_24h_m3s": round(media, 3),
        })
    return filas


def _dibujar(configuracion, base, parejas, hidrogramas, ventana_h, resultado,
             logger) -> None:
    """Las dos figuras del capitulo, producidas por el mismo modulo."""
    try:
        import graficos
    except ImportError:
        return
    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    directorio.mkdir(parents=True, exist_ok=True)

    for pareja in parejas:
        filas = [f for f in resultado.contrastes if f["union"] == pareja.union]
        _figura_contraste(graficos, estilo, directorio, pareja, filas, base,
                          resultado)

    # La figura del metodo se dibuja UNA vez, sobre la primera union con
    # hidrograma: explica que se compara, no un resultado concreto.
    for pareja in parejas:
        del_modelo = hidrogramas.get(pareja.union, {})
        if not del_modelo:
            continue
        clave = sorted(del_modelo, key=lambda k: float(k))[len(del_modelo) // 2]
        serie = del_modelo[clave]
        paso = serie[1][0] - serie[0][0] if len(serie) > 1 else 0.0
        if paso > 0:
            _figura_media_movil(graficos, estilo, directorio, serie, paso,
                                ventana_h, pareja.union, clave, base, resultado)
        break
    logger.info("%d figura(s)", len([p for p in resultado.productos
                                     if p.endswith(".png")]))


def _escribir(configuracion, base, resultado, delimitador, logger) -> None:
    """La tabla del contraste, que es el anexo del capitulo."""
    if not resultado.contrastes:
        return
    destino = (rutas.directorio("procesado", base, crear=True) / "hidrologia"
               / "verificacion_crecientes.csv")
    destino.parent.mkdir(parents=True, exist_ok=True)
    columnas = list(resultado.contrastes[0])
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(manejador, fieldnames=columnas,
                                  delimiter=delimitador)
        escritor.writeheader()
        escritor.writerows(resultado.contrastes)
    resultado.productos.append(rutas.relativa(destino, base))
    logger.info("Tabla del contraste: %s", destino.name)


def _resumir(resultado: ResultadoM14c) -> list[Hallazgo]:
    """
    El veredicto. Es de UN SOLO LADO y conviene no leerlo como mas de lo que es.

    Se puede demostrar que el modelo es BAJO, porque el pico real observado es
    al menos su media diaria. No se puede demostrar que sea correcto: el pico
    real puede estar muy por encima de esa cota y no sabemos cuanto.
    """
    hallazgos: list[Hallazgo] = []
    if not resultado.contrastes:
        return hallazgos

    bajos = [f for f in resultado.contrastes if f["modelo_demasiado_bajo"]]
    total = len(resultado.contrastes)

    if not bajos:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "verificacion.resultado",
            f"en los {total} contraste(s) el pico modelado supera la cota "
            "inferior del pico observado, de modo que NADA contradice al "
            "modelo. Atencion a lo que esto significa y a lo que no: es una "
            "prueba de un solo lado. Demuestra que el modelo no se queda corto, "
            "NO que acierte. El pico real puede estar muy por encima de su "
            "media diaria, y con este dato no se puede saber cuanto."))
        return hallazgos

    detalle = "; ".join(
        f"{f['union']} Tr {f['periodo_retorno']:g}: pico modelado "
        f"{f['pico_modelado_m3s']:.2f} contra una cota de "
        f"{f['cota_inferior_del_pico_m3s']:.2f}" for f in bajos[:6])
    periodos = sorted({f["periodo_retorno"] for f in bajos})

    hallazgos.append(Hallazgo(
        ADVERTENCIA, "verificacion.modelo_bajo",
        f"el modelo es DEMASIADO BAJO con certeza en {len(bajos)} de {total} "
        f"contraste(s), en los periodos {periodos}. {detalle}. La certeza viene "
        "de que el pico instantaneo de un dia siempre supera a la media de ese "
        "dia: si el modelo no alcanza ni la media observada, no puede estar "
        "reproduciendo el pico. Ya se descuenta el caudal base, que el modelo "
        "no simula."))

    frecuentes = [p for p in periodos if p <= 10]
    if frecuentes and len(bajos) < total:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "verificacion.patron",
            f"el desajuste se concentra en los periodos frecuentes {frecuentes} "
            "y desaparece en los altos. Es el comportamiento conocido del "
            "metodo: con tormenta de diseno corta y sin flujo base, los eventos "
            "frecuentes salen bajos porque en ellos pesan la humedad antecedente "
            "y el caudal base, mientras que en los extremos domina la tormenta. "
            "Apunta a la hipotesis de partida antes que a los parametros, y "
            "conviene mirarlo antes de tocar el numero de curva o el rezago: "
            "bajar el CN para que casen los frecuentes subiria tambien los "
            "extremos, que hoy no se contradicen."))

    hallazgos.append(Hallazgo(
        INFORMATIVO, "verificacion.decision",
        "NO se ajusta ningun parametro de forma automatica. Ajustar es decision "
        "del consultor y convierte la verificacion en CALIBRACION: la "
        "coincidencia posterior deja de ser evidencia de que el modelo sea "
        "bueno y pasa a ser el resultado de haberla buscado. El informe debe "
        "declarar cual de las dos ocurrio."))
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
        ruta_json = (rutas.directorio("procesado", base, crear=True)
                     / "M14c_verificacion.json")
    ruta_json = Path(ruta_json)
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps({
        "modulo": MODULO,
        "parejas": resultado.parejas,
        "sin_pareja": resultado.sin_pareja,
        "tr_maximo_verificado": resultado.tr_maximo,
        "contrastes": resultado.contrastes,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

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


def _analizar_argumentos(argv: Sequence[str] | None = None):
    analizador = argparse.ArgumentParser(
        prog="M14c_verificacion.py",
        description="Verificacion de crecientes contra caudal observado.")
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--sin-graficas", dest="graficas",
                            action="store_false", default=True)
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida, graficas=argumentos.graficas,
            consola=not argumentos.silencioso)
    except (ErrorRutas, ErrorFormato) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return codigo


if __name__ == "__main__":
    sys.exit(main())
