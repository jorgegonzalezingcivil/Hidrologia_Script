#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M17 - Ensamble y verificación de anexos
=======================================
Entorno: venv del proyecto.

Reúne en un solo paquete lo que el informe cita como anexo, comprueba que esté
completo y deja constancia de qué contiene cada uno.

NO ES UN COPIADOR DE CARPETAS. Lo que hace y que no se puede hacer a mano sin
equivocarse es RESPONDER si el entregable está completo: el informe remite a
ocho anexos por su número, y quien lo revise abrirá el paquete buscando
exactamente eso. Un anexo que el texto cita y el paquete no trae es un hallazgo
de interventoría, y no se nota al armar el zip.

LA ESTRUCTURA ES DOCTRINA Y VIVE EN config/anexos.yaml. Los números y títulos
están transcritos del índice de anexos de la plantilla del consultor, que es lo
que el informe cita en su texto. Cambiar uno aquí sin cambiarlo allí deja al
informe remitiendo a un anexo que no existe, y por eso el módulo compara los dos
y lo reporta.

SE DISTINGUE LO QUE FALTA DE LO QUE NO APLICA. Un anexo obligatorio ausente es
BLOQUEANTE. Uno opcional ausente es una advertencia con su motivo: los planos
topográficos los entrega el consultor y puede que aún no los tenga, y eso es
distinto de que la cadena no haya producido el balance.

SE CALCULA LA HUELLA DE CADA ARCHIVO. Es lo que permite demostrar, meses
después, que el anexo entregado es el mismo que el estudio produjo. Sin ella,
una revisión que encuentre un número distinto no puede saber si el dato cambió
o si el archivo se sustituyó.

EL PAQUETE SE REESCRIBE ENTERO en cada corrida. Un anexo de una versión anterior
mezclado con los de esta no se distingue de los demás, y el informe citaría
cifras que el anexo no respalda.

Productos:
    data/05_resultados/anexos/<N>. <Titulo>/...
    data/05_resultados/anexos/ACTA_DE_ENTREGA.md
    data/02_procesado/M17_anexos.json

Uso:
    python src/M17_anexos.py
    python src/M17_anexos.py --sin-copiar    solo verifica y reporta

Códigos de salida:
    0  paquete armado sin hallazgos bloqueantes
    1  falta un anexo obligatorio
    3  no se pudo leer la configuración o la estructura
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M17"
DESCRIPCION = "Ensamble y verificación de anexos"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3


@dataclass
class Pieza:
    """Un anexo o subanexo, con lo que se encontró de él."""

    numero: str
    titulo: str
    origen: str
    fuente: str
    obligatorio: bool
    nota: str = ""
    archivos: list[dict[str, Any]] = field(default_factory=list)
    bytes: int = 0

    @property
    def hay(self) -> bool:
        return bool(self.archivos)


@dataclass
class ResultadoM17:
    piezas: list[Pieza] = field(default_factory=list)
    faltan_obligatorios: list[str] = field(default_factory=list)
    faltan_opcionales: list[str] = field(default_factory=list)
    sin_citar: list[str] = field(default_factory=list)
    destino: str = ""
    archivos: int = 0
    bytes: int = 0
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def leer_estructura(ruta: Path) -> dict[str, Any]:
    """
    Qué anexos declara el estudio y de dónde sale cada uno.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si no declara ningún anexo: un paquete vacío no es un entregable.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra la estructura de anexos en {ruta}.")
    datos = leer_yaml(ruta) or {}
    if not datos.get("anexos"):
        raise ErrorFormato(f"{ruta.name} no declara ningun anexo.")
    return datos


def aplanar(estructura: dict[str, Any]) -> list[Pieza]:
    """
    Convierte el árbol de anexos en la lista de piezas que hay que buscar.

    UN ANEXO CON SUBANEXOS NO TIENE ORIGEN PROPIO: lo que se entrega son sus
    hijos. Tratarlo como pieza produciría una carpeta vacía con su número, que
    al revisar se lee como un anexo que se perdió.
    """
    piezas: list[Pieza] = []
    for anexo in estructura.get("anexos") or []:
        hijos = anexo.get("subanexos") or []
        candidatos = hijos if hijos else [anexo]
        for ficha in candidatos:
            piezas.append(Pieza(
                numero=str(ficha.get("numero", "")).strip(),
                titulo=str(ficha.get("titulo", "")).strip(),
                origen=str(ficha.get("origen", "")).strip(),
                fuente=str(ficha.get("fuente", "")).strip(),
                obligatorio=bool(ficha.get("obligatorio", False)),
                nota=str(ficha.get("nota", "") or "").strip(),
            ))
    return piezas


def excluido(nombre: str, patrones: Sequence[str]) -> bool:
    """Si un archivo cae en la lista de lo que nunca se copia."""
    return any(fnmatch(nombre, patron) for patron in patrones)


def buscar_archivos(origen: Path, base: Path,
                    excluir: Sequence[str]) -> list[Path]:
    """
    Los archivos de un origen, sea archivo, carpeta o comodín.

    SE RECORRE LA CARPETA ENTERA. Un anexo declarado sobre un directorio
    entrega lo que ese directorio tenga el día de la corrida, no una lista
    fijada: si un módulo añade un producto, entra solo.
    """
    origen = Path(origen)
    if any(c in origen.name for c in "*?["):
        candidatos = sorted(origen.parent.glob(origen.name))
    elif origen.is_dir():
        candidatos = sorted(p for p in origen.rglob("*") if p.is_file())
    elif origen.is_file():
        candidatos = [origen]
    else:
        candidatos = []
    return [p for p in candidatos
            if p.is_file() and not excluido(p.name, excluir)]


def huella(ruta: Path, bloque: int = 1 << 20) -> str:
    """
    SHA-256 de un archivo, leído por bloques.

    POR BLOQUES Y NO DE UNA VEZ. Los crudos del IDEAM y el DEM recortado pesan
    cientos de megas, y cargarlos enteros en memoria para resumirlos no aporta
    nada y puede no caber.
    """
    resumen = hashlib.sha256()
    with Path(ruta).open("rb") as archivo:
        for trozo in iter(lambda: archivo.read(bloque), b""):
            resumen.update(trozo)
    return resumen.hexdigest()


def carpeta_de(pieza: Pieza) -> str:
    """
    Nombre de la carpeta de un anexo, tal como el informe lo cita.

    Se limpian los caracteres que Windows no admite en un nombre de carpeta.
    Sin esto, 'Análisis de Crecientes (HEC-HMS)' pasa, pero cualquier titulo
    con dos puntos o barra rompe la escritura y el paquete queda a medias.
    """
    limpio = f"{pieza.numero}. {pieza.titulo}"
    for malo in '<>:"/\\|?*':
        limpio = limpio.replace(malo, "-")
    return limpio.strip().rstrip(".")


def numeros_citados(documento) -> set[str]:
    """
    Los números de anexo que el texto del informe menciona.

    SIRVE PARA CONTRASTAR LOS DOS SENTIDOS. Que el paquete traiga los ocho
    anexos no basta: si el informe cita un 'Anexo 9' que nadie declaro, quien
    lo busque no lo encontrara, y eso no se ve mirando solo la estructura.
    """
    import re

    patron = re.compile(r"\bAnexo\s+(\d+(?:\.\d+)?)", re.I)
    citados: set[str] = set()
    for parrafo in documento.paragraphs:
        citados.update(patron.findall(parrafo.text))
    for tabla in documento.tables:
        for fila in tabla.rows:
            for celda in fila.cells:
                citados.update(patron.findall(celda.text))
    return citados


def escribir_acta(destino: Path, piezas: Sequence[Pieza],
                  proyecto: str, fecha: str) -> Path:
    """
    Acta de entrega: qué trae cada anexo y con qué huella.

    ES EL DOCUMENTO QUE SOSTIENE EL PAQUETE. Meses después, una revisión que
    encuentre una cifra distinta puede comprobar contra estas huellas si el dato
    cambió o si el archivo se sustituyó. Sin ella no hay forma de distinguirlo.
    """
    lineas = [
        f"# Acta de entrega de anexos",
        "",
        f"**Proyecto:** {proyecto}",
        f"**Fecha de armado:** {fecha}",
        "",
        "La huella SHA-256 de cada archivo permite comprobar que el anexo",
        "entregado es el mismo que el estudio produjo.",
        "",
    ]
    total_archivos = total_bytes = 0
    for pieza in piezas:
        lineas.append(f"## Anexo {pieza.numero}. {pieza.titulo}")
        lineas.append("")
        if pieza.nota:
            lineas.append(f"> {pieza.nota}")
            lineas.append("")
        if not pieza.hay:
            estado = ("NO ENTREGADO, y es obligatorio" if pieza.obligatorio
                      else "no entregado")
            lineas.append(f"**{estado}.** Origen declarado: `{pieza.origen}`")
            lineas.append("")
            continue
        total_archivos += len(pieza.archivos)
        total_bytes += pieza.bytes
        lineas.append(f"Origen: `{pieza.origen}` ({pieza.fuente})")
        lineas.append("")
        lineas.append(f"{len(pieza.archivos)} archivo(s), "
                      f"{pieza.bytes / 1e6:.1f} MB.")
        lineas.append("")
        lineas.append("| Archivo | Bytes | SHA-256 |")
        lineas.append("|---|---:|---|")
        for ficha in pieza.archivos:
            lineas.append(f"| `{ficha['nombre']}` | {ficha['bytes']:,} | "
                          f"`{ficha['sha256'][:16]}…` |")
        lineas.append("")

    lineas.insert(7, f"**Total:** {total_archivos} archivo(s), "
                     f"{total_bytes / 1e6:.1f} MB.")
    lineas.insert(8, "")

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(lineas), encoding="utf-8")
    return destino


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    copiar: bool = True,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Arma el paquete de anexos y comprueba que esté completo."""
    import datetime as _dt

    inicio = time.perf_counter()
    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM17()

    ruta_estructura = rutas.resolver(
        configuracion.obtener("anexos.estructura"), rutas.raiz_codigo())
    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"estructura": rutas.relativa(ruta_estructura,
                                              rutas.raiz_codigo())},
        parametros={"anexos.calcular_hash": configuracion.obtener(
            "anexos.calcular_hash", True)},
    )

    try:
        estructura = leer_estructura(ruta_estructura)
    except (ErrorRutas, ErrorFormato) as error:
        resultado.hallazgos.append(Hallazgo(BLOQUEANTE, "anexos.estructura",
                                            str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_ERROR)

    piezas = aplanar(estructura)
    excluir = estructura.get("excluir") or []
    con_huella = bool(configuracion.obtener("anexos.calcular_hash", True))
    destino = rutas.resolver(str(estructura.get("destino")), base)
    resultado.destino = rutas.relativa(destino, base)

    # --- Lo que hay de cada anexo -------------------------------------------
    with registro.bloque(logger, "Inventario de anexos"):
        for pieza in piezas:
            encontrados = buscar_archivos(
                rutas.resolver(pieza.origen, base), base, excluir)
            for archivo in encontrados:
                ficha = {"nombre": archivo.name,
                         "ruta": rutas.relativa(archivo, base),
                         "bytes": archivo.stat().st_size}
                ficha["sha256"] = huella(archivo) if con_huella else ""
                pieza.archivos.append(ficha)
                pieza.bytes += ficha["bytes"]
            resultado.piezas.append(pieza)
            if not pieza.hay:
                destino_lista = (resultado.faltan_obligatorios
                                 if pieza.obligatorio
                                 else resultado.faltan_opcionales)
                destino_lista.append(f"{pieza.numero}. {pieza.titulo}")
        resultado.archivos = sum(len(p.archivos) for p in resultado.piezas)
        resultado.bytes = sum(p.bytes for p in resultado.piezas)
        logger.info("%d anexo(s) declarados | %d con contenido | %d archivo(s), "
                    "%.1f MB", len(piezas),
                    sum(1 for p in resultado.piezas if p.hay),
                    resultado.archivos, resultado.bytes / 1e6)

    # --- Lo que el informe cita y no esta declarado -------------------------
    with registro.bloque(logger, "Contraste con el informe"):
        plantilla = rutas.resolver(
            configuracion.obtener("informe.plantilla_base"),
            rutas.raiz_codigo())
        try:
            import docx_plantilla as plantilla_docx

            documento = plantilla_docx.abrir(plantilla)
            citados = numeros_citados(documento)
            declarados = {p.numero for p in piezas}
            # Un anexo padre se cita por su numero aunque se entregue por hijos.
            declarados |= {n.split(".")[0] for n in declarados}
            resultado.sin_citar = sorted(citados - declarados)
            logger.info("%d numero(s) de anexo citados en el informe, %d sin "
                        "declarar", len(citados), len(resultado.sin_citar))
        except Exception as error:  # noqa: BLE001 - el contraste no es esencial
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "anexos.sin_contraste",
                f"no se pudo contrastar contra el informe ({error}): no se "
                "puede afirmar que el paquete traiga todo lo que el texto "
                "cita."))

    # --- El paquete ---------------------------------------------------------
    if copiar:
        with registro.bloque(logger, "Ensamble del paquete"):
            # SE REESCRIBE ENTERO. Un anexo de una version anterior mezclado
            # con los de esta no se distingue de los demas, y el informe
            # citaria cifras que el anexo no respalda.
            if destino.exists():
                shutil.rmtree(destino)
            destino.mkdir(parents=True, exist_ok=True)
            for pieza in resultado.piezas:
                if not pieza.hay:
                    continue
                carpeta = destino / carpeta_de(pieza)
                carpeta.mkdir(parents=True, exist_ok=True)
                raiz_origen = rutas.resolver(pieza.origen, base)
                for ficha in pieza.archivos:
                    fuente = base / ficha["ruta"]
                    # Se conserva la estructura interna de la carpeta de
                    # origen: los crudos del IDEAM vienen por estacion y
                    # aplanarlos perderia de cual es cada archivo.
                    try:
                        relativa = fuente.relative_to(raiz_origen)
                    except ValueError:
                        relativa = Path(fuente.name)
                    salida = carpeta / relativa
                    salida.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(fuente, salida)
            acta = escribir_acta(
                destino / "ACTA_DE_ENTREGA.md", resultado.piezas,
                str(configuracion.obtener("proyecto.nombre", "")),
                _dt.date.today().isoformat())
            resultado.productos.append(rutas.relativa(acta, base))
            resultado.productos.append(resultado.destino)
            logger.info("paquete armado en %s", resultado.destino)

    resultado.hallazgos.extend(_resumir(resultado))
    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _resumir(resultado: ResultadoM17) -> list[Hallazgo]:
    """Qué quedó entregado y qué no."""
    hallazgos: list[Hallazgo] = []
    con_contenido = [p for p in resultado.piezas if p.hay]
    if con_contenido:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "anexos.armados",
            f"{len(con_contenido)} de {len(resultado.piezas)} anexo(s) con "
            f"contenido: {resultado.archivos} archivo(s), "
            f"{resultado.bytes / 1e6:.1f} MB. El acta de entrega lista la "
            "huella de cada uno, que es lo que permite comprobar meses despues "
            "que el anexo es el que el estudio produjo."))

    if resultado.faltan_obligatorios:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "anexos.faltan_obligatorios",
            f"{len(resultado.faltan_obligatorios)} anexo(s) OBLIGATORIOS sin "
            f"contenido: {resultado.faltan_obligatorios}. El informe los cita "
            "por su numero y quien lo revise los buscara en el paquete. No se "
            "entrega asi."))

    if resultado.faltan_opcionales:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "anexos.faltan_opcionales",
            f"{len(resultado.faltan_opcionales)} anexo(s) opcionales sin "
            f"contenido: {resultado.faltan_opcionales}. Son los que aporta el "
            "consultor, no la cadena. Si el estudio se entrega sin ellos, el "
            "informe debe decir por que."))

    if resultado.sin_citar:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "anexos.citados_sin_declarar",
            f"el informe cita anexo(s) que la estructura no declara: "
            f"{resultado.sin_citar}. O sobra la cita, o falta el anexo; en los "
            "dos casos quien lo busque no lo encontrara."))
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
            emitir("  %-40s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    if ruta_json is None:
        ruta_json = (rutas.directorio("procesado", base, crear=True)
                     / "M17_anexos.json")
    reporte = {
        "modulo": MODULO,
        "destino": resultado.destino,
        "anexos": [{"numero": p.numero, "titulo": p.titulo,
                    "origen": p.origen, "fuente": p.fuente,
                    "obligatorio": p.obligatorio,
                    "archivos": len(p.archivos), "bytes": p.bytes,
                    "detalle": p.archivos}
                   for p in resultado.piezas],
        "faltan_obligatorios": resultado.faltan_obligatorios,
        "faltan_opcionales": resultado.faltan_opcionales,
        "citados_sin_declarar": resultado.sin_citar,
        "archivos": resultado.archivos,
        "bytes": resultado.bytes,
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


def main(argv: Sequence[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(description=DESCRIPCION)
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None)
    analizador.add_argument("--sin-copiar", action="store_true",
                            help="solo verifica y reporta, no arma el paquete")
    analizador.add_argument("--silencioso", action="store_true")
    argumentos = analizador.parse_args(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json, copiar=not argumentos.sin_copiar,
            consola=not argumentos.silencioso)
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato) as error:
        print(f"{MODULO}: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
