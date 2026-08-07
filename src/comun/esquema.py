# -*- coding: utf-8 -*-
"""
comun.esquema
=============
Esquema declarativo de config/config.yaml y su validador.

Doctrina (CLAUDE.md, sección 2): ningún módulo contiene parámetros embebidos, y
un módulo se detiene y reporta antes de producir un resultado incorrecto. La
consecuencia es que la configuración debe validarse una sola vez, de forma
completa, antes de que cualquier módulo la consuma.

El esquema se declara como una tabla de rutas con punto (``tormenta.huff.cuartil``)
asociadas a un objeto Campo. Sobre esa validación estructural se aplican las
invariantes cruzadas, que son las que codifican las decisiones cerradas de la
sección 6 y las alertas permanentes de la sección 7 de CLAUDE.md.

Severidades del reporte, alineadas con las del MANIFIESTO.yaml:

    BLOQUEANTE   la configuración no puede usarse; la ejecución se detiene
    ADVERTENCIA  el valor es admisible pero técnicamente cuestionable
    INFORMATIVO  registro para trazabilidad

Solo usa la librería estándar: es importable desde el entorno de QGIS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "BLOQUEANTE",
    "ADVERTENCIA",
    "INFORMATIVO",
    "Hallazgo",
    "Campo",
    "ESQUEMA",
    "CLAVES_RUTA",
    "validar",
    "hay_bloqueantes",
    "resumen_por_severidad",
]

BLOQUEANTE = "BLOQUEANTE"
ADVERTENCIA = "ADVERTENCIA"
INFORMATIVO = "INFORMATIVO"

_ORDEN_SEVERIDAD = {BLOQUEANTE: 0, ADVERTENCIA: 1, INFORMATIVO: 2}

# Centinela para distinguir "clave ausente" de "clave presente con valor nulo".
_AUSENTE = object()

_NUMERO = (int, float)


# =============================================================================
# Estructuras del reporte
# =============================================================================
@dataclass(frozen=True)
class Hallazgo:
    """Un problema detectado en la configuración."""

    severidad: str
    clave: str
    mensaje: str

    @property
    def es_bloqueante(self) -> bool:
        return self.severidad == BLOQUEANTE

    def __str__(self) -> str:
        return f"[{self.severidad}] {self.clave}: {self.mensaje}"

    def como_dict(self) -> dict[str, str]:
        return {
            "severidad": self.severidad,
            "clave": self.clave,
            "mensaje": self.mensaje,
        }


@dataclass(frozen=True)
class Campo:
    """
    Contrato de un valor de la configuración.

    Atributos
    ---------
    tipo:          tipos admitidos para el valor.
    descripcion:   texto que se incorpora al mensaje de error.
    requerido:     si es False, la ausencia de la clave no es bloqueante.
    permite_nulo:  si es True, el valor puede ser None (decisión pendiente).
    opciones:      conjunto cerrado de valores admitidos.
    minimo/maximo: límites inclusivos para valores numéricos.
    elemento:      contrato de cada elemento, cuando el valor es una lista.
    valor_mapa:    contrato de cada valor, cuando el nodo es un mapa libre.
    nodo_libre:    el nodo admite claves no declaradas en el esquema.
    no_vacio:      la lista o el texto no pueden estar vacíos.
    creciente:     la lista numérica debe ser estrictamente creciente.
    es_ruta:       el valor es una ruta cuya existencia se verifica aparte.
    """

    tipo: tuple[type, ...]
    descripcion: str = ""
    requerido: bool = True
    permite_nulo: bool = False
    opciones: tuple = ()
    minimo: float | None = None
    maximo: float | None = None
    elemento: "Campo | None" = None
    valor_mapa: "Campo | None" = None
    nodo_libre: bool = False
    no_vacio: bool = False
    creciente: bool = False
    es_ruta: bool = False


# --- Constructores abreviados, para que la tabla del esquema sea legible ------
def texto(descripcion: str, **kw: Any) -> Campo:
    return Campo(tipo=(str,), descripcion=descripcion, **kw)


def ruta(descripcion: str, **kw: Any) -> Campo:
    return Campo(tipo=(str,), descripcion=descripcion, es_ruta=True, **kw)


def booleano(descripcion: str, **kw: Any) -> Campo:
    return Campo(tipo=(bool,), descripcion=descripcion, **kw)


def entero(descripcion: str, **kw: Any) -> Campo:
    return Campo(tipo=(int,), descripcion=descripcion, **kw)


def numero(descripcion: str, **kw: Any) -> Campo:
    return Campo(tipo=_NUMERO, descripcion=descripcion, **kw)


def lista(descripcion: str, elemento: Campo | None = None, **kw: Any) -> Campo:
    kw.setdefault("no_vacio", True)
    return Campo(tipo=(list,), descripcion=descripcion, elemento=elemento, **kw)


def mapa(descripcion: str, valor: Campo, **kw: Any) -> Campo:
    return Campo(
        tipo=(dict,),
        descripcion=descripcion,
        valor_mapa=valor,
        nodo_libre=True,
        **kw,
    )


# =============================================================================
# Vocabularios cerrados
# =============================================================================
DISTRIBUCIONES = (
    "normal", "lognormal2", "lognormal3", "gumbel_max", "gumbel_min", "gev",
    "pearson3", "logpearson3", "exponencial", "weibull", "gamma",
)
METODOS_AJUSTE = ("momentos", "momentos_l", "maxima_verosimilitud")
PRUEBAS_BONDAD = ("ks", "anderson_darling", "chi2")
CRITERIOS_SELECCION = ("aic", "bic")
METODOS_CONSISTENCIA = (
    "doble_masa", "correlacion", "pettitt", "snht", "mann_kendall",
    "rachas", "buishand",
)
METODOS_COMPLEMENTO = (
    "regresion_vecinas", "razon_normal", "idw", "knn", "mice", "promedio_vecinas",
)
HIPOTESIS_DESAGREGACION = ("h1_directa", "h2_idf", "h3_factor")
METODOS_TRANSITO = ("muskingum", "muskingum_cunge", "lag", "puls", "kinematic_wave")
TRANSFORMACIONES_HMS = (
    "scs_uh", "clark", "snyder", "modclark", "kinematic_wave", "user_hydrograph",
)
# Transformaciones que consumen el tiempo de rezago como parámetro de entrada.
TRANSFORMACIONES_CON_REZAGO = ("scs_uh", "snyder")
PERDIDAS_HMS = (
    "scs_cn", "green_ampt", "initial_constant", "deficit_constant", "soil_moisture",
)
FLUJO_BASE_HMS = ("recession", "constant_monthly", "linear_reservoir", "none")
METRICAS_CALIBRACION = ("nash_sutcliffe", "rmse", "pbias", "r2", "kge")
ESCENARIOS_CC = (
    "rcp26", "rcp45", "rcp60", "rcp85", "ssp126", "ssp245", "ssp370", "ssp585",
)
NIVELES_LOG = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Categorías del Catálogo Nacional de Estaciones del IDEAM. Un código fuera de
# esta lista no detiene el proceso, pero se advierte: puede ser una categoría
# nueva o un error de transcripción.
CATEGORIAS_IDEAM = (
    "AM", "CO", "CP", "PG", "PM", "PV", "SP", "SS", "ME", "EM", "HA", "RN",
    "MR", "DM", "LG", "LM", "RM", "LA", "SU",
)


# =============================================================================
# Esquema
# =============================================================================
ESQUEMA: dict[str, Campo] = {
    # --- proyecto ------------------------------------------------------------
    "proyecto.nombre": texto("nombre del estudio", no_vacio=True),
    "proyecto.contratante": texto("entidad contratante"),
    "proyecto.consultor": texto("firma consultora"),
    "proyecto.anio_estudio": entero(
        "año del estudio, controla el filtro de estaciones suspendidas",
        minimo=1900, maximo=2100,
    ),
    "proyecto.responsable": texto("profesional responsable"),

    # --- entornos ------------------------------------------------------------
    "entornos.qgis.version": texto("versión de QGIS declarada", no_vacio=True),
    "entornos.qgis.es_ltr": booleano("la versión declarada es LTR"),
    "entornos.qgis.python": ruta("intérprete de Python de QGIS", no_vacio=True),
    "entornos.qgis.prefix_path": ruta("QGIS_PREFIX_PATH", no_vacio=True),
    "entornos.venv.python": ruta("intérprete del venv del proyecto", no_vacio=True),
    "software.hec_hms.ruta": ruta("directorio de instalación de HEC-HMS"),
    "software.hec_hms.version": texto("versión de HEC-HMS", no_vacio=True),

    # --- sistema de referencia ----------------------------------------------
    "crs.calculo": texto("CRS de cálculo", no_vacio=True),
    "crs.geografico": texto("CRS de consulta a servicios externos", no_vacio=True),
    "crs.reproyeccion_explicita": booleano("prohibir reproyecciones implícitas"),

    # --- M00c ----------------------------------------------------------------
    "insumos_usuario.manifiesto": ruta("manifiesto de insumos del usuario"),
    "insumos_usuario.generar_homologacion": booleano(
        "generar las tablas de homologación a partir del insumo",
    ),
    "insumos_usuario.delimitador_csv": texto(
        "separador de las tablas de homologación", opciones=(";", ",", "\t"),
    ),
    "insumos_usuario.tabla_escala_area": ruta(
        "tabla de compatibilidad entre escala de suelos y área de cuenca",
    ),
    "insumos_usuario.cuenca_referencia": texto(
        "cuenca contra la cual se verifica la escala", no_vacio=True,
    ),
    "insumos_usuario.max_valores_homologacion": entero(
        "número máximo de valores únicos admitidos en una tabla de homologación",
        minimo=1, maximo=100000,
    ),

    # --- M00b ----------------------------------------------------------------
    "proyecto_qgis.declaracion": ruta("declaración del árbol de grupos y capas"),
    "proyecto_qgis.archivo": texto(
        "archivo .qgz que produce el módulo", no_vacio=True,
    ),
    "proyecto_qgis.estilos": texto(
        "directorio de estilos .qml del proyecto", no_vacio=True,
    ),
    "proyecto_qgis.titulo": texto("título del proyecto QGIS", no_vacio=True),
    "proyecto_qgis.escribir_estilo_inicial": booleano(
        "escribir el .qml por defecto cuando la capa aún no tiene estilo",
    ),
    "proyecto_qgis.detener_si_falta_capa": booleano(
        "detener el módulo si una capa declarada no existe",
    ),

    # --- M01 -----------------------------------------------------------------
    "punto_descarga.nombre": texto("rótulo del punto de descarga"),
    "punto_descarga.crs": texto(
        "CRS en que están expresadas las coordenadas del punto", no_vacio=True,
    ),
    "punto_descarga.x": numero(
        "Este en CRS proyectado, longitud en CRS geográfico", permite_nulo=True,
    ),
    "punto_descarga.y": numero(
        "Norte en CRS proyectado, latitud en CRS geográfico", permite_nulo=True,
    ),
    "referencia_nacional.directorio": texto(
        "directorio de las capas nacionales, fuera del repositorio",
        no_vacio=True,
    ),
    "referencia_nacional.drenaje_sencillo": texto(
        "archivo del drenaje sencillo", no_vacio=True,
    ),
    "referencia_nacional.drenaje_doble": texto(
        "archivo del drenaje doble", no_vacio=True,
    ),
    "referencia_nacional.escala": texto("escala de la cartografía", no_vacio=True),
    "referencia_nacional.fuente": texto("entidad productora", no_vacio=True),
    "referencia_nacional.fecha": texto("fecha de la cartografía", no_vacio=True),
    "referencia_nacional.campos.nombre": texto(
        "campo del nombre del cauce", no_vacio=True,
    ),
    "referencia_nacional.tolerancia_conexion_m": numero(
        "tolerancia de conexión entre afluente y cauce receptor",
        minimo=0, maximo=500,
    ),
    "referencia_nacional.max_incumplimiento_sentido_pct": numero(
        "incumplimiento máximo admitido de la convención de sentido",
        minimo=0, maximo=100,
    ),
    "referencia_nacional.salida_recorte_sencillo": texto(
        "recorte versionable del drenaje sencillo", no_vacio=True,
    ),
    "referencia_nacional.salida_recorte_doble": texto(
        "recorte versionable del drenaje doble", no_vacio=True,
    ),
    "referencia_nacional.salida_grafo": texto(
        "grafo de adyacencia de la red", no_vacio=True,
    ),
    "subzonas_hidrograficas.archivo": ruta("capa de subzonas hidrográficas"),
    "subzonas_hidrograficas.tolerancia_m": numero(
        "distancia máxima admitida si el punto cae fuera de toda subzona",
        minimo=0, maximo=50000,
    ),
    "subzonas_hidrograficas.campos.codigo_szh": texto(
        "campo del código de subzona", no_vacio=True,
    ),
    "subzonas_hidrograficas.campos.nombre_szh": texto(
        "campo del nombre de subzona", no_vacio=True,
    ),
    "subzonas_hidrograficas.campos.codigo_zh": texto(
        "campo del código de zona", no_vacio=True,
    ),
    "subzonas_hidrograficas.campos.nombre_zh": texto(
        "campo del nombre de zona", no_vacio=True,
    ),
    "subzonas_hidrograficas.campos.codigo_ah": texto(
        "campo del código de área hidrográfica", no_vacio=True,
    ),
    "subzonas_hidrograficas.campos.nombre_ah": texto(
        "campo del nombre de área hidrográfica", no_vacio=True,
    ),
    "subzonas_hidrograficas.salida_punto": texto(
        "shapefile del punto de descarga", no_vacio=True,
    ),
    "subzonas_hidrograficas.salida_subzona": texto(
        "shapefile de la subzona intersectada", no_vacio=True,
    ),

    # --- M02 -----------------------------------------------------------------
    "dem.fuente": texto(
        "fuente del modelo digital de terreno",
        opciones=("ALOS_PALSAR_RTC", "SRTM", "COPERNICUS_DEM", "usuario"),
    ),
    "dem.resolucion_m": numero("resolución del DEM en metros", minimo=0.5, maximo=90),
    "dem.earthdata.netrc": booleano("credenciales de Earthdata en archivo netrc"),
    "dem.earthdata.ruta_netrc": texto(
        "ubicación del archivo de credenciales; null usa ~/.netrc",
        permite_nulo=True,
    ),
    "dem.area_influencia.metodo": texto(
        "construcción del área de influencia",
        opciones=("envolvente", "buffer_cuenca", "poligono_usuario"),
    ),
    "dem.area_influencia.buffer_km": numero(
        "buffer del área de influencia en kilómetros", minimo=0, maximo=50,
    ),
    "dem.asf.nivel": texto(
        "nivel de producto de ASF", opciones=("RTC_HI_RES", "RTC_LOW_RES"),
    ),
    "dem.asf.plataforma": texto("plataforma satelital", no_vacio=True),
    "dem.asf.fecha_inicio": texto(
        "inicio de la ventana de adquisición (AAAA-MM-DD)", permite_nulo=True,
    ),
    "dem.asf.fecha_fin": texto(
        "fin de la ventana de adquisición (AAAA-MM-DD)", permite_nulo=True,
    ),
    "dem.asf.buffer_busqueda_km": numero(
        "holgura sobre la subzona para buscar escenas", minimo=0, maximo=50,
    ),
    "dem.asf.max_escenas": entero(
        "tope de escenas a descargar", minimo=1, maximo=500,
    ),
    "dem.asf.max_volumen_gb": numero(
        "tope de volumen de descarga en gigabytes", minimo=0.1, maximo=500,
    ),
    "dem.asf.verificar_md5": booleano("verificar la huella md5 de cada escena"),
    "dem.asf.reintentos": entero("reintentos por escena", minimo=1, maximo=10),
    "dem.asf.patron_dem_en_zip": texto(
        "sufijo del archivo de elevación dentro del .zip", no_vacio=True,
    ),
    "dem.asf.cobertura_minima_pct": numero(
        "cobertura mínima del área antes de advertir", minimo=0, maximo=100,
    ),
    "dem.delimitacion.metodo": texto(
        "método de delimitación preliminar",
        opciones=("automatico", "cartografico", "terreno"),
    ),
    "dem.delimitacion.umbral_drenaje_doble_m": numero(
        "distancia máxima al drenaje doble para el escenario 1",
        minimo=0, maximo=1000,
    ),
    "dem.delimitacion.umbral_drenaje_sencillo_m": numero(
        "distancia máxima al drenaje sencillo para el escenario 2",
        minimo=0, maximo=5000,
    ),
    "dem.delimitacion.tolerancia_empalme_eje_m": numero(
        "tolerancia de unión del eje con el drenaje sencillo",
        minimo=0, maximo=2000,
    ),
    "dem.delimitacion.origen_area.escenario_1": texto(
        "origen del área de influencia en el escenario 1",
        opciones=("subzona", "red", "cuenca"),
    ),
    "dem.delimitacion.origen_area.escenario_2": texto(
        "origen del área de influencia en el escenario 2",
        opciones=("subzona", "red", "cuenca"),
    ),
    "dem.delimitacion.origen_area.escenario_3": texto(
        "origen del área de influencia en el escenario 3",
        opciones=("subzona", "red", "cuenca"),
    ),
    "dem.delimitacion.buffer_red_km.escenario_1": numero(
        "buffer sobre la red trazada en el escenario 1", minimo=0, maximo=50,
    ),
    "dem.delimitacion.buffer_red_km.escenario_2": numero(
        "buffer sobre la red trazada en el escenario 2", minimo=0, maximo=50,
    ),
    "dem.delimitacion.umbral_celdas_cauce": entero(
        "celdas acumuladas para definir un cauce", minimo=1, maximo=10_000_000,
    ),
    "dem.delimitacion.radio_ajuste_m": numero(
        "radio de búsqueda del cauce alrededor del punto", minimo=0, maximo=5000,
    ),
    "dem.delimitacion.detener_si_toca_borde": booleano(
        "detener si la cuenca delimitada toca el borde del DEM",
    ),
    "dem.delimitacion.salida_dem": texto("ráster de elevación recortado",
                                         no_vacio=True),
    "dem.delimitacion.salida_cuenca": texto("cuenca preliminar", no_vacio=True),
    "dem.delimitacion.salida_envolvente": texto("envolvente de la cuenca",
                                                no_vacio=True),
    "dem.delimitacion.salida_area_influencia": texto(
        "área de influencia", no_vacio=True,
    ),

    # --- M03 -----------------------------------------------------------------
    "estaciones.buffer_adicional_km": numero(
        "buffer adicional para la selección de estaciones", minimo=0, maximo=50,
    ),
    "estaciones.catalogo": ruta("Catálogo Nacional de Estaciones del IDEAM"),
    "estaciones.fecha_catalogo": texto(
        "fecha de descarga del catálogo, para trazabilidad", no_vacio=True,
    ),
    "estaciones.campos.codigo": texto("campo del código", no_vacio=True),
    "estaciones.campos.nombre": texto("campo del nombre", no_vacio=True),
    "estaciones.campos.categoria": texto("campo de la categoría", no_vacio=True),
    "estaciones.campos.categoria_desc": texto(
        "campo de la descripción de categoría", no_vacio=True,
    ),
    "estaciones.campos.estado": texto("campo del estado", no_vacio=True),
    "estaciones.campos.entidad": texto("campo de la entidad", no_vacio=True),
    "estaciones.campos.altitud": texto("campo de la altitud", no_vacio=True),
    "estaciones.campos.latitud": texto("campo de la latitud", no_vacio=True),
    "estaciones.campos.longitud": texto("campo de la longitud", no_vacio=True),
    "estaciones.campos.corriente": texto("campo de la corriente", no_vacio=True),
    "estaciones.campos.departamento": texto("campo del departamento",
                                            no_vacio=True),
    "estaciones.campos.municipio": texto("campo del municipio", no_vacio=True),
    "estaciones.campos.subzona": texto("campo de la subzona", no_vacio=True),
    "estaciones.campos.fecha_instalacion": texto(
        "campo de la fecha de instalación", no_vacio=True,
    ),
    "estaciones.campos.fecha_suspension": texto(
        "campo de la fecha de suspensión", no_vacio=True,
    ),
    "estaciones.banda_cota_m": numero(
        "banda de incertidumbre vertical para juzgar la posición relativa",
        minimo=0, maximo=500,
    ),
    "estaciones.salida_seleccionadas": texto(
        "capa de estaciones seleccionadas", no_vacio=True,
    ),
    "estaciones.salida_descartadas": texto(
        "capa de estaciones descartadas", no_vacio=True,
    ),
    "estaciones.inventario_csv": texto(
        "inventario de estaciones en CSV", no_vacio=True,
    ),
    "estaciones.categorias_por_variable": mapa(
        "categorías de estación admitidas por variable",
        valor=lista("categorías admitidas", texto("categoría IDEAM")),
    ),

    # --- M04 -----------------------------------------------------------------
    "ideam.fuente_primaria": texto(
        "fuente primaria de ingesta", opciones=("socrata", "dhime_zip"),
    ),
    "ideam.socrata.dominio": texto("dominio Socrata", no_vacio=True),
    "ideam.socrata.token": texto("token de aplicación Socrata"),
    "ideam.socrata.limite_pagina": entero(
        "tamaño de página de la API", minimo=1, maximo=50000,
    ),
    "ideam.socrata.dataset_catalogo": texto(
        "identificador del Catálogo Nacional de Estaciones", no_vacio=True,
    ),
    "ideam.descarga.activar": booleano("habilitar la descarga automática"),
    "ideam.descarga.fecha_inicio": texto("inicio del periodo", no_vacio=True),
    "ideam.descarga.fecha_fin": texto("fin del periodo", no_vacio=True),
    "ideam.descarga.ventana_anios": entero(
        "años por petición; el IDEAM limita a 30", minimo=1, maximo=30,
    ),
    "ideam.descarga.max_series_por_trabajo": entero(
        "series por petición al servicio", minimo=1, maximo=50,
    ),
    "ideam.descarga.espera_entre_trabajos_s": numero(
        "espera entre peticiones", minimo=0, maximo=60,
    ),
    "ideam.descarga.reintentos": entero("reintentos por trabajo", minimo=1, maximo=10),
    "ideam.descarga.series_por_variable": mapa(
        "series a solicitar por variable",
        valor=Campo(tipo=(list,), descripcion="lista de series", no_vacio=True),
    ),
    "ideam.dhime_zip.patron_archivo": texto(
        "patrón de descubrimiento de archivos DHIME", no_vacio=True,
    ),
    "ideam.dhime_zip.perfiles": ruta("perfiles de formato de descarga IDEAM"),
    "ideam.deduplicacion.clave": lista(
        "clave de deduplicación de registros", texto("campo"),
    ),
    "ideam.deduplicacion.precedencia_aprobacion": lista(
        "precedencia entre niveles de aprobación", texto("nivel"),
    ),
    "ideam.deduplicacion.reportar_conflictos": booleano("reportar conflictos"),
    "ideam.nivel_aprobacion.usar_como_filtro": booleano(
        "usar NivelAprobacion como criterio de descarte",
    ),
    "ideam.escala_temporal.deteccion": texto(
        "estrategia de detección de la escala temporal",
        opciones=("cruzada", "parametro", "espaciamiento"),
    ),
    "ideam.escala_temporal.detener_si_discrepa": booleano(
        "detener si las estrategias de detección discrepan",
    ),
    "ideam.agregacion_diaria_a_mensual.max_dias_faltantes": entero(
        "días faltantes admitidos al totalizar un mes", minimo=0, maximo=28,
    ),
    "ideam.precipitacion_mensual.fuente_primaria": texto(
        "fuente primaria de la serie mensual",
        opciones=("mensual_ideam", "agregacion_diaria"),
    ),
    "ideam.precipitacion_mensual.completar_con_agregacion_diaria": booleano(
        "completar la serie mensual con la agregación de la diaria",
    ),
    "ideam.precipitacion_mensual.reporte_discrepancias": booleano(
        "reportar discrepancias entre ambas fuentes",
    ),

    # --- M04b ----------------------------------------------------------------
    "sensibilidad_series.umbrales_anios": lista(
        "umbrales de longitud de serie evaluados",
        entero("años", minimo=1, maximo=150), creciente=True,
    ),
    "sensibilidad_series.ventanas": lista("ventanas temporales evaluadas"),
    "sensibilidad_series.anios_max_suspension": entero(
        "antigüedad máxima admitida de la suspensión", minimo=0, maximo=100,
    ),
    "sensibilidad_series.umbral_adoptado_anios": entero(
        "umbral adoptado por el consultor",
        permite_nulo=True, minimo=1, maximo=150,
    ),
    "sensibilidad_series.ventana_adoptada": Campo(
        tipo=(list,), descripcion="ventana adoptada por el consultor",
        permite_nulo=True, no_vacio=True,
    ),

    # --- M05: anómalos -------------------------------------------------------
    "anomalos.metodo": texto(
        "método de detección de anómalos", opciones=("IQR", "ER", "ZSCORE"),
    ),
    "anomalos.q1": numero("cuartil inferior", minimo=0, maximo=1),
    "anomalos.q3": numero("cuartil superior", minimo=0, maximo=1),
    "anomalos.k_sigma": numero("factor k del método ER", minimo=0.1, maximo=10),
    "anomalos.zscore_umbral": numero("umbral del z-score", minimo=0.1, maximo=10),
    "anomalos.tratamiento": texto(
        "tratamiento del dato anómalo",
        opciones=("marcar", "drop", "cap", "imputar"),
    ),
    "anomalos.aplicar_a_serie_maximos": booleano(
        "aplicar el filtro de anómalos a la serie de máximos",
    ),

    # --- M05: consistencia ---------------------------------------------------
    "consistencia.metodos": lista(
        "pruebas de consistencia y homogeneidad",
        texto("método", opciones=METODOS_CONSISTENCIA),
    ),
    "consistencia.correlacion_minima": numero(
        "correlación mínima con la estación vecina", minimo=0, maximo=1,
    ),
    "consistencia.n_estaciones_vecinas": entero(
        "número de estaciones vecinas", minimo=1, maximo=50,
    ),
    "consistencia.descartar_bajo_umbral": booleano(
        "descartar estaciones bajo el umbral de correlación",
    ),

    # --- M05: complemento ----------------------------------------------------
    "complemento.metodos_evaluados": lista(
        "métodos de relleno evaluados",
        texto("método", opciones=METODOS_COMPLEMENTO),
    ),
    "complemento.metodo_adoptado": texto(
        "método de relleno adoptado",
        permite_nulo=True, opciones=METODOS_COMPLEMENTO,
    ),
    "complemento.validacion_cruzada": booleano("ejecutar validación cruzada"),
    "complemento.k_vecinos": entero("vecinos del método knn", minimo=1, maximo=50),
    "complemento.valor_minimo": numero(
        "valor mínimo admisible del dato rellenado", permite_nulo=True,
    ),
    "complemento.max_porcentaje_sintetico": numero(
        "porcentaje máximo de dato sintético antes de advertir",
        minimo=0, maximo=100,
    ),

    # --- M05b ----------------------------------------------------------------
    "enso.url_oni": texto("URL del índice ONI de la NOAA", no_vacio=True),
    "enso.umbral_anomalia_c": numero(
        "umbral de anomalía en grados Celsius", minimo=0.1, maximo=5,
    ),
    "enso.temporadas_consecutivas": entero(
        "temporadas consecutivas para declarar la fase", minimo=1, maximo=24,
    ),
    "enso.criterio": texto(
        "criterio de conteo de temporadas",
        opciones=("consecutivo", "no_consecutivo"),
    ),
    "enso.base_anual": texto(
        "base de agregación anual", opciones=("calendario", "hidrologico"),
    ),
    "enso.elimina_registros": booleano("permitir que ENSO descarte registros"),

    # --- M06 / M08 / M11 -----------------------------------------------------
    "interpolacion.metodo": texto(
        "método de interpolación", opciones=("IDW", "KRIGING", "TIN", "SPLINE"),
    ),
    "interpolacion.idw.potencia": numero("potencia del IDW", minimo=0.1, maximo=10),
    "interpolacion.idw.radio_busqueda": numero(
        "radio de búsqueda en metros", permite_nulo=True, minimo=0,
    ),
    "interpolacion.resolucion_raster_m": numero(
        "resolución del ráster interpolado", minimo=0.5, maximo=1000,
    ),
    "interpolacion.validacion_cruzada": booleano("validación cruzada de la superficie"),

    # --- M07 -----------------------------------------------------------------
    "frecuencia.periodos_retorno": lista(
        "periodos de retorno en años",
        numero("periodo de retorno", minimo=1.01, maximo=10000), creciente=True,
    ),
    "frecuencia.distribuciones": lista(
        "distribuciones ajustadas", texto("distribución", opciones=DISTRIBUCIONES),
    ),
    "frecuencia.ajuste": lista(
        "métodos de estimación de parámetros",
        texto("método", opciones=METODOS_AJUSTE),
    ),
    "frecuencia.pruebas": lista(
        "pruebas de bondad de ajuste", texto("prueba", opciones=PRUEBAS_BONDAD),
    ),
    "frecuencia.criterios_seleccion": lista(
        "criterios de selección", texto("criterio", opciones=CRITERIOS_SELECCION),
    ),
    "frecuencia.distribucion_adoptada": texto(
        "distribución forzada por el consultor",
        permite_nulo=True, opciones=DISTRIBUCIONES,
    ),
    "frecuencia.outliers_altos_bajos": texto(
        "tratamiento de outliers altos y bajos",
        opciones=("bulletin17c", "ninguno"),
    ),

    # --- M11 -----------------------------------------------------------------
    "zonificacion_pluviometrica.diferencia_maxima_pct": numero(
        "diferencia porcentual máxima dentro de un grupo", minimo=0, maximo=100,
    ),
    "zonificacion_pluviometrica.considerar_gradiente_altitudinal": booleano(
        "incorporar el gradiente altitudinal",
    ),
    "zonificacion_pluviometrica.ponderar_por_area": booleano("ponderar por área"),

    # --- M11c ----------------------------------------------------------------
    "arf.aplicar": texto(
        "criterio de aplicación del factor de reducción por área",
        opciones=("evaluar", "forzar_si", "forzar_no"),
    ),
    "arf.tabla": ruta("tabla ARF del INVIAS"),
    "arf.verificacion_analitica": booleano("verificación analítica del ARF"),

    # --- M12a ----------------------------------------------------------------
    "idf.metodologias": lista(
        "metodologías de curvas IDF",
        texto("metodología", opciones=("invias", "silva", "ideam")),
    ),
    "idf.coeficientes_invias": ruta("coeficientes IDF del INVIAS"),
    "idf.consultar_idf_ideam": booleano("consultar las IDF publicadas por el IDEAM"),
    "idf.antiguedad_maxima_anios": entero(
        "antigüedad máxima admitida de una IDF publicada", minimo=1, maximo=100,
    ),
    "idf.duraciones_min": lista(
        "duraciones de la curva IDF en minutos",
        entero("duración", minimo=1, maximo=10080), creciente=True,
    ),
    "cambio_climatico.aplicar": booleano("aplicar factores de cambio climático"),
    "cambio_climatico.fuente": ruta("tabla de factores de cambio climático"),
    "cambio_climatico.comunicacion": texto(
        "comunicación nacional de cambio climático",
        opciones=("tercera", "cuarta"),
    ),
    "cambio_climatico.escenarios": lista(
        "escenarios evaluados", texto("escenario", opciones=ESCENARIOS_CC),
    ),
    "cambio_climatico.horizontes": lista("horizontes temporales", texto("horizonte")),
    "cambio_climatico.solo_si_incremento": booleano(
        "aplicar el factor solo cuando represente incremento",
    ),

    # --- M12b ----------------------------------------------------------------
    "tormenta.duracion_h": numero(
        "duración de la tormenta de diseño en horas", minimo=0.25, maximo=72,
    ),
    "tormenta.intervalo_calculo_min": entero(
        "intervalo de cálculo en minutos", minimo=1, maximo=1440,
    ),
    "tormenta.metodo": texto(
        "método de construcción del hietograma",
        opciones=("huff", "bloques_alternos", "scs", "triangular", "chicago"),
    ),
    "tormenta.huff.cuartil": entero("cuartil de Huff", minimo=1, maximo=4),
    "tormenta.huff.probabilidad_excedencia": numero(
        "probabilidad de excedencia de la curva de Huff", minimo=1, maximo=99,
    ),
    "tormenta.huff.tabla": ruta("tabla de cuartiles de Huff"),
    "tormenta.hipotesis_p24_a_pd": lista(
        "hipótesis de desagregación evaluadas",
        texto("hipótesis", opciones=HIPOTESIS_DESAGREGACION),
    ),
    "tormenta.hipotesis_adoptada": texto(
        "hipótesis adoptada por el consultor",
        permite_nulo=True, opciones=HIPOTESIS_DESAGREGACION,
    ),
    "tormenta.coeficiente_desagregacion.valor": numero(
        "coeficiente de desagregación de h3_factor",
        permite_nulo=True, minimo=0, maximo=1,
    ),
    "tormenta.coeficiente_desagregacion.fuente": texto(
        "fuente documental del coeficiente de desagregación",
    ),
    "tormenta.advertir_si_duracion_menor_tc": booleano(
        "advertir si la duración es menor que el tiempo de concentración",
    ),

    # --- M10 -----------------------------------------------------------------
    "morfometria.parametros.geometria": lista(
        "parámetros de geometría", texto("parámetro"),
    ),
    "morfometria.parametros.relieve": lista(
        "parámetros de relieve", texto("parámetro"),
    ),
    "morfometria.parametros.drenaje": lista(
        "parámetros de drenaje", texto("parámetro"),
    ),
    "morfometria.parametros.respuesta": lista(
        "parámetros de respuesta hidrológica", texto("parámetro"),
    ),
    "tiempo_concentracion.tabla_aplicabilidad": ruta(
        "matriz de aplicabilidad de fórmulas de Tc",
    ),
    "tiempo_concentracion.filtrar_por_tipo_cuenca": booleano(
        "filtrar fórmulas por tipo de cuenca",
    ),
    "tiempo_concentracion.valor_adoptado": texto(
        "estadístico adoptado del subconjunto aplicable",
        opciones=("mediana", "media", "minimo", "maximo"),
    ),
    "tiempo_concentracion.min_formulas_aplicables": entero(
        "mínimo de fórmulas aplicables para adoptar el estadístico",
        minimo=1, maximo=30,
    ),
    "tiempo_concentracion.cv_maximo_admisible": numero(
        "coeficiente de variación máximo admisible", minimo=0, maximo=5,
    ),
    "tiempo_concentracion.reportar_sensibilidad_caudal": booleano(
        "reportar la sensibilidad del caudal al Tc",
    ),
    "tiempo_rezago.criterio": texto(
        "criterio de cálculo del tiempo de rezago", opciones=("scs", "hechms"),
    ),
    "tiempo_rezago.validar_coherencia_con_transform": booleano(
        "validar coherencia con el método de transformación de HEC-HMS",
    ),
    "numero_curva.condicion_humedad": texto(
        "condición de humedad antecedente", opciones=("I", "II", "III"),
    ),
    "numero_curva.tabla_cn": ruta("tabla de números de curva del SCS"),
    "numero_curva.tabla_grupo_hidrologico": ruta(
        "tabla de grupo hidrológico por textura",
    ),
    "numero_curva.homologacion_suelos": ruta("homologación de suelos del consultor"),
    "numero_curva.homologacion_cobertura": ruta(
        "homologación de cobertura del consultor",
    ),
    "numero_curva.advertir_escala_incompatible": booleano(
        "advertir si la escala del shape de suelos es incompatible",
    ),

    # --- M13 / M14 -----------------------------------------------------------
    "hec_hms.transform": texto(
        "método de transformación", opciones=TRANSFORMACIONES_HMS,
    ),
    "hec_hms.loss": texto("método de pérdidas", opciones=PERDIDAS_HMS),
    "hec_hms.baseflow": texto("método de flujo base", opciones=FLUJO_BASE_HMS),
    "hec_hms.transito.metodos": lista(
        "métodos de tránsito calculados",
        texto("método", opciones=METODOS_TRANSITO),
    ),
    "hec_hms.transito.metodo_adoptado": texto(
        "método de tránsito adoptado", opciones=METODOS_TRANSITO,
    ),
    "hec_hms.transito.muskingum.celeridad_ms": numero(
        "celeridad de onda en m/s", permite_nulo=True, minimo=0.01, maximo=20,
    ),
    "hec_hms.transito.muskingum_cunge.forma_seccion": texto(
        "forma de la sección de tránsito",
        opciones=("trapezoidal", "rectangular", "triangular", "circular", "natural"),
    ),
    "hec_hms.transito.muskingum_cunge.n_manning": numero(
        "coeficiente n de Manning", permite_nulo=True, minimo=0.005, maximo=0.2,
    ),
    "hec_hms.control.intervalo_min": entero(
        "intervalo de cálculo de las especificaciones de control",
        minimo=1, maximo=1440,
    ),
    "calibracion.activar_si_hay_series": booleano(
        "activar la calibración si existen series utilizables",
    ),
    "calibracion.fuente_caudales": lista(
        "fuentes de caudal para calibrar",
        texto("fuente", opciones=("socrata", "usuario", "dhime_zip")),
    ),
    "calibracion.metricas": lista(
        "métricas de desempeño", texto("métrica", opciones=METRICAS_CALIBRACION),
    ),

    # --- M18 / M19 -----------------------------------------------------------
    "balance_hidrico.activar": booleano("ejecutar el balance hídrico"),
    "balance_hidrico.metodo": texto(
        "método del balance", opciones=("budyko", "turc", "coutagne"),
    ),
    "balance_hidrico.evapotranspiracion.metodo": texto(
        "método de evapotranspiración",
        opciones=("turc", "thornthwaite", "penman_monteith", "cenicafe", "hargreaves"),
    ),
    "balance_hidrico.infiltracion.metodo": texto(
        "método de infiltración", opciones=("scs", "horton", "green_ampt", "philip"),
    ),
    "balance_hidrico.contrastar_con_ena": booleano(
        "contrastar con el Estudio Nacional del Agua",
    ),
    "balance_hidrico.ena_anio": entero(
        "año del Estudio Nacional del Agua", minimo=1998, maximo=2100,
    ),
    "caudal_ambiental.metodos": lista(
        "métodos de caudal ambiental",
        texto("método", opciones=("q95", "qirh", "7q10", "tennant")),
    ),
    "caudal_ambiental.umbral_irh": numero(
        "umbral del índice de retención y regulación hídrica", minimo=0, maximo=1,
    ),
    "caudal_ambiental.cdc_si_irh_menor": numero(
        "percentil de la CDC si el IRH es menor al umbral", minimo=0, maximo=100,
    ),
    "caudal_ambiental.cdc_si_irh_mayor": numero(
        "percentil de la CDC si el IRH es mayor al umbral", minimo=0, maximo=100,
    ),
    "caudal_ambiental.metodo_adoptado": texto(
        "método de caudal ambiental adoptado",
        opciones=("q95", "qirh", "7q10", "tennant"),
    ),

    # --- M15 / M16 / M17 -----------------------------------------------------
    "informe.plantilla": ruta("plantilla .dotx del informe"),
    "informe.formato_figuras": texto("rótulo de las figuras", no_vacio=True),
    "informe.formato_graficos": texto("rótulo de los gráficos", no_vacio=True),
    "informe.formato_tablas": texto("rótulo de las tablas", no_vacio=True),
    "informe.numeracion_por_capitulo": booleano("numerar con prefijo de capítulo"),
    "cartografia.plantillas_qpt": ruta("directorio de composiciones .qpt"),
    "cartografia.escalas": lista(
        "denominadores de escala", entero("escala", minimo=100), creciente=True,
    ),
    "cartografia.formato_salida": lista(
        "formatos de exportación",
        texto("formato", opciones=("pdf", "png", "svg", "jpg", "tif")),
    ),
    "cartografia.dpi": entero("resolución de salida", minimo=72, maximo=1200),
    "anexos.estructura": ruta("estructura de anexos"),
    "anexos.verificar_completitud": booleano("verificar completitud de los anexos"),
    "anexos.calcular_hash": booleano("calcular hash de los anexos"),

    # --- ejecución -----------------------------------------------------------
    "ejecucion.nivel_log": texto("nivel de log", opciones=NIVELES_LOG),
    "ejecucion.detener_en_advertencia": booleano("detener ante una advertencia"),
    "ejecucion.guardar_intermedios": booleano("conservar productos intermedios"),
    "ejecucion.semilla_aleatoria": entero(
        "semilla de los procesos aleatorios", permite_nulo=True, minimo=0,
    ),
}

# Claves cuyo valor es una ruta que debe existir en el repositorio. La ausencia
# se reporta como advertencia: los insumos se van incorporando a medida que
# avanza el estudio y no todos son necesarios desde el primer módulo.
CLAVES_RUTA: tuple[str, ...] = tuple(
    clave for clave, campo in ESQUEMA.items() if campo.es_ruta
)

# Rutas que apuntan a software externo y no al repositorio. Se verifican, pero
# su ausencia solo es relevante para los módulos que las usan.
_RUTAS_EXTERNAS = (
    "entornos.qgis.python",
    "entornos.qgis.prefix_path",
    "software.hec_hms.ruta",
)

# Prefijos implícitos del esquema, para detectar claves no reconocidas.
def _prefijos(claves: Iterable[str]) -> set[str]:
    acumulado: set[str] = set()
    for clave in claves:
        partes = clave.split(".")
        for i in range(1, len(partes)):
            acumulado.add(".".join(partes[:i]))
    return acumulado


_PREFIJOS = _prefijos(ESQUEMA)


# =============================================================================
# Acceso a valores anidados
# =============================================================================
def obtener(datos: dict, clave: str, defecto: Any = _AUSENTE) -> Any:
    """
    Devuelve el valor de una clave con puntos, o `defecto` si no existe.

    No lanza excepción: el validador necesita distinguir ausencia de error.
    """
    nodo: Any = datos
    for parte in clave.split("."):
        if not isinstance(nodo, dict) or parte not in nodo:
            return defecto
        nodo = nodo[parte]
    return nodo


# =============================================================================
# Validación estructural
# =============================================================================
def _nombre_tipo(tipos: Sequence[type]) -> str:
    equivalencias = {
        str: "texto", bool: "booleano", int: "entero",
        float: "decimal", list: "lista", dict: "bloque",
    }
    return " o ".join(equivalencias.get(t, t.__name__) for t in tipos)


def _tipo_admitido(valor: Any, tipos: Sequence[type]) -> bool:
    # bool es subclase de int en Python. Un booleano donde se espera un número
    # es un error de configuración, no una coincidencia de tipo.
    if isinstance(valor, bool) and bool not in tipos:
        return False
    return isinstance(valor, tuple(tipos))


def _validar_valor(clave: str, valor: Any, campo: Campo) -> list[Hallazgo]:
    """Aplica el contrato de un Campo a un valor concreto."""
    hallazgos: list[Hallazgo] = []

    if valor is None:
        if not campo.permite_nulo:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, clave,
                f"no admite valor nulo ({campo.descripcion}).",
            ))
        return hallazgos

    if not _tipo_admitido(valor, campo.tipo):
        hallazgos.append(Hallazgo(
            BLOQUEANTE, clave,
            f"se esperaba {_nombre_tipo(campo.tipo)} y se recibió "
            f"{type(valor).__name__} ({valor!r}).",
        ))
        return hallazgos

    if campo.opciones and valor not in campo.opciones:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, clave,
            f"valor {valor!r} fuera del conjunto admitido: "
            f"{', '.join(str(o) for o in campo.opciones)}.",
        ))

    if isinstance(valor, str) and campo.no_vacio and not valor.strip():
        hallazgos.append(Hallazgo(
            BLOQUEANTE, clave, f"no puede estar vacío ({campo.descripcion}).",
        ))

    if isinstance(valor, _NUMERO) and not isinstance(valor, bool):
        if campo.minimo is not None and valor < campo.minimo:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, clave,
                f"valor {valor} menor que el mínimo admitido ({campo.minimo}).",
            ))
        if campo.maximo is not None and valor > campo.maximo:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, clave,
                f"valor {valor} mayor que el máximo admitido ({campo.maximo}).",
            ))

    if isinstance(valor, list):
        if campo.no_vacio and not valor:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, clave, f"la lista está vacía ({campo.descripcion}).",
            ))
        if campo.elemento is not None:
            for indice, elemento in enumerate(valor):
                hallazgos.extend(
                    _validar_valor(f"{clave}[{indice}]", elemento, campo.elemento)
                )
        if campo.creciente and len(valor) > 1:
            numericos = [v for v in valor if isinstance(v, _NUMERO)
                         and not isinstance(v, bool)]
            if len(numericos) == len(valor):
                if any(b <= a for a, b in zip(numericos, numericos[1:])):
                    hallazgos.append(Hallazgo(
                        BLOQUEANTE, clave,
                        "la lista debe ser estrictamente creciente.",
                    ))

    if isinstance(valor, dict) and campo.valor_mapa is not None:
        if campo.nodo_libre and not valor:
            hallazgos.append(Hallazgo(
                ADVERTENCIA, clave, f"el bloque está vacío ({campo.descripcion}).",
            ))
        for subclave, subvalor in valor.items():
            hallazgos.extend(
                _validar_valor(f"{clave}.{subclave}", subvalor, campo.valor_mapa)
            )

    return hallazgos


def validar_estructura(datos: dict) -> list[Hallazgo]:
    """Verifica presencia, tipo y dominio de cada clave declarada."""
    hallazgos: list[Hallazgo] = []

    for clave, campo in ESQUEMA.items():
        valor = obtener(datos, clave, _AUSENTE)
        if valor is _AUSENTE:
            severidad = BLOQUEANTE if campo.requerido else ADVERTENCIA
            hallazgos.append(Hallazgo(
                severidad, clave, f"clave ausente ({campo.descripcion}).",
            ))
            continue
        hallazgos.extend(_validar_valor(clave, valor, campo))

    hallazgos.extend(_claves_no_reconocidas(datos))
    return hallazgos


def _claves_no_reconocidas(datos: dict, prefijo: str = "") -> list[Hallazgo]:
    """
    Recorre la configuración y advierte sobre claves fuera del esquema.

    Una clave no reconocida suele ser una errata: el módulo leería el valor por
    defecto sin enterarse de que el consultor intentó cambiarlo.
    """
    hallazgos: list[Hallazgo] = []
    if not isinstance(datos, dict):
        return hallazgos

    for clave, valor in datos.items():
        ruta_clave = f"{prefijo}.{clave}" if prefijo else str(clave)

        if ruta_clave in ESQUEMA:
            continue
        if ruta_clave in _PREFIJOS:
            if isinstance(valor, dict):
                hallazgos.extend(_claves_no_reconocidas(valor, ruta_clave))
            else:
                hallazgos.append(Hallazgo(
                    BLOQUEANTE, ruta_clave,
                    "se esperaba un bloque de claves y se recibió un valor simple.",
                ))
            continue

        hallazgos.append(Hallazgo(
            ADVERTENCIA, ruta_clave,
            "clave no reconocida por el esquema; ningún módulo la leerá.",
        ))

    return hallazgos


# =============================================================================
# Invariantes cruzadas
# =============================================================================
# Cada invariante codifica una decisión cerrada de CLAUDE.md, sección 6, o una
# alerta permanente de la sección 7. La referencia se cita en el mensaje para
# que el reporte sea auditable ante interventoría.

def _inv_anomalos_maximos(datos: dict) -> list[Hallazgo]:
    if obtener(datos, "anomalos.aplicar_a_serie_maximos") is True:
        return [Hallazgo(
            BLOQUEANTE, "anomalos.aplicar_a_serie_maximos",
            "debe ser false. Aplicar el filtro de anómalos a la serie de máximos "
            "truncaría el dato de diseño (CLAUDE.md, sección 7).",
        )]
    return []


def _inv_enso_no_elimina(datos: dict) -> list[Hallazgo]:
    if obtener(datos, "enso.elimina_registros") is True:
        return [Hallazgo(
            BLOQUEANTE, "enso.elimina_registros",
            "debe ser false. El análisis ENSO clasifica, no elimina estaciones ni "
            "registros (CLAUDE.md, sección 6).",
        )]
    return []


def _inv_cuartiles(datos: dict) -> list[Hallazgo]:
    q1 = obtener(datos, "anomalos.q1")
    q3 = obtener(datos, "anomalos.q3")
    if isinstance(q1, _NUMERO) and isinstance(q3, _NUMERO) and q1 >= q3:
        return [Hallazgo(
            BLOQUEANTE, "anomalos.q1",
            f"el cuartil inferior ({q1}) debe ser menor que el superior ({q3}).",
        )]
    return []


def _inv_intervalo_calculo(datos: dict) -> list[Hallazgo]:
    tormenta = obtener(datos, "tormenta.intervalo_calculo_min")
    control = obtener(datos, "hec_hms.control.intervalo_min")
    if isinstance(tormenta, int) and isinstance(control, int) and tormenta != control:
        return [Hallazgo(
            BLOQUEANTE, "hec_hms.control.intervalo_min",
            f"vale {control} min y tormenta.intervalo_calculo_min vale "
            f"{tormenta} min. Deben coincidir: el hietograma y las "
            "especificaciones de control comparten el paso de tiempo.",
        )]
    return []


def _inv_duracion_multiplo(datos: dict) -> list[Hallazgo]:
    duracion = obtener(datos, "tormenta.duracion_h")
    paso = obtener(datos, "tormenta.intervalo_calculo_min")
    if not (isinstance(duracion, _NUMERO) and isinstance(paso, int) and paso > 0):
        return []
    minutos = duracion * 60.0
    residuo = minutos % paso
    if abs(residuo) > 1e-9 and abs(residuo - paso) > 1e-9:
        return [Hallazgo(
            BLOQUEANTE, "tormenta.duracion_h",
            f"la duración ({duracion} h = {minutos:g} min) no es múltiplo del "
            f"intervalo de cálculo ({paso} min). El hietograma quedaría "
            "incompleto en su último bloque.",
        )]
    return []


def _inv_h3_factor(datos: dict) -> list[Hallazgo]:
    hipotesis = obtener(datos, "tormenta.hipotesis_adoptada")
    if hipotesis != "h3_factor":
        return []
    hallazgos: list[Hallazgo] = []
    valor = obtener(datos, "tormenta.coeficiente_desagregacion.valor")
    fuente = obtener(datos, "tormenta.coeficiente_desagregacion.fuente")
    if valor is None:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "tormenta.coeficiente_desagregacion.valor",
            "la hipótesis adoptada es h3_factor y el coeficiente está sin definir.",
        ))
    if not (isinstance(fuente, str) and fuente.strip()):
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "tormenta.coeficiente_desagregacion.fuente",
            "la hipótesis adoptada es h3_factor y la fuente documental del "
            "coeficiente es obligatoria (CLAUDE.md, sección 7).",
        ))
    return hallazgos


def _inv_hipotesis_evaluada(datos: dict) -> list[Hallazgo]:
    adoptada = obtener(datos, "tormenta.hipotesis_adoptada")
    evaluadas = obtener(datos, "tormenta.hipotesis_p24_a_pd", [])
    if adoptada and isinstance(evaluadas, list) and adoptada not in evaluadas:
        return [Hallazgo(
            BLOQUEANTE, "tormenta.hipotesis_adoptada",
            f"la hipótesis adoptada ({adoptada}) no figura entre las evaluadas "
            f"({', '.join(map(str, evaluadas))}).",
        )]
    return []


def _inv_h2_idf_duracion(datos: dict) -> list[Hallazgo]:
    evaluadas = obtener(datos, "tormenta.hipotesis_p24_a_pd", [])
    if not isinstance(evaluadas, list) or "h2_idf" not in evaluadas:
        return []
    duracion = obtener(datos, "tormenta.duracion_h")
    duraciones = obtener(datos, "idf.duraciones_min", [])
    if not (isinstance(duracion, _NUMERO) and isinstance(duraciones, list)):
        return []
    objetivo = duracion * 60.0
    if not any(isinstance(d, _NUMERO) and abs(d - objetivo) < 1e-9 for d in duraciones):
        return [Hallazgo(
            ADVERTENCIA, "idf.duraciones_min",
            f"la hipótesis h2_idf integra la IDF sobre {objetivo:g} min y esa "
            "duración no está tabulada. El valor se obtendrá por interpolación.",
        )]
    return []


def _inv_distribucion_adoptada(datos: dict) -> list[Hallazgo]:
    adoptada = obtener(datos, "frecuencia.distribucion_adoptada")
    ajustadas = obtener(datos, "frecuencia.distribuciones", [])
    if adoptada and isinstance(ajustadas, list) and adoptada not in ajustadas:
        return [Hallazgo(
            BLOQUEANTE, "frecuencia.distribucion_adoptada",
            f"la distribución forzada ({adoptada}) no figura entre las ajustadas.",
        )]
    return []


def _inv_metodo_complemento(datos: dict) -> list[Hallazgo]:
    adoptado = obtener(datos, "complemento.metodo_adoptado")
    evaluados = obtener(datos, "complemento.metodos_evaluados", [])
    if adoptado and isinstance(evaluados, list) and adoptado not in evaluados:
        return [Hallazgo(
            BLOQUEANTE, "complemento.metodo_adoptado",
            f"el método adoptado ({adoptado}) no figura entre los evaluados.",
        )]
    return []


def _inv_transito_adoptado(datos: dict) -> list[Hallazgo]:
    adoptado = obtener(datos, "hec_hms.transito.metodo_adoptado")
    metodos = obtener(datos, "hec_hms.transito.metodos", [])
    if adoptado and isinstance(metodos, list) and adoptado not in metodos:
        return [Hallazgo(
            BLOQUEANTE, "hec_hms.transito.metodo_adoptado",
            f"el método adoptado ({adoptado}) no figura entre los calculados.",
        )]
    return []


def _inv_caudal_ambiental(datos: dict) -> list[Hallazgo]:
    adoptado = obtener(datos, "caudal_ambiental.metodo_adoptado")
    metodos = obtener(datos, "caudal_ambiental.metodos", [])
    if adoptado and isinstance(metodos, list) and adoptado not in metodos:
        return [Hallazgo(
            BLOQUEANTE, "caudal_ambiental.metodo_adoptado",
            f"el método adoptado ({adoptado}) no figura entre los calculados.",
        )]
    return []


def _inv_deduplicacion(datos: dict) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    clave = obtener(datos, "ideam.deduplicacion.clave", [])
    esperada = ["CodigoEstacion", "Parametro", "Fecha"]
    if isinstance(clave, list) and list(clave) != esperada:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "ideam.deduplicacion.clave",
            f"la clave declarada {clave} difiere de la establecida "
            f"{esperada} (CLAUDE.md, sección 7).",
        ))

    precedencia = obtener(datos, "ideam.deduplicacion.precedencia_aprobacion", [])
    if isinstance(precedencia, list) and precedencia:
        if precedencia[0] != "Definitivo":
            hallazgos.append(Hallazgo(
                BLOQUEANTE, "ideam.deduplicacion.precedencia_aprobacion",
                f"el primer nivel es {precedencia[0]!r}. La precedencia debe ser "
                "Definitivo sobre Preliminar (CLAUDE.md, sección 7); invertirla "
                "descartaría el dato aprobado.",
            ))
    return hallazgos


def _inv_completitud_mensual(datos: dict) -> list[Hallazgo]:
    maximo = obtener(datos, "ideam.agregacion_diaria_a_mensual.max_dias_faltantes")
    if isinstance(maximo, int) and not isinstance(maximo, bool) and maximo > 5:
        return [Hallazgo(
            ADVERTENCIA, "ideam.agregacion_diaria_a_mensual.max_dias_faltantes",
            f"admitir {maximo} días faltantes al totalizar un mes subestima el "
            "acumulado mensual (CLAUDE.md, sección 7).",
        )]
    return []


def _inv_rezago_transform(datos: dict) -> list[Hallazgo]:
    if obtener(datos, "tiempo_rezago.validar_coherencia_con_transform") is not True:
        return []
    transform = obtener(datos, "hec_hms.transform")
    if isinstance(transform, str) and transform not in TRANSFORMACIONES_CON_REZAGO:
        return [Hallazgo(
            ADVERTENCIA, "hec_hms.transform",
            f"el método de transformación {transform!r} no consume el tiempo de "
            "rezago como parámetro de entrada, y tiempo_rezago está configurado. "
            "Verificar la coherencia del parámetro calculado (CLAUDE.md, sección 7).",
        )]
    return []


def _inv_min_formulas_tc(datos: dict) -> list[Hallazgo]:
    minimo = obtener(datos, "tiempo_concentracion.min_formulas_aplicables")
    if isinstance(minimo, int) and not isinstance(minimo, bool) and minimo < 5:
        return [Hallazgo(
            ADVERTENCIA, "tiempo_concentracion.min_formulas_aplicables",
            f"se admite adoptar la mediana con {minimo} fórmulas. CLAUDE.md, "
            "sección 7, fija cinco como mínimo antes de adoptarla de forma "
            "automática.",
        )]
    return []


def _inv_cambio_climatico(datos: dict) -> list[Hallazgo]:
    if obtener(datos, "cambio_climatico.aplicar") is not True:
        return []
    if obtener(datos, "cambio_climatico.solo_si_incremento") is not True:
        return [Hallazgo(
            ADVERTENCIA, "cambio_climatico.solo_si_incremento",
            "está en false. La regla condicional de CLAUDE.md, sección 6, aplica "
            "el factor solo si representa incremento; permitir factores a la baja "
            "reduce el caudal de diseño y debe justificarse de forma explícita.",
        )]
    return []


def _inv_ventana_asf(datos: dict) -> list[Hallazgo]:
    """Verifica la ventana de adquisición declarada para las escenas."""
    import re

    inicio = obtener(datos, "dem.asf.fecha_inicio")
    fin = obtener(datos, "dem.asf.fecha_fin")
    hallazgos: list[Hallazgo] = []

    patron = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for clave, valor in (("dem.asf.fecha_inicio", inicio),
                         ("dem.asf.fecha_fin", fin)):
        if valor is None:
            continue
        if not isinstance(valor, str) or not patron.match(valor.strip()):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, clave,
                f"{valor!r} no tiene el formato AAAA-MM-DD.",
            ))

    if isinstance(inicio, str) and isinstance(fin, str) and not hallazgos:
        if inicio.strip() >= fin.strip():
            hallazgos.append(Hallazgo(
                BLOQUEANTE, "dem.asf.fecha_inicio",
                f"la fecha de inicio ({inicio}) no es anterior a la de fin ({fin}).",
            ))
        else:
            hallazgos.append(Hallazgo(
                INFORMATIVO, "dem.asf",
                f"búsqueda restringida a las trayectorias adquiridas entre "
                f"{inicio} y {fin}. Verificar en el reporte del M02 que la "
                "cobertura del área siga siendo completa.",
            ))

    if inicio is None and fin is None:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "dem.asf",
            "sin ventana de adquisición: se usa todo el catálogo disponible.",
        ))

    return hallazgos


def _inv_qgis_ltr(datos: dict) -> list[Hallazgo]:
    if obtener(datos, "entornos.qgis.es_ltr") is False:
        version = obtener(datos, "entornos.qgis.version", "sin declarar")
        return [Hallazgo(
            ADVERTENCIA, "entornos.qgis.es_ltr",
            f"la versión declarada de QGIS ({version}) no es LTR. CLAUDE.md, "
            "sección 3, adopta el esquema LTR; los módulos SIG quedan expuestos "
            "a cambios de API en actualizaciones menores. La decisión debe estar "
            "registrada en MANIFIESTO.yaml.",
        )]
    return []


def _inv_crs(datos: dict) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    calculo = obtener(datos, "crs.calculo")
    geografico = obtener(datos, "crs.geografico")
    if calculo != "EPSG:9377":
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "crs.calculo",
            f"el CRS de cálculo es {calculo!r}. La convención del repositorio es "
            "EPSG:9377, MAGNA-SIRGAS / Origen Nacional CTM12 (CLAUDE.md, sección 5).",
        ))
    if geografico != "EPSG:4326":
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "crs.geografico",
            f"el CRS de consulta es {geografico!r} y la convención es EPSG:4326.",
        ))
    if obtener(datos, "crs.reproyeccion_explicita") is not True:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "crs.reproyeccion_explicita",
            "está en false. La convención exige reproyección siempre explícita.",
        ))
    return hallazgos


def _inv_resolucion_interpolacion(datos: dict) -> list[Hallazgo]:
    dem = obtener(datos, "dem.resolucion_m")
    raster = obtener(datos, "interpolacion.resolucion_raster_m")
    if isinstance(dem, _NUMERO) and isinstance(raster, _NUMERO) and raster < dem:
        return [Hallazgo(
            ADVERTENCIA, "interpolacion.resolucion_raster_m",
            f"la superficie interpolada ({raster} m) es más fina que el DEM "
            f"({dem} m). El detalle adicional es aparente, no real.",
        )]
    return []


# CRS geográficos cuyas coordenadas el M00 puede verificar sin reproyectar. Para
# cualquier otro, la comprobación de envolvente se difiere al M01, que corre
# bajo QGIS y sí puede reproyectar.
CRS_GEOGRAFICOS = ("EPSG:4326", "EPSG:4686", "EPSG:4269", "CRS:84")

# Envolvente aproximada del territorio continental e insular colombiano.
_ENVOLVENTE_COLOMBIA = (-82.0, -4.5, -66.5, 13.5)  # lon_min, lat_min, lon_max, lat_max


def _inv_punto_descarga(datos: dict) -> list[Hallazgo]:
    x = obtener(datos, "punto_descarga.x")
    y = obtener(datos, "punto_descarga.y")
    crs = str(obtener(datos, "punto_descarga.crs", "") or "").upper().replace(" ", "")

    if x is None or y is None:
        return [Hallazgo(
            ADVERTENCIA, "punto_descarga",
            "el punto de descarga está sin definir. El M01 y toda la cadena "
            "aguas abajo no pueden ejecutarse hasta que se declare.",
        )]

    hallazgos: list[Hallazgo] = []

    if crs in CRS_GEOGRAFICOS:
        lon_min, lat_min, lon_max, lat_max = _ENVOLVENTE_COLOMBIA

        if abs(x) > 180 or abs(y) > 90:
            return [Hallazgo(
                BLOQUEANTE, "punto_descarga",
                f"el CRS declarado ({crs}) es geográfico y las coordenadas "
                f"({x}, {y}) exceden el rango de grados decimales. Revisar si "
                "en realidad son coordenadas proyectadas.",
            )]

        # El error de transcripción más frecuente: intercambiar x e y. En
        # Colombia la longitud es siempre negativa y la latitud está entre -4 y
        # 14, de modo que el intercambio es detectable.
        if lon_min <= y <= lon_max and lat_min <= x <= lat_max:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, "punto_descarga",
                f"las coordenadas ({x}, {y}) parecen intercambiadas: en un CRS "
                "geográfico x es la longitud e y la latitud.",
            ))
        elif not (lon_min <= x <= lon_max) or not (lat_min <= y <= lat_max):
            hallazgos.append(Hallazgo(
                ADVERTENCIA, "punto_descarga",
                f"las coordenadas ({x}, {y}) quedan fuera de la envolvente de "
                "Colombia. Verificar el orden x, y y el CRS declarado.",
            ))
        return hallazgos

    # CRS proyectado o no reconocido como geográfico.
    if abs(x) <= 180 and abs(y) <= 90:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "punto_descarga",
            f"el CRS declarado ({crs or 'sin declarar'}) no es geográfico y las "
            f"coordenadas ({x}, {y}) tienen magnitud de grados decimales. "
            "Verificar si el CRS declarado es el correcto.",
        ))
    else:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "punto_descarga",
            f"punto declarado en {crs}. La verificación de que caiga dentro de "
            "Colombia se difiere al M01, que reproyecta con QGIS.",
        ))
    return hallazgos


def _inv_categorias_estacion(datos: dict) -> list[Hallazgo]:
    bloque = obtener(datos, "estaciones.categorias_por_variable", {})
    if not isinstance(bloque, dict):
        return []
    hallazgos: list[Hallazgo] = []
    for variable, categorias in bloque.items():
        if not isinstance(categorias, list):
            continue
        desconocidas = [
            c for c in categorias
            if isinstance(c, str) and c not in CATEGORIAS_IDEAM
        ]
        if desconocidas:
            hallazgos.append(Hallazgo(
                ADVERTENCIA, f"estaciones.categorias_por_variable.{variable}",
                f"categorías no reconocidas en el Catálogo Nacional de Estaciones: "
                f"{', '.join(desconocidas)}.",
            ))
    return hallazgos


def _inv_ventanas(datos: dict) -> list[Hallazgo]:
    ventanas = obtener(datos, "sensibilidad_series.ventanas", [])
    if not isinstance(ventanas, list):
        return []
    hallazgos: list[Hallazgo] = []
    for indice, ventana in enumerate(ventanas):
        clave = f"sensibilidad_series.ventanas[{indice}]"
        if not isinstance(ventana, list) or len(ventana) != 2:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, clave,
                "cada ventana debe ser una pareja [anio_inicio, anio_fin]; "
                "null en el fin significa el año del estudio.",
            ))
            continue
        inicio, fin = ventana
        for extremo, valor in (("inicio", inicio), ("fin", fin)):
            if valor is None:
                continue
            if not isinstance(valor, int) or isinstance(valor, bool):
                hallazgos.append(Hallazgo(
                    BLOQUEANTE, clave,
                    f"el año de {extremo} ({valor!r}) no es un entero ni null.",
                ))
            elif not (1900 <= valor <= 2100):
                hallazgos.append(Hallazgo(
                    BLOQUEANTE, clave,
                    f"el año de {extremo} ({valor}) está fuera de 1900 a 2100.",
                ))
        if isinstance(inicio, int) and isinstance(fin, int) and inicio >= fin:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, clave,
                f"el año de inicio ({inicio}) no es anterior al de fin ({fin}).",
            ))
    return hallazgos


def _inv_umbral_adoptado(datos: dict) -> list[Hallazgo]:
    adoptado = obtener(datos, "sensibilidad_series.umbral_adoptado_anios")
    umbrales = obtener(datos, "sensibilidad_series.umbrales_anios", [])
    if adoptado is None:
        return [Hallazgo(
            INFORMATIVO, "sensibilidad_series.umbral_adoptado_anios",
            "sin definir. El M04b producirá la matriz de sensibilidad y el "
            "consultor debe fijar el umbral antes del M05.",
        )]
    if isinstance(umbrales, list) and adoptado not in umbrales:
        return [Hallazgo(
            ADVERTENCIA, "sensibilidad_series.umbral_adoptado_anios",
            f"el umbral adoptado ({adoptado} años) no figura entre los evaluados "
            f"({umbrales}). La decisión no quedará respaldada por la matriz.",
        )]
    return []


def _inv_cdc_irh(datos: dict) -> list[Hallazgo]:
    menor = obtener(datos, "caudal_ambiental.cdc_si_irh_menor")
    mayor = obtener(datos, "caudal_ambiental.cdc_si_irh_mayor")
    if isinstance(menor, _NUMERO) and isinstance(mayor, _NUMERO) and menor >= mayor:
        return [Hallazgo(
            ADVERTENCIA, "caudal_ambiental.cdc_si_irh_menor",
            f"el percentil para IRH bajo ({menor}) no es menor que el de IRH alto "
            f"({mayor}). Una regulación menor exige un percentil más exigente.",
        )]
    return []


def _inv_decisiones_pendientes(datos: dict) -> list[Hallazgo]:
    """Registra como informativas las decisiones que el consultor aún no fijó."""
    pendientes = {
        "complemento.metodo_adoptado": "método de relleno (M05)",
        "frecuencia.distribucion_adoptada":
            "distribución de frecuencia (M07); null activa la selección automática "
            "por criterio de información",
        "tormenta.hipotesis_adoptada": "hipótesis de desagregación P24h a P3h (M12b)",
    }
    hallazgos: list[Hallazgo] = []
    for clave, descripcion in pendientes.items():
        if obtener(datos, clave, _AUSENTE) is None:
            hallazgos.append(Hallazgo(
                INFORMATIVO, clave, f"sin definir: {descripcion}.",
            ))
    return hallazgos


INVARIANTES: tuple[Callable[[dict], list[Hallazgo]], ...] = (
    _inv_anomalos_maximos,
    _inv_enso_no_elimina,
    _inv_cuartiles,
    _inv_intervalo_calculo,
    _inv_duracion_multiplo,
    _inv_h3_factor,
    _inv_hipotesis_evaluada,
    _inv_h2_idf_duracion,
    _inv_distribucion_adoptada,
    _inv_metodo_complemento,
    _inv_transito_adoptado,
    _inv_caudal_ambiental,
    _inv_deduplicacion,
    _inv_completitud_mensual,
    _inv_rezago_transform,
    _inv_min_formulas_tc,
    _inv_cambio_climatico,
    _inv_ventana_asf,
    _inv_qgis_ltr,
    _inv_crs,
    _inv_resolucion_interpolacion,
    _inv_punto_descarga,
    _inv_categorias_estacion,
    _inv_ventanas,
    _inv_umbral_adoptado,
    _inv_cdc_irh,
    _inv_decisiones_pendientes,
)


def validar_invariantes(datos: dict) -> list[Hallazgo]:
    """Aplica las reglas cruzadas entre claves."""
    hallazgos: list[Hallazgo] = []
    for invariante in INVARIANTES:
        hallazgos.extend(invariante(datos))
    return hallazgos


# =============================================================================
# Verificación de rutas declaradas
# =============================================================================
def _validar_ubicacion_credenciales(datos: dict, base: Path) -> list[Hallazgo]:
    """
    Impide que el archivo de credenciales viva dentro del repositorio.

    La carpeta del proyecto se comprime y se entrega como anexo. El .gitignore
    protege el historial de versiones, no una copia ni un .zip: una contraseña
    depositada ahí acabaría en manos del cliente o de la interventoría.
    """
    declarada = obtener(datos, "dem.earthdata.ruta_netrc")
    if not isinstance(declarada, str) or not declarada.strip():
        return []

    candidata = Path(declarada).expanduser()
    try:
        absoluta = candidata.resolve()
    except OSError:  # pragma: no cover - ruta con caracteres inválidos
        return [Hallazgo(
            BLOQUEANTE, "dem.earthdata.ruta_netrc",
            f"la ruta declarada no se pudo resolver: {declarada!r}",
        )]

    if not candidata.is_absolute():
        return [Hallazgo(
            BLOQUEANTE, "dem.earthdata.ruta_netrc",
            f"{declarada!r} es una ruta relativa, de modo que se resolvería "
            "dentro del repositorio. Declarar una ruta absoluta fuera de él.",
        )]

    try:
        absoluta.relative_to(base.resolve())
    except ValueError:
        return [Hallazgo(
            INFORMATIVO, "dem.earthdata.ruta_netrc",
            f"archivo de credenciales declarado fuera del repositorio: {absoluta}",
        )]

    return [Hallazgo(
        BLOQUEANTE, "dem.earthdata.ruta_netrc",
        f"{absoluta} está dentro del repositorio. La carpeta del proyecto se "
        "comprime y se entrega como anexo, y el .gitignore no alcanza a un .zip "
        "ni a una copia: la contraseña saldría con el entregable. Declarar una "
        "ruta fuera del repositorio, o dejar la clave en null para usar "
        "~/.netrc.",
    )]


def _validar_referencia_nacional(datos: dict, base: Path) -> list[Hallazgo]:
    """
    Verifica que las capas nacionales vivan fuera del repositorio.

    El drenaje 1:100.000 del IGAC ocupa 645 MB. Dentro del árbol del proyecto
    haría inmanejable el control de versiones y multiplicaría el tamaño del
    entregable, sin aportar nada: el mismo archivo sirve a todos los estudios de
    la máquina. Lo que sí se versiona es el recorte al área de influencia.
    """
    declarado = obtener(datos, "referencia_nacional.directorio")
    if not isinstance(declarado, str) or not declarado.strip():
        return []

    candidato = Path(declarado).expanduser()
    if not candidato.is_absolute():
        return [Hallazgo(
            BLOQUEANTE, "referencia_nacional.directorio",
            f"{declarado!r} es una ruta relativa, de modo que se resolvería "
            "dentro del repositorio. Declarar una ruta absoluta fuera de él.",
        )]

    hallazgos: list[Hallazgo] = []
    try:
        candidato.resolve().relative_to(base.resolve())
    except ValueError:
        pass
    else:
        return [Hallazgo(
            BLOQUEANTE, "referencia_nacional.directorio",
            f"{candidato} está dentro del repositorio. Las capas nacionales "
            "ocupan cientos de megabytes y no deben versionarse ni viajar en el "
            "entregable. Declarar un directorio externo, compartido por todos "
            "los estudios de la máquina.",
        )]

    if not candidato.is_dir():
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "referencia_nacional.directorio",
            f"el directorio declarado no existe: {candidato}. Los módulos que "
            "usen el drenaje del IGAC se detendrán hasta que se cree y se "
            "copien las capas.",
        ))
        return hallazgos

    for clave in ("drenaje_sencillo", "drenaje_doble"):
        nombre = obtener(datos, f"referencia_nacional.{clave}")
        if isinstance(nombre, str) and nombre.strip():
            if not (candidato / nombre).is_file():
                hallazgos.append(Hallazgo(
                    ADVERTENCIA, f"referencia_nacional.{clave}",
                    f"no se encuentra {nombre} en {candidato}.",
                ))

    return hallazgos


def validar_rutas(
    datos: dict,
    raiz: str | os.PathLike,
) -> list[Hallazgo]:
    """
    Verifica la existencia de los archivos y directorios declarados.

    Toda ausencia es advertencia: los insumos se incorporan progresivamente. El
    módulo que necesite el archivo debe detenerse por su cuenta si no está.
    """
    base = Path(raiz)
    hallazgos: list[Hallazgo] = []

    hallazgos.extend(_validar_ubicacion_credenciales(datos, base))
    hallazgos.extend(_validar_referencia_nacional(datos, base))

    for clave in CLAVES_RUTA:
        valor = obtener(datos, clave)
        if not isinstance(valor, str) or not valor.strip():
            continue
        candidata = Path(valor)
        destino = candidata if candidata.is_absolute() else base / candidata
        if destino.exists():
            continue

        externa = clave in _RUTAS_EXTERNAS
        detalle = (
            "software externo no encontrado en la ruta declarada"
            if externa else "el archivo o directorio declarado no existe"
        )
        hallazgos.append(Hallazgo(
            ADVERTENCIA, clave, f"{detalle}: {valor}",
        ))

    return hallazgos


# =============================================================================
# Entrada pública
# =============================================================================
def validar(
    datos: dict,
    raiz: str | os.PathLike | None = None,
    verificar_rutas: bool = True,
) -> list[Hallazgo]:
    """
    Valida la configuración completa y devuelve los hallazgos ordenados.

    Parámetros
    ----------
    datos:
        Diccionario cargado desde config/config.yaml.
    raiz:
        Raíz del repositorio, necesaria para verificar las rutas declaradas.
    verificar_rutas:
        Permite desactivar la comprobación de existencia, útil en pruebas.

    Devuelve
    --------
    Lista de Hallazgo ordenada por severidad y luego por clave. La lista vacía
    significa que la configuración es válida y no plantea reservas técnicas.
    """
    if not isinstance(datos, dict):
        return [Hallazgo(
            BLOQUEANTE, "<raiz>",
            f"el archivo debe contener un bloque de claves y contiene "
            f"{type(datos).__name__}.",
        )]

    hallazgos = validar_estructura(datos)
    hallazgos.extend(validar_invariantes(datos))
    if verificar_rutas and raiz is not None:
        hallazgos.extend(validar_rutas(datos, raiz))

    return sorted(
        hallazgos,
        key=lambda h: (_ORDEN_SEVERIDAD.get(h.severidad, 9), h.clave),
    )


def hay_bloqueantes(hallazgos: Iterable[Hallazgo]) -> bool:
    """Indica si algún hallazgo impide usar la configuración."""
    return any(h.es_bloqueante for h in hallazgos)


def resumen_por_severidad(hallazgos: Iterable[Hallazgo]) -> dict[str, int]:
    """Cuenta los hallazgos por severidad, incluidas las categorías en cero."""
    conteo = {BLOQUEANTE: 0, ADVERTENCIA: 0, INFORMATIVO: 0}
    for hallazgo in hallazgos:
        conteo[hallazgo.severidad] = conteo.get(hallazgo.severidad, 0) + 1
    return conteo
