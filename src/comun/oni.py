# -*- coding: utf-8 -*-
"""
Adaptador del índice ONI de la NOAA
===================================

Doctrina (CLAUDE.md, sección 2): los puntos frágiles ante actualizaciones
externas se aíslan en adaptadores. El archivo `oni.ascii.txt` lo publica el
Climate Prediction Center de la NOAA y su formato puede cambiar sin aviso, de
modo que toda la interpretación vive aquí y ningún módulo la repite.

Solo librería estándar, como el resto de `comun`.

Qué es el ONI. El Oceanic Niño Index es la media móvil de tres meses de la
anomalía de temperatura superficial del mar en la región Niño 3.4. Cada fila del
archivo es una TEMPORADA de tres meses, no un mes:

    SEAS  YR   TOTAL   ANOM
     DJF 1950  25.01  -1.32

'DJF' cubre diciembre del año anterior, enero y febrero, y su mes central es
enero. Esa correspondencia es la que permite clasificar mes a mes, y es lo que
la rutina heredada ENSOONI.py no hacía: asignaba una etiqueta por año
calendario, con lo que un episodio como el Niño de 1997-98 quedaba partido en
dos y los meses de cada año recibían una sola etiqueta promediada.

Definición oficial de episodio (NOAA): se declara Niño o Niña cuando el umbral
de más o menos 0,5 grados se alcanza durante al menos cinco TEMPORADAS
CONSECUTIVAS superpuestas. Una temporada aislada por encima del umbral no
constituye episodio, y por eso el conteo de rachas no se puede omitir.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .errores import ErrorFormato, ErrorRutas

__all__ = [
    "RegistroONI",
    "MES_CENTRAL",
    "FASE_NINO",
    "FASE_NINA",
    "FASE_NEUTRAL",
    "FASE_COMPUESTA",
    "NOMBRE_DE_FASE",
    "nombre_de_fase",
    "COLOR_DE_FASE",
    "RELLENO_DE_FASE",
    "descargar",
    "leer",
    "interpretar",
    "clasificar",
]

# Mes central de cada temporada de tres meses. DJF abarca diciembre del año
# anterior, enero y febrero: su centro es enero.
MES_CENTRAL: dict[str, int] = {
    "DJF": 1, "JFM": 2, "FMA": 3, "MAM": 4, "AMJ": 5, "MJJ": 6,
    "JJA": 7, "JAS": 8, "ASO": 9, "SON": 10, "OND": 11, "NDJ": 12,
}

FASE_NINO = "nino"
FASE_NINA = "nina"
FASE_NEUTRAL = "neutral"

# NO ES UNA FASE, ES SU AGREGADO. El año compuesto reúne todos los meses sin
# separarlos por episodio, y representa el régimen medio del sitio: es la lámina
# que va al balance hídrico y la que el informe presenta antes de abrir el
# análisis por fase. Se nombra aquí junto a las tres para que ningún módulo la
# escriba con otra etiqueta.
FASE_COMPUESTA = "compuesto"

# CÓMO SE NOMBRA Y SE PINTA CADA FASE, EN UN SOLO SITIO. El informe pone juntas
# las figuras del M05b y las del M06, y basta con que una use rojo para el Niño y
# otra verde para que el lector deje de fiarse de las dos. Se declara aquí, al
# lado de las propias fases, para que ningún módulo invente la suya.
#
# ROJO PARA EL NIÑO Y AZUL PARA LA NIÑA no es una elección estética: es la
# convención del NOAA y del IDEAM, que es contra quien se contrasta este
# análisis. El cálido va en rojo y el frío en azul; invertirlo obliga a leer la
# leyenda en cada figura para saber qué se está mirando.
NOMBRE_DE_FASE: dict[str, str] = {
    FASE_NINO: "El Niño",
    FASE_NINA: "La Niña",
    FASE_NEUTRAL: "Neutral",
    FASE_COMPUESTA: "Año compuesto",
}

def nombre_de_fase(fase: str) -> str:
    """
    Como se escribe una fase donde alguien la va a leer.

    Las claves viajan por los archivos y por los nombres de columna, y ahi
    siguen sin tilde ni ene porque cambiarlas romperia productos ya calculados.
    Lo que el informe muestra es otra cosa: 'nino' en la leyenda de un grafico
    es tan visible como un numero mal puesto.
    """
    return NOMBRE_DE_FASE.get(str(fase), str(fase))


COLOR_DE_FASE: dict[str, str] = {
    FASE_NINO: "#c00000",
    FASE_NINA: "#1f4e79",
    FASE_NEUTRAL: "#9a9a9a",
    FASE_COMPUESTA: "#404040",
}

# Version clara para rellenos de area, donde el color pleno tapa lo que hay
# debajo. Mantiene el tono para que se reconozca la fase sin leer la leyenda.
RELLENO_DE_FASE: dict[str, str] = {
    FASE_NINO: "#f2b8b5",
    FASE_NINA: "#b8cce4",
    FASE_NEUTRAL: "#d9d9d9",
    FASE_COMPUESTA: "#cccccc",
}


CABECERA_ESPERADA = ("SEAS", "YR", "TOTAL", "ANOM")


@dataclass(frozen=True)
class RegistroONI:
    """Una temporada del índice, ya situada en su mes central."""

    temporada: str
    anio: int
    total: float
    anomalia: float

    @property
    def mes(self) -> int:
        return MES_CENTRAL[self.temporada]

    @property
    def clave(self) -> tuple[int, int]:
        return (self.anio, self.mes)


def descargar(url: str, destino: Path, tiempo_maximo: int = 60) -> Path:
    """
    Trae el archivo del servicio y lo guarda tal cual se recibió.

    El crudo se conserva sin tocar (CLAUDE.md, sección 8: los descargados van a
    data/01_crudos). Si el servicio no responde y ya existe una copia local, se
    reutiliza y quien llama lo reporta: un estudio no puede detenerse porque la
    NOAA esté caída, pero tampoco debe usar una copia vieja sin decirlo.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=tiempo_maximo) as respuesta:
            contenido = respuesta.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if destino.is_file():
            raise ErrorRutas(
                f"no se pudo consultar {url} ({exc}). Existe una copia local en "
                f"{destino}, que quien llama debe decidir si usa."
            ) from exc
        raise ErrorRutas(
            f"no se pudo consultar {url} ({exc}) y no hay copia local en "
            f"{destino}."
        ) from exc
    destino.write_text(contenido, encoding="utf-8")
    return destino


def leer(ruta: Path) -> str:
    """Devuelve el contenido del archivo local."""
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra el índice ONI en {ruta}.")
    return ruta.read_text(encoding="utf-8", errors="replace")


def interpretar(contenido: str) -> list[RegistroONI]:
    """
    Convierte el texto en registros, verificando la cabecera.

    La cabecera se comprueba de forma exacta y no por posición: si la NOAA
    reordena o renombra columnas, el módulo debe detenerse en lugar de leer un
    número de la columna equivocada, que produciría una clasificación errónea
    sin ninguna señal.
    """
    lineas = [l for l in contenido.splitlines() if l.strip()]
    if not lineas:
        raise ErrorFormato("el índice ONI llegó vacío.")

    cabecera = tuple(lineas[0].split())
    if cabecera != CABECERA_ESPERADA:
        raise ErrorFormato(
            f"la cabecera del índice ONI cambió: se esperaba "
            f"{CABECERA_ESPERADA} y se leyó {cabecera}. El formato de la NOAA "
            "se modificó y el adaptador debe revisarse antes de continuar."
        )

    registros: list[RegistroONI] = []
    for numero, linea in enumerate(lineas[1:], start=2):
        partes = linea.split()
        if len(partes) != 4:
            raise ErrorFormato(
                f"la línea {numero} del índice ONI tiene {len(partes)} campo(s) "
                f"y se esperaban 4: {linea!r}."
            )
        temporada = partes[0].strip().upper()
        if temporada not in MES_CENTRAL:
            raise ErrorFormato(
                f"temporada no reconocida en la línea {numero}: {temporada!r}. "
                f"Las válidas son {sorted(MES_CENTRAL)}."
            )
        try:
            registros.append(RegistroONI(
                temporada, int(partes[1]), float(partes[2]), float(partes[3])))
        except ValueError as exc:
            raise ErrorFormato(
                f"no se pudieron interpretar los números de la línea {numero}: "
                f"{linea!r} ({exc})."
            ) from exc
    return registros


def _signo(anomalia: float, umbral: float) -> str:
    if anomalia >= umbral:
        return FASE_NINO
    if anomalia <= -umbral:
        return FASE_NINA
    return FASE_NEUTRAL


def clasificar(
    registros: Sequence[RegistroONI],
    umbral: float = 0.5,
    consecutivas: int = 5,
    exigir_consecutivas: bool = True,
) -> list[dict]:
    """
    Asigna fase a cada temporada, aplicando la definición oficial de episodio.

    Se declara Niño o Niña cuando el umbral se alcanza durante al menos
    'consecutivas' temporadas seguidas. Una temporada aislada por encima del
    umbral NO constituye episodio y queda neutral: sin ese control, cualquier
    oscilación breve inflaría el conteo de años Niño y la agregación por fase
    mezclaría meses que no pertenecen a ningún evento.

    Con 'exigir_consecutivas' en falso se clasifica por umbral simple, que es lo
    que el consultor puede pedir para contrastar, y entonces el resultado NO
    corresponde a la definición de la NOAA.

    Devuelve una fila por temporada, con su mes central, su fase y el
    identificador del episodio al que pertenece.
    """
    if not registros:
        return []
    ordenados = sorted(registros, key=lambda r: r.clave)
    crudos = [_signo(r.anomalia, umbral) for r in ordenados]

    if not exigir_consecutivas:
        fases = list(crudos)
    else:
        fases = [FASE_NEUTRAL] * len(crudos)
        inicio = 0
        while inicio < len(crudos):
            fin = inicio
            while fin + 1 < len(crudos) and crudos[fin + 1] == crudos[inicio]:
                fin += 1
            largo = fin - inicio + 1
            if crudos[inicio] != FASE_NEUTRAL and largo >= consecutivas:
                for indice in range(inicio, fin + 1):
                    fases[indice] = crudos[inicio]
            inicio = fin + 1

    filas: list[dict] = []
    episodio = 0
    anterior = FASE_NEUTRAL
    for registro, fase in zip(ordenados, fases):
        if fase != FASE_NEUTRAL and fase != anterior:
            episodio += 1
        anterior = fase
        filas.append({
            "anio": registro.anio,
            "mes": registro.mes,
            "temporada": registro.temporada,
            "anomalia": registro.anomalia,
            "fase": fase,
            "fase_por_umbral": crudos[len(filas)],
            "episodio": episodio if fase != FASE_NEUTRAL else None,
        })
    return filas


def resumen_por_fase(filas: Iterable[dict]) -> dict[str, int]:
    """Cuántas temporadas quedaron en cada fase."""
    conteo: dict[str, int] = {FASE_NINO: 0, FASE_NINA: 0, FASE_NEUTRAL: 0}
    for fila in filas:
        conteo[fila["fase"]] = conteo.get(fila["fase"], 0) + 1
    return conteo
