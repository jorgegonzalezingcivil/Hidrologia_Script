# -*- coding: utf-8 -*-
"""
comun.dhime
===========
Adaptador del servicio de descarga del IDEAM (DHIME).

Doctrina (CLAUDE.md, sección 2): los puntos frágiles ante actualizaciones
externas se aíslan en adaptadores. Este es el único archivo que conoce el
contrato del servicio, que se determinó por inspección del código de la
aplicación web y no está documentado públicamente.

El servicio es una tarea de geoprocesamiento asíncrona de ArcGIS Server,
accesible sin autenticación:

    .../AtencionCiudadano/DescargarArchivo/GPServer/DescargarArchivo

Recibe dos cadenas y devuelve la ruta de un archivo:

    Filtro   cadena application/x-www-form-urlencoded con el filtro de series,
             el rango de fechas y el tipo de reporte.
    Items    JSON con la lista de series solicitadas.
    Archivo  parámetro de salida con la ruta del resultado.

ADVERTENCIA SOBRE ESTABILIDAD. El contrato se dedujo de código minificado que
muestra varias generaciones de implementación y un ajuste fechado en agosto de
2025. Puede cambiar sin aviso. Por eso toda la construcción de las dos cadenas
vive aquí, y el módulo verifica la respuesta en lugar de darla por buena.

ADVERTENCIA DE USO. La aplicación web exige aceptar unos términos de uso y una
política de descarga antes de enviar la petición. Invocar el servicio de forma
directa se salta ese diálogo. Corresponde al consultor revisar esas condiciones
y dejar constancia de la decisión en MANIFIESTO.yaml.

Solo usa la librería estándar.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .errores import ErrorHidrologia

__all__ = [
    "URL_SERVICIO",
    "SerieSolicitada",
    "ErrorDHIME",
    "construir_filtro",
    "construir_items",
    "enviar_trabajo",
    "consultar_trabajo",
    "esperar_trabajo",
]

URL_SERVICIO = ("https://dhime.ideam.gov.co/server/rest/services"
                "/AtencionCiudadano/DescargarArchivo/GPServer/DescargarArchivo")

_TIEMPO_ESPERA = 120
_UA = {"User-Agent": "Mozilla/5.0"}

# Estados que publica ArcGIS Server para un trabajo asíncrono.
_EN_CURSO = ("esriJobSubmitted", "esriJobWaiting", "esriJobExecuting",
             "esriJobNew")
_CORRECTO = "esriJobSucceeded"


class ErrorDHIME(ErrorHidrologia):
    """El servicio de descarga no respondió como se espera."""


@dataclass(frozen=True)
class SerieSolicitada:
    """
    Una serie a descargar: estación, parámetro y etiqueta.

    `tipo_serie` y `calculo` proceden del catálogo de series del propio
    servicio, guardado en data/01_crudos/ideam/api/. La etiqueta es la que
    identifica la serie de forma unívoca, por ejemplo PTPM_TT_M para
    precipitación total mensual.
    """

    estacion: str
    parametro: str
    etiqueta: str
    tipo_serie: str = "Estandar"
    calculo: str = ""

    def como_condicion(self) -> str:
        """Condición del filtro, con la sintaxis del servicio."""
        return (f"(IdParametro~eq~'{self.parametro}'"
                f"~and~Etiqueta~eq~'{self.etiqueta}'"
                f"~and~IdEstacion~eq~'{self.estacion}')")

    def como_item(self) -> dict[str, Any]:
        """Entrada de la lista Items. Los booleanos son de graficación."""
        return {
            "IdParametro": self.parametro,
            "Etiqueta": self.etiqueta,
            "EsEjeY1": False,
            "EsEjeY2": False,
            "EsTipoLinea": False,
            "EsTipoBarra": False,
            "TipoSerie": self.tipo_serie,
            "Calculo": self.calculo,
        }


def construir_filtro(
    series: Sequence[SerieSolicitada],
    fecha_inicio: str,
    fecha_fin: str,
    tipo_reporte: str = "csv",
) -> str:
    """
    Construye la cadena Filtro.

    Los tres indicadores 'mostrar' van siempre en true. No es opcional para este
    repositorio: sin Calificador no se detectan los registros ACUMULADO, que son
    falsos máximos en 24 horas (CLAUDE.md, sección 7), y sin NivelAprobacion no
    se puede resolver la precedencia al deduplicar.

    Excepciones
    -----------
    ValueError
        Si no se pide ninguna serie o el tipo de reporte no está admitido.
    """
    if not series:
        raise ValueError("Hay que solicitar al menos una serie.")
    if tipo_reporte not in ("csv", "Excel"):
        raise ValueError(f"tipo_reporte {tipo_reporte!r} no admitido.")

    condiciones = "~or~".join(s.como_condicion() for s in series)
    datos = {
        "sort": "",
        "filter": f"({condiciones})",
        "group": "",
        "fechaInicio": fecha_inicio,
        "fechaFin": fecha_fin,
        "mostrarGrado": "true",
        "mostrarCalificador": "true",
        "mostrarNivelAprobacion": "true",
        "tipoReporte": tipo_reporte,
    }
    return urllib.parse.urlencode(datos)


def construir_items(series: Sequence[SerieSolicitada]) -> str:
    """Construye la cadena Items, que es la lista de series serializada."""
    return json.dumps([s.como_item() for s in series], ensure_ascii=False)


def _peticion(url: str, datos: dict | None = None) -> dict:
    cuerpo = urllib.parse.urlencode(datos).encode() if datos else None
    peticion = urllib.request.Request(url, data=cuerpo, headers=_UA)
    try:
        with urllib.request.urlopen(peticion, timeout=_TIEMPO_ESPERA) as r:
            crudo = r.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise ErrorDHIME(f"No se pudo contactar con el servicio: {exc}") from exc
    try:
        return json.loads(crudo)
    except json.JSONDecodeError as exc:
        raise ErrorDHIME(
            f"El servicio devolvió algo que no es JSON: {crudo[:200]}"
        ) from exc


def enviar_trabajo(
    series: Sequence[SerieSolicitada],
    fecha_inicio: str,
    fecha_fin: str,
    tipo_reporte: str = "csv",
    url_servicio: str = URL_SERVICIO,
) -> str:
    """
    Envía el trabajo de descarga y devuelve su identificador.

    Excepciones
    -----------
    ErrorDHIME
        Si el servicio no acepta el trabajo o no devuelve identificador.
    """
    respuesta = _peticion(f"{url_servicio}/submitJob", {
        "Filtro": construir_filtro(series, fecha_inicio, fecha_fin, tipo_reporte),
        "Items": construir_items(series),
        "f": "json",
    })

    if "error" in respuesta:
        raise ErrorDHIME(f"El servicio rechazó el trabajo: {respuesta['error']}")

    identificador = respuesta.get("jobId")
    if not identificador:
        raise ErrorDHIME(
            f"El servicio no devolvió jobId. Respuesta: {str(respuesta)[:200]}"
        )
    return str(identificador)


def consultar_trabajo(identificador: str,
                      url_servicio: str = URL_SERVICIO) -> dict:
    """Consulta el estado de un trabajo enviado."""
    return _peticion(f"{url_servicio}/jobs/{identificador}?f=json")


def esperar_trabajo(
    identificador: str,
    url_servicio: str = URL_SERVICIO,
    espera_s: float = 5.0,
    maximo_s: float = 900.0,
) -> dict:
    """
    Sondea el trabajo hasta que termine y devuelve su estado final.

    Excepciones
    -----------
    ErrorDHIME
        Si el trabajo falla o si agota el tiempo máximo. Un trabajo que no
        termina no debe dejarse en segundo plano sin más: el módulo tiene que
        detenerse y reportar, no continuar sin los datos.
    """
    inicio = time.perf_counter()
    while True:
        estado = consultar_trabajo(identificador, url_servicio)
        situacion = estado.get("jobStatus", "")

        if situacion == _CORRECTO:
            return estado
        if situacion not in _EN_CURSO:
            mensajes = "; ".join(
                str(m.get("description", ""))[:120]
                for m in estado.get("messages", [])[:3]
            )
            raise ErrorDHIME(
                f"El trabajo {identificador} terminó en estado {situacion!r}. "
                f"{mensajes}"
            )

        if time.perf_counter() - inicio > maximo_s:
            raise ErrorDHIME(
                f"El trabajo {identificador} sigue en {situacion!r} tras "
                f"{maximo_s:.0f} s. Se abandona la espera."
            )
        time.sleep(espera_s)
