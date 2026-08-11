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
from typing import Any, Iterable, Iterator

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

__all__ = [
    "CLAVES_LOCALES",
    "Config",
    "NOMBRE_LOCAL",
    "cargar",
    "huella_sha256",
    "leer_yaml",
    "superponer",
]

_AUSENTE = object()

# =============================================================================
# Superposición local
# =============================================================================
# Nombre del archivo que cada equipo mantiene sin versionar, junto al
# config.yaml compartido.
NOMBRE_LOCAL = "config.local.yaml"

# ÚNICAS claves que la superposición local puede sobrescribir.
#
# La lista es cerrada a propósito. Si cualquier clave pudiera sobrescribirse en
# local, un miembro del equipo podría cambiar un periodo de retorno, un umbral
# de completitud o el método de interpolación sin que quedara rastro en el
# repositorio, y dos ejecuciones del mismo estudio darían resultados distintos
# sin explicación. Eso es exactamente lo que la sección 7 de CLAUDE.md prohíbe:
# un estudio que no puede explicar sus decisiones no es defendible.
#
# Lo que sí varía de una máquina a otra es dónde está instalado el software,
# dónde viven las capas nacionales y qué credenciales usa cada quien. Nada de
# eso es doctrina del estudio.
CLAVES_LOCALES: tuple[str, ...] = (
    # Dónde está instalado QGIS y qué versión es
    "entornos.qgis.version",
    "entornos.qgis.es_ltr",
    "entornos.qgis.python",
    "entornos.qgis.prefix_path",
    # Intérprete del venv, por si el equipo lo crea en otra ruta
    "entornos.venv.python",
    # Dónde está instalado HEC-HMS y qué versión es
    "software.hec_hms.ruta",
    "software.hec_hms.version",
    # Dónde viven las capas nacionales, que no caben en el repositorio
    "referencia_nacional.directorio",
    # Credenciales propias de cada quien, que nunca se versionan
    "ideam.socrata.token",
    "dem.earthdata.ruta_netrc",
    # Preferencia personal, sin efecto sobre el resultado
    "ejecucion.nivel_log",
)


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


def _hojas(datos: Mapping, prefijo: str = "") -> Iterator[tuple[str, Any]]:
    """Recorre un YAML anidado y entrega (clave con puntos, valor) de cada hoja."""
    for clave, valor in datos.items():
        ruta = f"{prefijo}.{clave}" if prefijo else str(clave)
        if isinstance(valor, dict) and valor:
            yield from _hojas(valor, ruta)
        else:
            yield ruta, valor


def _leer_anidado(datos: Mapping, clave: str) -> Any:
    """Lee una clave con puntos. Devuelve _AUSENTE si no existe."""
    actual: Any = datos
    for parte in clave.split("."):
        if not isinstance(actual, Mapping) or parte not in actual:
            return _AUSENTE
        actual = actual[parte]
    return actual


def _fijar_anidado(datos: dict, clave: str, valor: Any) -> None:
    """Escribe una clave con puntos sobre un diccionario mutable."""
    partes = clave.split(".")
    actual = datos
    for parte in partes[:-1]:
        actual = actual[parte]
    actual[partes[-1]] = valor


def superponer(
    datos: dict, local: Mapping, permitidas: Iterable[str] = CLAVES_LOCALES
) -> tuple[tuple[str, Any, Any], ...]:
    """
    Aplica sobre `datos` las claves de `local`, en el sitio.

    Devuelve lo sustituido como (clave, valor compartido, valor local), que es
    lo que el log registra: sin ese rastro, dos ejecuciones de máquinas
    distintas serían indistinguibles en los anexos del estudio.

    Se rechaza toda clave fuera de `permitidas` y toda clave que no exista ya
    en la configuración compartida. Lo primero impide que la doctrina del
    estudio se altere por máquina; lo segundo atrapa el error de escritura, que
    de otro modo crearía en silencio un parámetro que ningún módulo lee.

    Excepciones
    -----------
    ErrorConfiguracion
        Si el archivo local declara una clave no permitida o inexistente.
    """
    admitidas = set(permitidas)
    sustituidas: list[tuple[str, Any, Any]] = []
    rechazadas: list[str] = []
    desconocidas: list[str] = []

    for clave, valor in _hojas(local):
        if clave not in admitidas:
            rechazadas.append(clave)
            continue
        previo = _leer_anidado(datos, clave)
        if previo is _AUSENTE:
            desconocidas.append(clave)
            continue
        if previo != valor:
            sustituidas.append((clave, previo, valor))
        _fijar_anidado(datos, clave, valor)

    if rechazadas:
        raise ErrorConfiguracion(
            f"{NOMBRE_LOCAL} intenta sobrescribir claves que no son de "
            f"máquina: {', '.join(sorted(rechazadas))}. La superposición local "
            "existe para declarar dónde está instalado el software y dónde "
            "viven las capas nacionales, no para cambiar la doctrina del "
            "estudio. Un parámetro técnico distinto en cada equipo produciría "
            "resultados distintos sin dejar rastro en el repositorio. Si el "
            "cambio es del estudio, va en config/config.yaml y se versiona. "
            f"Claves admitidas: {', '.join(CLAVES_LOCALES)}."
        )
    if desconocidas:
        raise ErrorConfiguracion(
            f"{NOMBRE_LOCAL} declara claves que no existen en config.yaml: "
            f"{', '.join(sorted(desconocidas))}. Suele ser un error de "
            "escritura; sin este control quedaría un valor que ningún módulo "
            "lee y la máquina seguiría usando el de la configuración "
            "compartida."
        )
    return tuple(sustituidas)


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
    ruta_local:   archivo de superposición aplicado, o None.
    sha256_local: huella de ese archivo, o None.
    superpuestas: (clave, valor compartido, valor local) de lo sustituido.
    """

    __slots__ = ("_datos", "ruta", "raiz", "sha256", "fecha_carga", "hallazgos",
                 "ruta_local", "sha256_local", "superpuestas")

    def __init__(
        self,
        datos: dict,
        ruta: Path,
        raiz: Path,
        sha256: str,
        hallazgos: tuple = (),
        ruta_local: Path | None = None,
        sha256_local: str | None = None,
        superpuestas: tuple = (),
    ) -> None:
        object.__setattr__(self, "_datos", _congelar(datos))
        object.__setattr__(self, "ruta", ruta)
        object.__setattr__(self, "raiz", raiz)
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(self, "fecha_carga", _dt.datetime.now())
        object.__setattr__(self, "hallazgos", tuple(hallazgos))
        object.__setattr__(self, "ruta_local", ruta_local)
        object.__setattr__(self, "sha256_local", sha256_local)
        object.__setattr__(self, "superpuestas", tuple(superpuestas))

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
    usar_local: bool = True,
    ruta_local: str | os.PathLike | None = None,
) -> Config:
    """
    Lee y valida config/config.yaml y devuelve un objeto Config congelado.

    Si junto al archivo compartido existe 'config.local.yaml', sus claves se
    superponen ANTES de validar, de modo que lo que se valida y lo que se
    ejecuta son lo mismo. Ese archivo no se versiona: declara dónde está
    instalado el software de cada equipo, dónde viven las capas nacionales y
    qué credenciales usa cada quien. Solo puede sobrescribir las claves de
    CLAVES_LOCALES; cualquier otra detiene la carga.

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
    usar_local:
        Si es False se ignora la superposición. Sirve para comprobar qué haría
        el estudio con la configuración compartida sola.
    ruta_local:
        Archivo de superposición. Si es None se busca 'config.local.yaml'
        junto al archivo compartido.

    Excepciones
    -----------
    ErrorConfiguracion
        Si el archivo no se puede leer o interpretar, o si la superposición
        local declara claves que no son de máquina.
    ErrorValidacion
        Si la configuración incumple el esquema o una invariante bloqueante.
    """
    base = Path(raiz).resolve() if raiz is not None else _rutas.raiz_proyecto()
    destino = Path(ruta).resolve() if ruta is not None else _rutas.ruta_config(base)

    datos = leer_yaml(destino)
    huella = huella_sha256(destino)

    local = (Path(ruta_local).resolve() if ruta_local is not None
             else destino.with_name(NOMBRE_LOCAL))
    aplicado: Path | None = None
    huella_local: str | None = None
    superpuestas: tuple = ()
    if usar_local and local.is_file():
        superpuestas = superponer(datos, leer_yaml(local))
        aplicado = local
        huella_local = huella_sha256(local)

    hallazgos: tuple = ()
    if validar:
        hallazgos = tuple(_esquema.validar(datos, raiz=base))
        detener = _esquema.hay_bloqueantes(hallazgos) or (
            estricto and any(h.severidad == _esquema.ADVERTENCIA for h in hallazgos)
        )
        if detener:
            raise ErrorValidacion(hallazgos, str(destino))

    return Config(datos, destino, base, huella, hallazgos,
                  aplicado, huella_local, superpuestas)
