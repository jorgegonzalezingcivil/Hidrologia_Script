# -*- coding: utf-8 -*-
"""
comun.asf
=========
Adaptador del servicio de búsqueda y descarga de ASF (Alaska Satellite
Facility), del que se obtienen las escenas ALOS PALSAR RTC.

Doctrina (CLAUDE.md, sección 2): los puntos frágiles ante actualizaciones
externas se aíslan en adaptadores. Este es el único archivo que conoce la forma
de la API de ASF y el mecanismo de autenticación de Earthdata.

CREDENCIALES. Nunca se escriben aquí, ni en la configuración, ni en el log. Se
leen de ~/.netrc (o ~/_netrc en Windows), que el consultor crea por su cuenta
con el formato:

    machine urs.earthdata.nasa.gov login USUARIO password CLAVE

El adaptador comprueba que exista una entrada para ese servidor, pero no expone
su contenido en ningún reporte.

DEDUPLICACIÓN. ALOS PALSAR acumula muchas adquisiciones sobre la misma huella
espacial. Para un DEM solo interesa la cobertura, no la fecha: el modelo de
elevación que acompaña a cada producto RTC no cambia entre pasadas. Descargar
todas las adquisiciones multiplica el volumen por diez o por veinte sin aportar
nada. deduplicar_por_huella() deja una escena por combinación de órbita
relativa y marco.

Solo usa la librería estándar: es importable desde el entorno de QGIS y desde el
venv del proyecto.
"""

from __future__ import annotations

import hashlib
import json
import netrc
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .errores import ErrorFormato, ErrorHidrologia

__all__ = [
    "URL_BUSQUEDA",
    "SERVIDOR_EARTHDATA",
    "NIVELES_PROCESO",
    "EscenaASF",
    "ErrorASF",
    "buscar",
    "deduplicar_por_huella",
    "credenciales_disponibles",
    "descargar",
    "verificar_md5",
]

URL_BUSQUEDA = "https://api.daac.asf.alaska.edu/services/search/param"
SERVIDOR_EARTHDATA = "urs.earthdata.nasa.gov"

# Niveles de producto de ALOS PALSAR con corrección radiométrica y de terreno.
# El de alta resolución trae el modelo de elevación a 12,5 m.
NIVELES_PROCESO = ("RTC_HI_RES", "RTC_LOW_RES")

_TIEMPO_ESPERA = 180
_BLOQUE = 1024 * 512


class ErrorASF(ErrorHidrologia):
    """La consulta o la descarga desde ASF no se pudo completar."""


@dataclass(frozen=True)
class EscenaASF:
    """Una escena tal como la describe el catálogo de ASF."""

    identificador: str
    nombre_archivo: str
    url: str
    md5: str
    tamano_mb: float
    huella_wkt: str
    orbita_relativa: int | None
    marco: int | None
    modo_haz: str
    nivel: str
    fecha_escena: str
    fecha_proceso: str

    @property
    def huella(self) -> tuple[int | None, int | None]:
        """Identifica la posición espacial, con independencia de la fecha."""
        return (self.orbita_relativa, self.marco)

    @property
    def tamano_bytes(self) -> int:
        return int(self.tamano_mb * 1024 * 1024)


def _entero(valor) -> int | None:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _a_escena(registro: dict) -> EscenaASF:
    return EscenaASF(
        identificador=str(registro.get("sceneId") or registro.get("granuleName") or ""),
        nombre_archivo=str(registro.get("fileName") or ""),
        url=str(registro.get("downloadUrl") or ""),
        md5=str(registro.get("md5sum") or "").strip().lower(),
        tamano_mb=float(registro.get("sizeMB") or 0.0),
        huella_wkt=str(registro.get("stringFootprint") or ""),
        orbita_relativa=_entero(registro.get("relativeOrbit")),
        marco=_entero(registro.get("frameNumber")),
        modo_haz=str(registro.get("beamModeType") or ""),
        nivel=str(registro.get("processingLevel") or ""),
        fecha_escena=str(registro.get("sceneDate") or ""),
        fecha_proceso=str(registro.get("processingDate") or ""),
    )


# =============================================================================
# Búsqueda
# =============================================================================
def buscar(
    poligono_wkt: str,
    nivel: str = "RTC_HI_RES",
    plataforma: str = "ALOS",
    maximo: int = 5000,
    tiempo_espera: int = _TIEMPO_ESPERA,
    reintentos: int = 3,
    espera_entre_intentos: float = 5.0,
) -> list[EscenaASF]:
    """
    Consulta el catálogo de ASF por las escenas que intersecan un polígono.

    El polígono va en WKT y en coordenadas geográficas EPSG:4326, que es lo
    único que acepta el servicio. La búsqueda no requiere autenticación.

    Excepciones
    -----------
    ErrorASF
        Si el servicio no responde o devuelve algo que no es JSON.
    """
    if nivel not in NIVELES_PROCESO:
        raise ValueError(
            f"Nivel {nivel!r} no admitido. Opciones: {', '.join(NIVELES_PROCESO)}"
        )

    consulta = {
        "platform": plataforma,
        "processingLevel": nivel,
        "intersectsWith": poligono_wkt,
        "maxResults": maximo,
        "output": "json",
    }
    url = f"{URL_BUSQUEDA}?{urllib.parse.urlencode(consulta)}"

    # El servicio responde 504 con cierta frecuencia cuando el área es amplia.
    # Un reintento con espera creciente resuelve la mayoría de esos casos; sin
    # él, un módulo de arranque falla por una incidencia pasajera del servidor.
    crudo = ""
    ultimo: Exception | None = None
    for intento in range(1, reintentos + 1):
        try:
            with urllib.request.urlopen(url, timeout=tiempo_espera) as respuesta:
                crudo = respuesta.read().decode("utf-8")
            break
        except (urllib.error.URLError, OSError) as exc:
            ultimo = exc
            if intento < reintentos:
                time.sleep(espera_entre_intentos * intento)

    if not crudo:
        motivo = getattr(ultimo, "reason", ultimo)
        raise ErrorASF(
            f"No se pudo consultar el catálogo de ASF tras {reintentos} "
            f"intento(s): {motivo}"
        ) from ultimo

    try:
        datos = json.loads(crudo)
    except json.JSONDecodeError as exc:
        raise ErrorASF(
            f"La respuesta de ASF no es JSON válido: {exc}"
        ) from exc

    # El servicio devuelve una lista de listas cuando hay resultados.
    plano = datos[0] if datos and isinstance(datos[0], list) else datos
    if not isinstance(plano, list):
        raise ErrorASF(
            f"Se esperaba una lista de escenas y se recibió {type(plano).__name__}."
        )

    return [_a_escena(registro) for registro in plano if isinstance(registro, dict)]


def deduplicar_por_huella(escenas: Sequence[EscenaASF]) -> list[EscenaASF]:
    """
    Deja una escena por huella espacial, la de procesamiento más reciente.

    Es la reducción que evita descargar decenas de adquisiciones de la misma
    posición. El resultado se ordena por huella para que dos ejecuciones
    produzcan la misma selección.
    """
    mejores: dict[tuple, EscenaASF] = {}
    for escena in escenas:
        actual = mejores.get(escena.huella)
        if actual is None or escena.fecha_proceso > actual.fecha_proceso:
            mejores[escena.huella] = escena
    return sorted(
        mejores.values(),
        key=lambda e: (
            e.orbita_relativa if e.orbita_relativa is not None else -1,
            e.marco if e.marco is not None else -1,
        ),
    )


# =============================================================================
# Credenciales
# =============================================================================
def _ruta_netrc() -> Path | None:
    """Devuelve la ruta del archivo netrc si existe, sin leer su contenido."""
    inicio = Path(os.path.expanduser("~"))
    for nombre in (".netrc", "_netrc"):
        candidato = inicio / nombre
        if candidato.is_file():
            return candidato
    return None


def credenciales_disponibles(servidor: str = SERVIDOR_EARTHDATA) -> tuple[bool, str]:
    """
    Indica si hay credenciales de Earthdata configuradas.

    Devuelve (disponibles, motivo). El motivo nunca incluye el usuario ni la
    clave: solo describe qué falta.
    """
    ruta = _ruta_netrc()
    if ruta is None:
        return False, (
            "no existe ~/.netrc ni ~/_netrc. Crearlo con una línea "
            f"'machine {servidor} login USUARIO password CLAVE' y permisos de "
            "solo lectura para el usuario."
        )
    try:
        autenticacion = netrc.netrc(str(ruta)).authenticators(servidor)
    except (netrc.NetrcParseError, OSError) as exc:
        return False, f"{ruta.name} no se pudo interpretar: {exc}"

    if autenticacion is None:
        return False, f"{ruta.name} no tiene una entrada para {servidor}."
    usuario, _, clave = autenticacion
    if not usuario or not clave:
        return False, f"la entrada de {servidor} en {ruta.name} está incompleta."
    return True, f"credenciales de {servidor} encontradas en {ruta.name}."


def _abridor_autenticado(servidor: str = SERVIDOR_EARTHDATA):
    """
    Construye el abridor de URL que sobrevive a la redirección de Earthdata.

    La descarga redirige del repositorio de datos al servidor de autenticación y
    vuelve. Sin gestor de cookies la sesión se pierde en el regreso y el
    servidor responde con una página de inicio de sesión en lugar del archivo.
    """
    ruta = _ruta_netrc()
    if ruta is None:
        raise ErrorASF(
            "No hay archivo netrc con las credenciales de Earthdata. "
            "Ver credenciales_disponibles()."
        )
    autenticacion = netrc.netrc(str(ruta)).authenticators(servidor)
    if autenticacion is None:
        raise ErrorASF(f"El archivo netrc no tiene entrada para {servidor}.")
    usuario, _, clave = autenticacion

    gestor = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    gestor.add_password(None, f"https://{servidor}", usuario, clave)
    gestor.add_password(None, "https://datapool.asf.alaska.edu", usuario, clave)

    return urllib.request.build_opener(
        urllib.request.HTTPBasicAuthHandler(gestor),
        urllib.request.HTTPCookieProcessor(CookieJar()),
    )


# =============================================================================
# Descarga
# =============================================================================
def verificar_md5(destino: Path, esperado: str) -> bool:
    """Comprueba la huella md5 del archivo descargado."""
    if not esperado:
        return True
    digestor = hashlib.md5()
    with destino.open("rb") as manejador:
        for bloque in iter(lambda: manejador.read(_BLOQUE), b""):
            digestor.update(bloque)
    return digestor.hexdigest() == esperado


def descargar(
    escena: EscenaASF,
    directorio: Path,
    verificar: bool = True,
    reintentos: int = 3,
    progreso: Callable[[str, int, int], None] | None = None,
) -> Path:
    """
    Descarga una escena, omitiendo la transferencia si ya está completa.

    Un archivo presente con la huella md5 correcta no se vuelve a descargar: las
    escenas pesan cientos de megabytes y el módulo debe poder reanudarse.

    Excepciones
    -----------
    ErrorASF
        Si la descarga falla tras agotar los reintentos.
    ErrorFormato
        Si el archivo descargado no coincide con la huella declarada.
    """
    directorio.mkdir(parents=True, exist_ok=True)
    destino = directorio / (escena.nombre_archivo or f"{escena.identificador}.zip")

    if destino.is_file() and (not verificar or verificar_md5(destino, escena.md5)):
        return destino

    abridor = _abridor_autenticado()
    parcial = destino.with_suffix(destino.suffix + ".parcial")
    ultimo_error: Exception | None = None

    for intento in range(1, reintentos + 1):
        try:
            with abridor.open(escena.url, timeout=_TIEMPO_ESPERA) as respuesta:
                total = int(respuesta.headers.get("Content-Length") or 0)
                tipo = (respuesta.headers.get("Content-Type") or "").lower()
                if "text/html" in tipo:
                    raise ErrorASF(
                        "el servidor devolvió una página HTML en lugar del "
                        "archivo. Suele significar credenciales inválidas o que "
                        "falta aceptar el acuerdo de uso de ASF en Earthdata."
                    )
                descargado = 0
                with parcial.open("wb") as manejador:
                    while True:
                        bloque = respuesta.read(_BLOQUE)
                        if not bloque:
                            break
                        manejador.write(bloque)
                        descargado += len(bloque)
                        if progreso is not None:
                            progreso(escena.nombre_archivo, descargado, total)

            parcial.replace(destino)
            break

        except (urllib.error.URLError, OSError, ErrorASF) as exc:
            ultimo_error = exc
            parcial.unlink(missing_ok=True)
            if intento == reintentos:
                raise ErrorASF(
                    f"No se pudo descargar {escena.nombre_archivo} tras "
                    f"{reintentos} intento(s): {exc}"
                ) from exc

    if verificar and not verificar_md5(destino, escena.md5):
        destino.unlink(missing_ok=True)
        raise ErrorFormato(
            f"{escena.nombre_archivo} se descargó con una huella md5 distinta de "
            "la declarada por el catálogo. El archivo se eliminó."
        )

    if ultimo_error is not None:
        pass  # el reintento tuvo éxito; el error previo queda solo como historia

    return destino


def resumen_descarga(escenas: Iterable[EscenaASF]) -> dict[str, float]:
    """Resume el volumen de una selección, para reportarlo antes de bajar nada."""
    lista = list(escenas)
    total_mb = sum(escena.tamano_mb for escena in lista)
    return {
        "escenas": float(len(lista)),
        "volumen_mb": total_mb,
        "volumen_gb": total_mb / 1024.0,
    }
