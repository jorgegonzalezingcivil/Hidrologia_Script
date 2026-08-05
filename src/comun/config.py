# -*- coding: utf-8 -*-
"""
comun.config
============
Lectura, validación y acceso de solo lectura a config/config.yaml.

Doctrina (CLAUDE.md, sección 2): sin rutas absolutas ni parámetros embebidos, y
sin estado compartido en memoria entre módulos. La configuración se carga una
vez por proceso, se valida contra el esquema y se entrega congelada, de modo que
ningún módulo pueda alterar el valor que leerá el siguiente.

Dos decisiones deliberadas:

1. Las claves duplicadas en el YAML detienen la carga. PyYAML conserva en
   silencio la última aparición, lo que produce un estudio ejecutado con un
   parámetro distinto del que el consultor cree haber fijado.
2. El objeto Config se congela: los diccionarios se exponen como mapas de solo
   lectura y las listas como tuplas. Un módulo que necesite modificar un valor
   debe copiarlo de forma explícita.

Depende únicamente de la librería estándar y de PyYAML, que el Python de QGIS ya
incluye: es importable desde ambos entornos.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
from pathlib import Path
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Iterator

try:
    import yaml
except ImportError as exc:  # pragma: no cover - depende del entorno
    raise ImportError(
        "PyYAML no está instalado. Es la única dependencia externa de "
        "src/comun. Instalar con: pip install pyyaml"
    ) from exc

from . import esquema as _esquema
from . import rutas as _rutas
from .errores import ErrorClaveInexistente, ErrorConfiguracion, ErrorValidacion

__all__ = ["Config", "cargar", "leer_yaml", "huella_sha256"]

_AUSENTE = object()


# =============================================================================
# Carga del YAML
# =============================================================================
class _CargadorEstricto(yaml.SafeLoader):
    """SafeLoader que rechaza claves duplicadas en un mismo bloque."""


def _construir_mapa(loader: yaml.SafeLoader, nodo: yaml.MappingNode, deep: bool = False):
    mapa: dict = {}
    for nodo_clave, nodo_valor in nodo.value:
        clave = loader.construct_object(nodo_clave, deep=deep)
        if clave in mapa:
            linea = nodo_clave.start_mark.line + 1
            raise ErrorConfiguracion(
                f"Clave duplicada '{clave}' en la línea {linea}. PyYAML "
                f"conservaría solo la última aparición, de modo que el estudio "
                f"se ejecutaría con un valor distinto del previsto."
            )
        mapa[clave] = loader.construct_object(nodo_valor, deep=deep)
    return mapa


_CargadorEstricto.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construir_mapa
)


def leer_yaml(ruta: str | os.PathLike) -> dict:
    """
    Lee un archivo YAML y devuelve su contenido como diccionario.

    No valida nada más allá de la sintaxis y la ausencia de claves duplicadas.
    Se expone porque el MANIFIESTO.yaml y los perfiles de ingesta se leen con el
    mismo criterio.

    Excepciones
    -----------
    ErrorConfiguracion
        Si el archivo no existe, no es YAML válido, tiene claves duplicadas o su
        contenido no es un bloque de claves.
    """
    destino = Path(ruta)
    if not destino.is_file():
        raise ErrorConfiguracion(f"No existe el archivo: {destino}")

    try:
        texto = destino.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ErrorConfiguracion(
            f"{destino} no está codificado en UTF-8: {exc}"
        ) from exc

    try:
        datos = yaml.load(texto, Loader=_CargadorEstricto)
    except ErrorConfiguracion as exc:
        raise ErrorConfiguracion(f"{destino}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ErrorConfiguracion(f"YAML inválido en {destino}: {exc}") from exc

    if datos is None:
        raise ErrorConfiguracion(f"{destino} está vacío.")
    if not isinstance(datos, dict):
        raise ErrorConfiguracion(
            f"{destino} debe contener un bloque de claves y contiene "
            f"{type(datos).__name__}."
        )
    return datos


def huella_sha256(ruta: str | os.PathLike) -> str:
    """
    Huella sha256 de un archivo, para trazabilidad en el log y en los anexos.

    Permite demostrar que dos ejecuciones partieron de la misma configuración o
    del mismo insumo, sin transcribir su contenido al informe.
    """
    digestor = hashlib.sha256()
    with Path(ruta).open("rb") as manejador:
        for bloque in iter(lambda: manejador.read(65536), b""):
            digestor.update(bloque)
    return digestor.hexdigest()


def _congelar(valor: Any) -> Any:
    """Convierte diccionarios en mapas de solo lectura y listas en tuplas."""
    if isinstance(valor, dict):
        return MappingProxyType({c: _congelar(v) for c, v in valor.items()})
    if isinstance(valor, list):
        return tuple(_congelar(v) for v in valor)
    return valor


def _descongelar(valor: Any) -> Any:
    """Operación inversa de _congelar, para exportar la configuración."""
    if isinstance(valor, (dict, MappingProxyType)):
        return {c: _descongelar(v) for c, v in valor.items()}
    if isinstance(valor, tuple):
        return [_descongelar(v) for v in valor]
    return valor


# =============================================================================
# Objeto de configuración
# =============================================================================
class Config(Mapping):
    """
    Vista de solo lectura de config/config.yaml.

    Se comporta como un mapeo sobre las claves de primer nivel y ofrece acceso
    por ruta con puntos. Las listas se entregan como tuplas: un módulo que
    necesite mutarlas debe construir su propia copia.

    Atributos
    ---------
    ruta:         ruta absoluta del archivo leído.
    raiz:         raíz del repositorio contra la que se resuelven las rutas.
    sha256:       huella del archivo, para trazabilidad.
    fecha_carga:  instante de la lectura.
    hallazgos:    hallazgos de la validación, incluidas advertencias.
    """

    __slots__ = ("_datos", "ruta", "raiz", "sha256", "fecha_carga", "hallazgos")

    def __init__(
        self,
        datos: dict,
        ruta: Path,
        raiz: Path,
        sha256: str,
        hallazgos: tuple = (),
    ) -> None:
        object.__setattr__(self, "_datos", _congelar(datos))
        object.__setattr__(self, "ruta", ruta)
        object.__setattr__(self, "raiz", raiz)
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(self, "fecha_carga", _dt.datetime.now())
        object.__setattr__(self, "hallazgos", tuple(hallazgos))

    # --- protocolo Mapping ---------------------------------------------------
    def __getitem__(self, clave: str) -> Any:
        return self._datos[clave]

    def __iter__(self) -> Iterator[str]:
        return iter(self._datos)

    def __len__(self) -> int:
        return len(self._datos)

    def __repr__(self) -> str:
        return (
            f"Config(ruta={self.ruta.name!r}, sha256={self.sha256[:12]}..., "
            f"claves={len(self._datos)})"
        )

    # --- acceso por ruta con puntos -----------------------------------------
    def obtener(self, clave: str, defecto: Any = _AUSENTE) -> Any:
        """
        Devuelve el valor de una clave con puntos.

        Sin `defecto`, la ausencia de la clave es un error: un módulo que pide
        un parámetro inexistente está mal escrito o el esquema quedó desfasado,
        y en ninguno de los dos casos debe continuar con un valor implícito.

        Excepciones
        -----------
        ErrorClaveInexistente
            Si la clave no existe y no se suministró un valor por defecto.
        """
        nodo: Any = self._datos
        for parte in clave.split("."):
            if not isinstance(nodo, (dict, MappingProxyType)) or parte not in nodo:
                if defecto is _AUSENTE:
                    raise ErrorClaveInexistente(clave, str(self.ruta))
                return defecto
            nodo = nodo[parte]
        return nodo

    def tiene(self, clave: str) -> bool:
        """
        Indica si la clave con puntos existe, con independencia de su valor.

        Una clave presente con valor nulo devuelve True: existe, pero está sin
        definir. Para exigir un valor utilizable se usa requerir().
        """
        nodo: Any = self._datos
        for parte in clave.split("."):
            if not isinstance(nodo, (dict, MappingProxyType)) or parte not in nodo:
                return False
            nodo = nodo[parte]
        return True

    def requerir(self, clave: str) -> Any:
        """
        Devuelve el valor de una clave que además no puede ser nulo.

        Se usa para las decisiones que el consultor debe fijar antes de ejecutar
        un módulo, como `tormenta.hipotesis_adoptada`.

        Excepciones
        -----------
        ErrorClaveInexistente
            Si la clave no existe.
        ErrorConfiguracion
            Si la clave existe pero su valor es nulo o una cadena vacía.
        """
        valor = self.obtener(clave)
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            raise ErrorConfiguracion(
                f"La clave '{clave}' está sin definir en {self.ruta}. El módulo "
                f"no puede continuar sin esa decisión del consultor."
            )
        return valor

    def ruta_de(self, clave: str, debe_existir: bool = False) -> Path:
        """
        Resuelve contra la raíz del repositorio una ruta declarada en el YAML.

        Excepciones
        -----------
        ErrorConfiguracion
            Si el valor no es texto, o si `debe_existir` es True y no existe.
        """
        valor = self.obtener(clave)
        if not isinstance(valor, str) or not valor.strip():
            raise ErrorConfiguracion(
                f"La clave '{clave}' no contiene una ruta utilizable: {valor!r}"
            )
        destino = _rutas.resolver(valor, self.raiz)
        if debe_existir and not destino.exists():
            raise ErrorConfiguracion(
                f"La ruta declarada en '{clave}' no existe: {destino}"
            )
        return destino

    def seccion(self, prefijo: str) -> dict:
        """Devuelve una copia mutable e independiente de un bloque."""
        return _descongelar(self.obtener(prefijo))

    def como_dict(self) -> dict:
        """Devuelve una copia mutable e independiente de toda la configuración."""
        return _descongelar(self._datos)

    def parametros(self, claves: tuple[str, ...]) -> dict[str, Any]:
        """
        Extrae un subconjunto de claves para dejarlo registrado en el log.

        Doctrina (CLAUDE.md, sección 2): el log de cada módulo declara los
        parámetros que efectivamente usó.
        """
        return {clave: _descongelar(self.obtener(clave, None)) for clave in claves}


# =============================================================================
# Punto de entrada
# =============================================================================
def cargar(
    ruta: str | os.PathLike | None = None,
    raiz: str | os.PathLike | None = None,
    validar: bool = True,
    estricto: bool = False,
) -> Config:
    """
    Lee y valida config/config.yaml y devuelve un objeto Config congelado.

    Parámetros
    ----------
    ruta:
        Archivo a leer. Si es None se usa config/config.yaml de la raíz.
    raiz:
        Raíz del repositorio. Si es None se detecta a partir de los marcadores.
    validar:
        Si es False se omite la validación contra el esquema. Reservado para
        pruebas y para el propio M00, que necesita reportar antes de fallar.
    estricto:
        Si es True, las advertencias también detienen la carga. Corresponde a
        `ejecucion.detener_en_advertencia` cuando el módulo así lo decida.

    Excepciones
    -----------
    ErrorConfiguracion
        Si el archivo no se puede leer o interpretar.
    ErrorValidacion
        Si la configuración incumple el esquema o una invariante bloqueante.
    """
    base = Path(raiz).resolve() if raiz is not None else _rutas.raiz_proyecto()
    destino = Path(ruta).resolve() if ruta is not None else _rutas.ruta_config(base)

    datos = leer_yaml(destino)
    huella = huella_sha256(destino)

    hallazgos: tuple = ()
    if validar:
        hallazgos = tuple(_esquema.validar(datos, raiz=base))
        detener = _esquema.hay_bloqueantes(hallazgos) or (
            estricto and any(h.severidad == _esquema.ADVERTENCIA for h in hallazgos)
        )
        if detener:
            raise ErrorValidacion(hallazgos, str(destino))

    return Config(datos, destino, base, huella, hallazgos)
