#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M00b - Constructor del proyecto QGIS
====================================
Entorno: Python de QGIS (OSGeo4W Shell en Windows). No importa ninguna librería
del venv del proyecto.

Construye data/03_SIG/proyecto/estudio_hidrologico.qgz a partir del árbol de
grupos y capas declarado en config/proyecto_qgis.yaml.

Política de regeneración: el proyecto se reconstruye por completo en cada
ejecución. Es seguro porque la simbología no vive dentro del .qgz sino en los
archivos .qml del directorio de estilos, que el módulo aplica pero nunca
sobrescribe una vez existen. Lo que el consultor ajuste en QGIS y guarde como
.qml sobrevive a la siguiente regeneración.

La regeneración es determinista en estructura: mismo orden de grupos, mismo
orden de capas y patrones resueltos en orden alfabético. No es determinista a
nivel de bytes, porque QGIS incorpora identificadores de capa con marca de
tiempo al escribir el archivo.

Una capa declarada cuyo archivo aún no existe no es un error: significa que el
módulo que la produce todavía no se ha ejecutado. Se reporta como informativa y
el módulo continúa, salvo que proyecto_qgis.detener_si_falta_capa sea true.

Uso:
    "C:/Program Files/QGIS 4.2.0/bin/python-qgis.bat" src/M00b_proyecto_qgis.py
    ... src/M00b_proyecto_qgis.py --solo-validar
    ... src/M00b_proyecto_qgis.py --json logs/M00b_reporte.json

Códigos de salida:
    0  proyecto escrito sin hallazgos bloqueantes
    1  hay hallazgos bloqueantes; el proyecto no se escribió
    3  no se pudo leer la configuración, la declaración o inicializar QGIS
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import sig  # noqa: E402
from comun import entorno, esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorEntorno,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M00b"
DESCRIPCION = "Constructor del proyecto QGIS"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

TIPOS_CAPA = ("vector", "raster")
PROVEEDOR = {"vector": "ogr", "raster": "gdal"}


# =============================================================================
# Estructuras de la declaración
# =============================================================================
@dataclass(frozen=True)
class CapaDeclarada:
    """Una capa del proyecto, ya resuelta a una ruta concreta del disco."""

    id: str
    nombre: str
    tipo: str
    ruta: Path
    modulo: str
    visible: bool
    estilo: str
    grupo: tuple[str, ...]
    origen: str  # 'capa' si se declaró de forma explícita, 'patron' si se expandió

    @property
    def existe(self) -> bool:
        return self.ruta.is_file()

    @property
    def ubicacion(self) -> str:
        return " / ".join(self.grupo) if self.grupo else "(raíz)"


@dataclass(frozen=True)
class GrupoDeclarado:
    """Un grupo del árbol de capas, con sus capas y subgrupos ya resueltos."""

    nombre: str
    expandido: bool
    visible: bool
    capas: tuple[CapaDeclarada, ...] = ()
    subgrupos: tuple["GrupoDeclarado", ...] = ()


@dataclass
class ResultadoConstruccion:
    """Lo que el módulo produjo y lo que encontró al hacerlo."""

    archivo: Path | None = None
    capas_cargadas: int = 0
    capas_ausentes: int = 0
    estilos_aplicados: int = 0
    estilos_creados: int = 0
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Validación de la declaración (funciones puras, no requieren QGIS)
# =============================================================================
_CLAVES_CAPA = {"id", "nombre", "tipo", "ruta", "modulo", "visible", "estilo"}
_CLAVES_PATRON = {"id", "nombre", "tipo", "patron", "modulo", "visible", "estilo"}
_CLAVES_GRUPO = {"nombre", "expandido", "visible", "capas", "patrones", "grupos"}


def _validar_entrada(
    entrada: Any,
    ubicacion: str,
    claves_admitidas: set[str],
    clave_ruta: str,
) -> list[Hallazgo]:
    """Valida una entrada de capa o de patrón dentro de un grupo."""
    hallazgos: list[Hallazgo] = []

    if not isinstance(entrada, dict):
        return [Hallazgo(
            BLOQUEANTE, ubicacion,
            f"cada entrada debe ser un bloque de claves y se recibió "
            f"{type(entrada).__name__}.",
        )]

    for obligatoria in ("id", "nombre", "tipo", clave_ruta):
        valor = entrada.get(obligatoria)
        if not isinstance(valor, str) or not valor.strip():
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{ubicacion}.{obligatoria}",
                "es obligatoria y debe ser un texto no vacío.",
            ))

    tipo = entrada.get("tipo")
    if isinstance(tipo, str) and tipo not in TIPOS_CAPA:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"{ubicacion}.tipo",
            f"valor {tipo!r} no admitido. Debe ser uno de: {', '.join(TIPOS_CAPA)}.",
        ))

    visible = entrada.get("visible", False)
    if not isinstance(visible, bool):
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"{ubicacion}.visible",
            f"debe ser booleano y se recibió {type(visible).__name__}.",
        ))

    estilo = entrada.get("estilo")
    if estilo is not None:
        if not isinstance(estilo, str) or not estilo.strip():
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{ubicacion}.estilo",
                "debe ser el nombre de un archivo .qml o quedar sin declarar.",
            ))
        elif not estilo.endswith(".qml"):
            hallazgos.append(Hallazgo(
                ADVERTENCIA, f"{ubicacion}.estilo",
                f"{estilo!r} no termina en .qml. QGIS solo aplica estilos con esa "
                "extensión.",
            ))
        elif Path(estilo).name != estilo:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{ubicacion}.estilo",
                f"{estilo!r} debe ser un nombre de archivo, sin directorios. La "
                "carpeta se declara en config.yaml (proyecto_qgis.estilos).",
            ))

    desconocidas = set(entrada) - claves_admitidas
    for clave in sorted(desconocidas):
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"{ubicacion}.{clave}",
            "clave no reconocida por el M00b; se ignorará.",
        ))

    return hallazgos


def validar_declaracion(datos: Any) -> list[Hallazgo]:
    """
    Valida la estructura de config/proyecto_qgis.yaml.

    Devuelve la lista completa de hallazgos, ordenada por severidad. No accede
    al disco ni a QGIS: es verificable sin el entorno SIG.
    """
    if not isinstance(datos, dict):
        return [Hallazgo(
            BLOQUEANTE, "<raiz>",
            f"la declaración debe ser un bloque de claves y es "
            f"{type(datos).__name__}.",
        )]

    hallazgos: list[Hallazgo] = []

    version = datos.get("version")
    if version != 1:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "version",
            f"versión de formato {version!r} no soportada. El M00b lee la versión 1.",
        ))

    grupos = datos.get("grupos")
    if not isinstance(grupos, list) or not grupos:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "grupos",
            "debe ser una lista no vacía de grupos.",
        ))
        return sorted(hallazgos, key=_orden_hallazgo)

    identificadores: dict[str, str] = {}
    hallazgos.extend(_validar_grupos(grupos, "grupos", identificadores))
    return sorted(hallazgos, key=_orden_hallazgo)


def _validar_grupos(
    grupos: list,
    prefijo: str,
    identificadores: dict[str, str],
) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []

    for indice, grupo in enumerate(grupos):
        ubicacion = f"{prefijo}[{indice}]"

        if not isinstance(grupo, dict):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, ubicacion,
                f"cada grupo debe ser un bloque de claves y se recibió "
                f"{type(grupo).__name__}.",
            ))
            continue

        nombre = grupo.get("nombre")
        if not isinstance(nombre, str) or not nombre.strip():
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{ubicacion}.nombre",
                "es obligatorio y debe ser un texto no vacío.",
            ))

        for clave in ("expandido", "visible"):
            valor = grupo.get(clave, False)
            if not isinstance(valor, bool):
                hallazgos.append(Hallazgo(
                    BLOQUEANTE, f"{ubicacion}.{clave}",
                    f"debe ser booleano y se recibió {type(valor).__name__}.",
                ))

        for clave in sorted(set(grupo) - _CLAVES_GRUPO):
            hallazgos.append(Hallazgo(
                ADVERTENCIA, f"{ubicacion}.{clave}",
                "clave no reconocida por el M00b; se ignorará.",
            ))

        for clave, claves_admitidas, clave_ruta in (
            ("capas", _CLAVES_CAPA, "ruta"),
            ("patrones", _CLAVES_PATRON, "patron"),
        ):
            entradas = grupo.get(clave, [])
            if entradas is None:
                continue
            if not isinstance(entradas, list):
                hallazgos.append(Hallazgo(
                    BLOQUEANTE, f"{ubicacion}.{clave}",
                    f"debe ser una lista y se recibió {type(entradas).__name__}.",
                ))
                continue
            for sub_indice, entrada in enumerate(entradas):
                sub_ubicacion = f"{ubicacion}.{clave}[{sub_indice}]"
                hallazgos.extend(_validar_entrada(
                    entrada, sub_ubicacion, claves_admitidas, clave_ruta
                ))
                if isinstance(entrada, dict):
                    hallazgos.extend(
                        _registrar_identificador(entrada, sub_ubicacion, identificadores)
                    )

        subgrupos = grupo.get("grupos")
        if subgrupos is not None:
            if not isinstance(subgrupos, list):
                hallazgos.append(Hallazgo(
                    BLOQUEANTE, f"{ubicacion}.grupos",
                    f"debe ser una lista y se recibió {type(subgrupos).__name__}.",
                ))
            else:
                hallazgos.extend(_validar_grupos(
                    subgrupos, f"{ubicacion}.grupos", identificadores
                ))

    return hallazgos


def _registrar_identificador(
    entrada: dict,
    ubicacion: str,
    identificadores: dict[str, str],
) -> list[Hallazgo]:
    """
    Verifica que el id de cada capa sea único en toda la declaración.

    Un id repetido produciría dos estilos con el mismo nombre y haría ambiguo
    cualquier reporte posterior sobre la capa.
    """
    identificador = entrada.get("id")
    if not isinstance(identificador, str) or not identificador.strip():
        return []
    if identificador in identificadores:
        return [Hallazgo(
            BLOQUEANTE, f"{ubicacion}.id",
            f"el identificador {identificador!r} ya se usó en "
            f"{identificadores[identificador]}. Debe ser único.",
        )]
    identificadores[identificador] = ubicacion
    return []


def _orden_hallazgo(hallazgo: Hallazgo) -> tuple[int, str]:
    orden = {BLOQUEANTE: 0, ADVERTENCIA: 1, INFORMATIVO: 2}
    return (orden.get(hallazgo.severidad, 9), hallazgo.clave)


# =============================================================================
# Expansión de la declaración a capas concretas
# =============================================================================
def expandir_grupos(
    grupos: list,
    raiz: Path,
    ruta_grupos: tuple[str, ...] = (),
) -> tuple[tuple[GrupoDeclarado, ...], list[Hallazgo]]:
    """
    Resuelve la declaración a rutas concretas del disco.

    Los patrones se expanden en orden alfabético para que dos ejecuciones
    produzcan el mismo orden de capas. Un patrón sin coincidencias no es un
    error: el módulo que produce esas capas aún no se ha ejecutado.
    """
    resueltos: list[GrupoDeclarado] = []
    hallazgos: list[Hallazgo] = []

    for grupo in grupos:
        nombre = str(grupo.get("nombre", "")).strip()
        ruta_actual = ruta_grupos + (nombre,)

        capas: list[CapaDeclarada] = []

        for entrada in grupo.get("capas") or []:
            capas.append(CapaDeclarada(
                id=entrada["id"],
                nombre=entrada["nombre"],
                tipo=entrada["tipo"],
                ruta=rutas.resolver(entrada["ruta"], raiz),
                modulo=str(entrada.get("modulo", "")),
                visible=bool(entrada.get("visible", False)),
                estilo=str(entrada.get("estilo") or f"{entrada['id']}.qml"),
                grupo=ruta_actual,
                origen="capa",
            ))

        for entrada in grupo.get("patrones") or []:
            coincidencias = sorted(raiz.glob(entrada["patron"]))
            if not coincidencias:
                hallazgos.append(Hallazgo(
                    INFORMATIVO, f"patron:{entrada['id']}",
                    f"sin coincidencias para {entrada['patron']!r}. El módulo "
                    f"{entrada.get('modulo', 'correspondiente')} aún no ha "
                    f"producido estas capas.",
                ))
                continue
            for coincidencia in coincidencias:
                # El nombre del archivo carga la etiqueta de escenario
                # (hipótesis, escenario de cambio climático, periodo de retorno),
                # de modo que es el rótulo más informativo para la leyenda.
                capas.append(CapaDeclarada(
                    id=f"{entrada['id']}__{coincidencia.stem}",
                    nombre=coincidencia.stem,
                    tipo=entrada["tipo"],
                    ruta=coincidencia,
                    modulo=str(entrada.get("modulo", "")),
                    visible=bool(entrada.get("visible", False)),
                    estilo=str(entrada.get("estilo") or f"{entrada['id']}.qml"),
                    grupo=ruta_actual,
                    origen="patron",
                ))

        subgrupos, sub_hallazgos = expandir_grupos(
            grupo.get("grupos") or [], raiz, ruta_actual
        )
        hallazgos.extend(sub_hallazgos)

        resueltos.append(GrupoDeclarado(
            nombre=nombre,
            expandido=bool(grupo.get("expandido", False)),
            visible=bool(grupo.get("visible", True)),
            capas=tuple(capas),
            subgrupos=subgrupos,
        ))

    return tuple(resueltos), hallazgos


def recorrer_capas(grupos: Iterable[GrupoDeclarado]) -> list[CapaDeclarada]:
    """Devuelve todas las capas del árbol, en orden de construcción."""
    acumulado: list[CapaDeclarada] = []
    for grupo in grupos:
        acumulado.extend(grupo.capas)
        acumulado.extend(recorrer_capas(grupo.subgrupos))
    return acumulado


def revisar_disponibilidad(
    capas: Sequence[CapaDeclarada],
    detener_si_falta: bool,
) -> list[Hallazgo]:
    """
    Reporta las capas declaradas cuyo archivo aún no existe.

    Severidad informativa por defecto: en el estado inicial del estudio ninguna
    capa existe todavía, y tratarlo como advertencia inundaría el reporte sin
    aportar información.
    """
    severidad = BLOQUEANTE if detener_si_falta else INFORMATIVO
    return [
        Hallazgo(
            severidad, f"capa:{capa.id}",
            f"el archivo no existe: {capa.ruta.name}. Lo produce el módulo "
            f"{capa.modulo or 'no declarado'}.",
        )
        for capa in capas
        if capa.origen == "capa" and not capa.existe
    ]


# El ciclo de vida de QGIS vive en src/sig.py, compartido por los módulos del
# entorno SIG. QGIS no admite reinicializarse dentro del mismo proceso: una
# segunda pareja initQgis/exitQgis produce una violación de acceso que mata el
# intérprete sin traza de Python.
iniciar_qgis = sig.iniciar_qgis
finalizar_qgis = sig.finalizar_qgis


# =============================================================================
# Construcción del proyecto (requiere QGIS)
# =============================================================================
def construir_proyecto(
    grupos: Sequence[GrupoDeclarado],
    destino: Path,
    directorio_estilos: Path,
    crs_calculo: str,
    titulo: str,
    prefix_path: str,
    escribir_estilo_inicial: bool = True,
    logger: Any = None,
) -> ResultadoConstruccion:
    """
    Construye y escribe el archivo .qgz.

    La importación de qgis.core se hace aquí y no al inicio del archivo para que
    las funciones puras de este módulo sigan siendo importables y verificables
    desde el venv, donde la API de QGIS no está disponible.

    No cierra la aplicación QGIS: puede invocarse varias veces en el mismo
    proceso. El cierre corresponde a finalizar_qgis(), que llama el punto de
    entrada una sola vez.

    Excepciones
    -----------
    ErrorEntorno
        Si la API de QGIS no se puede importar o inicializar.
    """
    iniciar_qgis(prefix_path)

    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsProject,
        QgsRasterLayer,
        QgsRectangle,
        QgsReferencedRectangle,
        QgsVectorLayer,
    )

    resultado = ResultadoConstruccion()

    crs = QgsCoordinateReferenceSystem(crs_calculo)
    if not crs.isValid():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "crs.calculo",
            f"QGIS no reconoce el CRS {crs_calculo!r}. El proyecto no se escribe.",
        ))
        return resultado

    proyecto = QgsProject.instance()
    proyecto.clear()
    proyecto.setCrs(crs)
    proyecto.setTitle(titulo)

    directorio_estilos.mkdir(parents=True, exist_ok=True)
    destino.parent.mkdir(parents=True, exist_ok=True)

    extension_total = QgsRectangle()
    extension_total.setNull()

    constructores = {
        "vector": lambda ruta, nombre: QgsVectorLayer(
            str(ruta), nombre, PROVEEDOR["vector"]
        ),
        "raster": lambda ruta, nombre: QgsRasterLayer(
            str(ruta), nombre, PROVEEDOR["raster"]
        ),
    }

    def agregar_grupo(nodo_padre, grupo: GrupoDeclarado) -> None:
        nodo = nodo_padre.addGroup(grupo.nombre)
        nodo.setExpanded(grupo.expandido)
        nodo.setItemVisibilityChecked(grupo.visible)

        for capa_declarada in grupo.capas:
            if not capa_declarada.existe:
                resultado.capas_ausentes += 1
                continue

            capa = constructores[capa_declarada.tipo](
                capa_declarada.ruta, capa_declarada.nombre
            )
            if not capa.isValid():
                resultado.hallazgos.append(Hallazgo(
                    BLOQUEANTE, f"capa:{capa_declarada.id}",
                    f"el archivo existe pero QGIS no lo pudo abrir como "
                    f"{capa_declarada.tipo}: {capa_declarada.ruta}",
                ))
                continue

            _verificar_crs(capa, crs, capa_declarada, resultado)
            _aplicar_estilo(
                capa, capa_declarada, directorio_estilos,
                escribir_estilo_inicial, resultado,
            )

            proyecto.addMapLayer(capa, False)
            nodo_capa = nodo.addLayer(capa)
            nodo_capa.setItemVisibilityChecked(capa_declarada.visible)
            resultado.capas_cargadas += 1

            _acumular_extension(
                capa, crs, proyecto, extension_total,
                QgsCoordinateTransform, capa_declarada, resultado,
            )

            if logger is not None:
                logger.debug(
                    "capa cargada: %s (%s) en %s",
                    capa_declarada.id, capa_declarada.tipo,
                    capa_declarada.ubicacion,
                )

        for subgrupo in grupo.subgrupos:
            agregar_grupo(nodo, subgrupo)

    raiz_arbol = proyecto.layerTreeRoot()
    for grupo in grupos:
        agregar_grupo(raiz_arbol, grupo)

    if not extension_total.isNull():
        proyecto.viewSettings().setDefaultViewExtent(
            QgsReferencedRectangle(extension_total, crs)
        )
    elif logger is not None:
        logger.info(
            "Sin capas cargadas: el proyecto se escribe sin extensión inicial."
        )

    if esquema.hay_bloqueantes(resultado.hallazgos):
        return resultado

    proyecto.setFileName(str(destino))
    if not proyecto.write():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "proyecto_qgis.archivo",
            f"QGIS no pudo escribir el proyecto en {destino}.",
        ))
        return resultado

    resultado.archivo = destino
    return resultado


def _verificar_crs(capa, crs_proyecto, declarada: CapaDeclarada,
                   resultado: ResultadoConstruccion) -> None:
    """
    Advierte si la capa no está en el CRS de cálculo.

    Doctrina (CLAUDE.md, sección 5): la reproyección es siempre explícita. Una
    capa en otro CRS dentro del proyecto se dibuja bien, porque QGIS reproyecta
    al vuelo, pero cualquier cálculo de área o de longitud sobre ella daría un
    resultado distinto del esperado.
    """
    if not capa.crs().isValid():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, f"capa:{declarada.id}",
            "la capa no declara CRS. Verificar que el .prj se haya escrito.",
        ))
        return
    if capa.crs().authid() != crs_proyecto.authid():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, f"capa:{declarada.id}",
            f"está en {capa.crs().authid()} y el CRS de cálculo es "
            f"{crs_proyecto.authid()}. QGIS la reproyecta al vuelo para dibujar, "
            "pero no para calcular.",
        ))


def _aplicar_estilo(capa, declarada: CapaDeclarada, directorio_estilos: Path,
                    escribir_inicial: bool,
                    resultado: ResultadoConstruccion) -> None:
    """
    Aplica el .qml de la capa, o lo crea si aún no existe.

    Un estilo existente nunca se sobrescribe: es el mecanismo por el que la
    simbología ajustada por el consultor sobrevive a la regeneración.
    """
    destino_qml = directorio_estilos / declarada.estilo

    if destino_qml.is_file():
        mensaje, aplicado = capa.loadNamedStyle(str(destino_qml))
        if aplicado:
            resultado.estilos_aplicados += 1
        else:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, f"estilo:{declarada.id}",
                f"no se pudo aplicar {declarada.estilo}: {mensaje or 'sin detalle'}",
            ))
        return

    if not escribir_inicial:
        return

    mensaje, guardado = capa.saveNamedStyle(str(destino_qml))
    if guardado:
        resultado.estilos_creados += 1
    else:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, f"estilo:{declarada.id}",
            f"no se pudo crear el estilo inicial {declarada.estilo}: "
            f"{mensaje or 'sin detalle'}",
        ))


def _acumular_extension(capa, crs_proyecto, proyecto, extension_total,
                        clase_transformacion, declarada: CapaDeclarada,
                        resultado: ResultadoConstruccion) -> None:
    """Suma la extensión de la capa a la extensión inicial del proyecto."""
    try:
        extension = capa.extent()
        if extension.isNull() or extension.isEmpty():
            return
        if capa.crs().authid() != crs_proyecto.authid():
            transformacion = clase_transformacion(capa.crs(), crs_proyecto, proyecto)
            extension = transformacion.transformBoundingBox(extension)
        extension_total.combineExtentWith(extension)
    except Exception as exc:  # la extensión es accesoria: no debe detener el módulo
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, f"capa:{declarada.id}",
            f"no se pudo calcular su extensión para el encuadre inicial: {exc}",
        ))


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    solo_validar: bool = False,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """
    Valida la declaración y construye el proyecto. Devuelve (codigo, hallazgos).
    """
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)

    logger = registro.configurar(
        MODULO,
        nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base,
        consola=consola,
    )

    ruta_declaracion = configuracion.ruta_de("proyecto_qgis.declaracion",
                                             debe_existir=True)
    destino = rutas.resolver(configuracion.obtener("proyecto_qgis.archivo"), base)
    directorio_estilos = rutas.resolver(
        configuracion.obtener("proyecto_qgis.estilos"), base
    )

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION,
        config=configuracion,
        insumos={
            "declaración": rutas.relativa(ruta_declaracion, base),
            "estilos": rutas.relativa(directorio_estilos, base),
            "modo": "solo validar" if solo_validar else "construir",
        },
        parametros=configuracion.parametros((
            "crs.calculo",
            "entornos.qgis.version",
            "entornos.qgis.prefix_path",
            "proyecto_qgis.archivo",
            "proyecto_qgis.titulo",
            "proyecto_qgis.escribir_estilo_inicial",
            "proyecto_qgis.detener_si_falta_capa",
        )),
    )

    hallazgos: list[Hallazgo] = []
    resultado = ResultadoConstruccion()

    with registro.bloque(logger, "Lectura y validación de la declaración"):
        declaracion = leer_yaml(ruta_declaracion)
        hallazgos.extend(validar_declaracion(declaracion))

    if esquema.hay_bloqueantes(hallazgos):
        logger.error("La declaración no es utilizable. El proyecto no se construye.")
        return _cerrar(logger, hallazgos, resultado, base, ruta_json,
                       destino, inicio, SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Expansión de grupos, capas y patrones"):
        grupos, hallazgos_expansion = expandir_grupos(declaracion["grupos"], base)
        hallazgos.extend(hallazgos_expansion)
        capas = recorrer_capas(grupos)
        hallazgos.extend(revisar_disponibilidad(
            capas, configuracion.obtener("proyecto_qgis.detener_si_falta_capa")
        ))
        disponibles = sum(1 for capa in capas if capa.existe)
        logger.info(
            "Declaradas %d capa(s) en %d grupo(s) de primer nivel; %d disponible(s) "
            "en disco.", len(capas), len(grupos), disponibles,
        )

    if solo_validar:
        logger.info("Modo solo validar: no se construye el proyecto.")
    elif esquema.hay_bloqueantes(hallazgos):
        logger.error("Hay hallazgos bloqueantes. El proyecto no se construye.")
    else:
        with registro.bloque(logger, "Construcción y escritura del proyecto QGIS"):
            resultado = construir_proyecto(
                grupos=grupos,
                destino=destino,
                directorio_estilos=directorio_estilos,
                crs_calculo=configuracion.obtener("crs.calculo"),
                titulo=configuracion.obtener("proyecto_qgis.titulo"),
                prefix_path=configuracion.obtener("entornos.qgis.prefix_path"),
                escribir_estilo_inicial=configuracion.obtener(
                    "proyecto_qgis.escribir_estilo_inicial"
                ),
                logger=logger,
            )
            hallazgos.extend(resultado.hallazgos)
            logger.info(
                "Capas cargadas: %d | ausentes: %d | estilos aplicados: %d | "
                "estilos creados: %d",
                resultado.capas_cargadas, resultado.capas_ausentes,
                resultado.estilos_aplicados, resultado.estilos_creados,
            )

    codigo = (SALIDA_BLOQUEANTE if esquema.hay_bloqueantes(hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, hallazgos, resultado, base, ruta_json, destino,
                   inicio, codigo)


def _cerrar(logger, hallazgos, resultado, base, ruta_json, destino, inicio,
            codigo) -> tuple[int, list[Hallazgo]]:
    """Emite el reporte, escribe el JSON opcional y cierra el log."""
    hallazgos = sorted(hallazgos, key=_orden_hallazgo)

    logger.info(registro.SEPARADOR)
    for severidad, emitir in ((BLOQUEANTE, logger.error),
                              (ADVERTENCIA, logger.warning),
                              (INFORMATIVO, logger.info)):
        grupo = [h for h in hallazgos if h.severidad == severidad]
        if not grupo:
            continue
        emitir("%s (%d)", severidad, len(grupo))
        for hallazgo in grupo:
            emitir("  %-46s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info(
        "RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
        conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO],
    )

    productos: dict[str, Any] = {}
    if resultado.archivo is not None:
        productos["proyecto QGIS"] = rutas.relativa(resultado.archivo, base)
        productos["capas cargadas"] = resultado.capas_cargadas
        productos["estilos creados"] = resultado.estilos_creados

    if ruta_json is not None:
        reporte = {
            "modulo": MODULO,
            "archivo": (rutas.relativa(resultado.archivo, base)
                        if resultado.archivo else None),
            "capas_cargadas": resultado.capas_cargadas,
            "capas_ausentes": resultado.capas_ausentes,
            "estilos_aplicados": resultado.estilos_aplicados,
            "estilos_creados": resultado.estilos_creados,
            "resumen": conteo,
            "codigo_salida": codigo,
            "hallazgos": [h.como_dict() for h in hallazgos],
        }
        ruta_json.parent.mkdir(parents=True, exist_ok=True)
        ruta_json.write_text(
            json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        productos["reporte JSON"] = rutas.relativa(ruta_json, base)

    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecución"] = rutas.relativa(archivo_log, base)

    estado = "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO"
    registro.registrar_cierre(
        logger, MODULO, estado,
        segundos=time.perf_counter() - inicio, productos=productos,
    )
    return codigo, hallazgos


# =============================================================================
# Interfaz de línea de comandos
# =============================================================================
def _analizar_argumentos(argv: Sequence[str] | None = None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        prog="M00b_proyecto_qgis.py",
        description="Construye el proyecto QGIS del estudio hidrológico.",
    )
    analizador.add_argument("--raiz", type=Path, default=None,
                            help="Raíz del repositorio.")
    analizador.add_argument("--config", type=Path, default=None,
                            help="Archivo de configuración a usar.")
    analizador.add_argument("--solo-validar", action="store_true",
                            dest="solo_validar",
                            help="Valida la declaración sin construir el .qgz.")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida",
                            help="Escribe el reporte en el archivo JSON indicado.")
    analizador.add_argument("--silencioso", action="store_true",
                            help="No escribe en consola.")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        entorno.exigir_entorno(entorno.ENTORNO_QGIS, MODULO)
        codigo, _ = ejecutar(
            raiz=argumentos.raiz,
            ruta_config=argumentos.config,
            solo_validar=argumentos.solo_validar,
            ruta_json=argumentos.json_salida,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorEntorno, ErrorRutas, ErrorConfiguracion) as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorHidrologia as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    finally:
        # La aplicación QGIS se cierra una sola vez, al terminar el proceso.
        finalizar_qgis()


if __name__ == "__main__":
    sys.exit(main())
