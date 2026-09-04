#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Arma el comprimido de entrega al cliente.

QUE HACE ADEMAS DE COMPRIMIR. Un .zip se hace con el explorador de archivos; lo
que este paso aporta es la VERIFICACION PREVIA y la trazabilidad. Antes de
empaquetar comprueba que el informe este, que el acta de entrega este, que
todos los anexos declarados con contenido lo tengan, y que el informe no haya
quedado con hallazgos bloqueantes. Un entregable incompleto se detecta al
armarlo, no cuando el cliente lo abre.

Y ESCRIBE DE QUE VERSION SALIO. El LEEME lleva el commit de la herramienta, la
fecha, el caudal de diseno y lo que queda pendiente de la mano del consultor.
Meses despues, esa es la unica forma de saber que produjo un entregable.

NO SE INCLUYE LO QUE NO ES DEL CLIENTE: ni los logs, ni el directorio de
trabajo intermedio, ni la configuracion del estudio. El paquete es el informe y
sus anexos, que es lo que el contrato entrega.

Uso:
    python tools/empaquetar_entrega.py --raiz C:/Estudios/refugio_del_valle
    python tools/empaquetar_entrega.py --raiz ... --sin-comprimir

Codigos de salida:
    0  paquete armado
    1  falta algo del entregable
    3  no se pudo leer la configuracion
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
import zipfile
from pathlib import Path

_RAIZ_CODIGO = Path(__file__).resolve().parents[1]
if str(_RAIZ_CODIGO / "src") not in sys.path:
    sys.path.insert(0, str(_RAIZ_CODIGO / "src"))

from comun import rutas  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorConfiguracion, ErrorRutas  # noqa: E402

SALIDA_CORRECTA = 0
SALIDA_INCOMPLETO = 1
SALIDA_ERROR = 3


def version_de_la_herramienta() -> str:
    """El commit con que se produjo el entregable, o vacio si no hay git."""
    try:
        salida = subprocess.run(
            ["git", "-C", str(_RAIZ_CODIGO), "describe", "--always", "--dirty"],
            capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return salida.stdout.strip() if salida.returncode == 0 else ""


def leer_reporte(base: Path, nombre: str) -> dict:
    """El JSON que dejo un modulo, o vacio si no esta."""
    ruta = rutas.directorio("procesado", base) / f"{nombre}.json"
    if not ruta.is_file():
        return {}
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def verificar(base: Path, informe: Path, anexos: Path) -> list[str]:
    """
    Lo que impide entregar. Devuelve los motivos, vacio si esta completo.

    SE COMPRUEBA EL BLOQUEANTE DEL M15, no solo que el archivo exista. Un
    informe se escribe igual con una tabla sin llenar o con prosa de otro
    estudio: el archivo esta y pesa lo mismo.
    """
    faltan: list[str] = []
    if not informe.is_file():
        faltan.append(f"no esta el informe en {informe}")
    if not anexos.is_dir():
        faltan.append(f"no esta el paquete de anexos en {anexos}")
    elif not (anexos / "ACTA_DE_ENTREGA.md").is_file():
        faltan.append("el paquete de anexos no trae ACTA_DE_ENTREGA.md")

    for modulo in ("M15_informe", "M17_anexos"):
        reporte = leer_reporte(base, modulo)
        if not reporte:
            faltan.append(f"no hay reporte de {modulo}: no se ha ejecutado")
            continue
        bloqueantes = [h for h in reporte.get("hallazgos", []) or []
                       if str(h.get("severidad", "")).upper() == "BLOQUEANTE"]
        for hallazgo in bloqueantes:
            faltan.append(f"{modulo} dejo un bloqueante: "
                          f"{hallazgo.get('clave')}")
    return faltan


def pendientes(base: Path) -> list[str]:
    """Lo que queda de la mano del consultor, para decirlo en el LEEME."""
    reporte = leer_reporte(base, "M15_informe")
    avisos = []
    for hallazgo in reporte.get("hallazgos", []) or []:
        if str(hallazgo.get("severidad", "")).upper() in ("ADVERTENCIA",):
            avisos.append(str(hallazgo.get("mensaje", "")).strip())
    reporte = leer_reporte(base, "M17_anexos")
    for hallazgo in reporte.get("hallazgos", []) or []:
        if str(hallazgo.get("clave", "")) == "anexos.faltan_opcionales":
            avisos.append(str(hallazgo.get("mensaje", "")).strip())
    return avisos


def caudales_de_diseno(base: Path) -> list[tuple[str, str, str]]:
    """(periodo, diseno, referencia) del punto de entrega."""
    import csv

    ruta = (rutas.directorio("procesado", base) / "hidrologia"
            / "escenarios_cc.csv")
    if not ruta.is_file():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        return [(f["periodo_retorno"], f["q_diseno_m3s"],
                 f["q_referencia_m3s"])
                for f in csv.DictReader(manejador, delimiter=";")]


def escribir_leeme(destino: Path, configuracion, base: Path,
                   informe: Path, anexos: Path) -> Path:
    """El LEEME que acompaña al paquete."""
    def dato(clave: str) -> str:
        return str(configuracion.obtener(clave, "") or "").strip()

    lineas = [
        f"# {dato('proyecto.nombre')}",
        "",
        "## Estudio hidrológico",
        "",
        f"- **Corriente:** {dato('proyecto.corriente')}",
        f"- **Municipio:** {dato('proyecto.municipio')}, "
        f"{dato('proyecto.departamento')}",
        f"- **Autoridad ambiental:** {dato('proyecto.autoridad_ambiental')}",
        f"- **Contratante:** {dato('proyecto.contratante')}",
        f"- **Consultor:** {dato('proyecto.consultor')}",
        f"- **Fecha del paquete:** {_dt.date.today().isoformat()}",
    ]
    version = version_de_la_herramienta()
    if version:
        lineas.append(f"- **Versión de la cadena de cálculo:** `{version}`")

    caudales = caudales_de_diseno(base)
    if caudales:
        lineas += [
            "",
            "## Caudales máximos en el sitio de proyecto",
            "",
            "El caudal de diseño es el del escenario CON factor de cambio "
            "climático. El de referencia representa la lluvia registrada y se "
            "presenta para mostrar qué parte del caudal procede de la "
            "proyección y qué parte del dato histórico.",
            "",
            "| Periodo de retorno (años) | Diseño (m³/s) | Referencia (m³/s) |",
            "|---|---|---|",
        ]
        for periodo, diseno, referencia in caudales:
            lineas.append(f"| {periodo} | {diseno} | {referencia} |")

    lineas += [
        "",
        "## Contenido",
        "",
        f"- `INFORME/{informe.name}`",
        "- `ANEXOS/` con el acta de entrega, que lista la huella de cada "
        "anexo. Esa huella es lo que permite comprobar meses después que el "
        "anexo es el que el estudio produjo.",
        "",
        "## Antes de leer el informe",
        "",
        "Abrir el documento en Word y aceptar la actualización de campos. Los "
        "índices de contenido, de ilustraciones y de tablas, y las leyendas "
        "numeradas, son campos que conservan su valor en caché: quedan "
        "marcados para recalcularse, pero solo Word lo hace.",
        "",
        "## Lo que queda pendiente",
        "",
    ]
    avisos = pendientes(base)
    if avisos:
        for aviso in avisos:
            lineas.append(f"- {aviso}")
    else:
        lineas.append("- Nada pendiente reportado por la cadena.")
    lineas.append("")

    ruta = destino / "LEEME.md"
    ruta.write_text("\n".join(lineas), encoding="utf-8")
    return ruta


def comprimir(zip_destino: Path, informe: Path, anexos: Path,
              leeme: Path, raiz_interna: str) -> tuple[int, float]:
    """Escribe el .zip y devuelve (archivos, MB)."""
    zip_destino.parent.mkdir(parents=True, exist_ok=True)
    archivos = 0
    with zipfile.ZipFile(zip_destino, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as paquete:
        paquete.write(leeme, f"{raiz_interna}/LEEME.md")
        paquete.write(informe, f"{raiz_interna}/INFORME/{informe.name}")
        archivos += 2
        for ruta in sorted(anexos.rglob("*")):
            if not ruta.is_file():
                continue
            relativa = ruta.relative_to(anexos).as_posix()
            paquete.write(ruta, f"{raiz_interna}/ANEXOS/{relativa}")
            archivos += 1
    return archivos, zip_destino.stat().st_size / (1024 * 1024)


def main(argv=None) -> int:
    analizador = argparse.ArgumentParser(description=DESCRIPCION)
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument(
        "--sin-comprimir", action="store_true",
        help="Verifica y escribe el LEEME, pero no arma el .zip.")
    argumentos = analizador.parse_args(argv)

    try:
        base = (Path(argumentos.raiz).resolve() if argumentos.raiz
                else rutas.raiz_proyecto())
        configuracion = cargar(ruta=argumentos.config, raiz=base)
        resultados = rutas.directorio("resultados", base)
        informe = resultados / str(configuracion.obtener("informe.archivo"))
        anexos = resultados / "anexos"
    except (ErrorConfiguracion, ErrorRutas) as error:
        print(f"empaquetar_entrega: {error}", file=sys.stderr)
        return SALIDA_ERROR

    faltan = verificar(base, informe, anexos)
    if faltan:
        print("NO SE ARMA EL PAQUETE:")
        for motivo in faltan:
            print(f"  - {motivo}")
        return SALIDA_INCOMPLETO

    nombre = str(configuracion.obtener("proyecto.nombre", "estudio"))
    etiqueta = "".join(c if c.isalnum() else "_" for c in nombre).strip("_")
    raiz_interna = f"{etiqueta}_estudio_hidrologico_{_dt.date.today():%Y%m%d}"

    destino = resultados / "entrega"
    destino.mkdir(parents=True, exist_ok=True)
    leeme = escribir_leeme(destino, configuracion, base, informe, anexos)
    print(f"LEEME: {leeme}")

    if argumentos.sin_comprimir:
        print("Verificado. No se comprimio, por --sin-comprimir.")
        return SALIDA_CORRECTA

    zip_destino = destino / f"{raiz_interna}.zip"
    print(f"Comprimiendo en {zip_destino} ...")
    archivos, megas = comprimir(zip_destino, informe, anexos, leeme,
                                raiz_interna)
    print(f"Paquete: {zip_destino}")
    print(f"  archivos  {archivos}")
    print(f"  tamano    {megas:.1f} MB")
    return SALIDA_CORRECTA


DESCRIPCION = "Comprimido de entrega al cliente"

if __name__ == "__main__":
    raise SystemExit(main())
