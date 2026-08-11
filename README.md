# Automatización de estudios hidrológicos

Cadena de módulos que produce un estudio hidrológico completo, desde la
descarga de datos del IDEAM hasta el informe en Word, sin abrir software de
forma manual salvo en un paso.

La doctrina técnica del proyecto (decisiones cerradas, alcance de cada módulo,
alertas permanentes) está en [CLAUDE.md](CLAUDE.md). Este archivo solo explica
cómo dejar una máquina en condiciones de ejecutar.

---

## 1. Qué hace falta antes de empezar

| Requisito | Versión | Lo necesitan |
|---|---|---|
| Python | 3.12 | Todos los módulos de análisis |
| QGIS | la declarada en `config.yaml` | M00b, M01, M02, M06, M08, M11, M16 |
| HEC-HMS | la declarada en `config.yaml` | M09 y M13, solo en modo detallado |
| Git | cualquiera reciente | Obtener y actualizar el repositorio |

Se adopta el **esquema de doble entorno** (CLAUDE.md, sección 3). Los módulos
SIG corren con el Python que trae QGIS; los de análisis, con el `venv` propio
del proyecto. No se mezclan: los módulos SIG no importan librerías del `venv`.

Además del repositorio hacen falta dos cosas que **no** están en él:

- **Capas nacionales de referencia**, cerca de 4 GB (drenaje 1:100.000 del
  IGAC, cobertura Corine Land Cover 2018 del IDEAM, ráster global de grupo
  hidrológico de suelo HYSOGs250m). Se copian de otro miembro del equipo.
- **Credenciales de Earthdata**, para que el M02 descargue el DEM. Cada quien
  usa su propia cuenta, gratuita, de <https://urs.earthdata.nasa.gov>.

---

## 2. Instalación

### 2.1 Obtener el repositorio

```
git clone <url-del-remoto> C:\Hidrologia_Script
cd C:\Hidrologia_Script
```

### 2.2 Crear el entorno de análisis

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

El `venv` no se copia de otra máquina: los lanzadores de `Scripts\` llevan la
ruta absoluta del intérprete embebida y dejan de funcionar al cambiar de sitio.

### 2.3 Declarar lo que es propio de esta máquina

`config/config.yaml` es doctrina del estudio y es igual para todo el equipo.
**No se edita** para acomodar una instalación local. Lo propio de cada máquina
va en un archivo aparte, que no se versiona:

```
copy config\config.local.ejemplo.yaml config\config.local.yaml
```

Abrir la copia y ajustar dónde está instalado QGIS, dónde está HEC-HMS y dónde
quedaron las capas nacionales. La plantilla explica clave por clave qué admite
y qué no. Lo que no se declare conserva el valor compartido.

Ese archivo **solo** puede sobrescribir claves de máquina. Si intenta cambiar
un umbral, un periodo de retorno o un método, la carga se detiene con un
mensaje explícito. La restricción es deliberada: un parámetro técnico distinto
en cada equipo produciría resultados distintos del mismo estudio sin dejar
rastro, y un estudio que no puede explicar sus decisiones no es defendible ante
interventoría (CLAUDE.md, sección 7). Todo lo que se sustituya queda escrito en
el log de cada módulo, con el valor compartido y el propio, uno al lado del
otro.

### 2.4 Credenciales

Las credenciales **nunca** van a un archivo del repositorio. El M02 las lee de
un `netrc`, que es también el que leen `curl`, `wget` y GDAL. En Windows se
crea como `C:\Users\<usuario>\.netrc` con este contenido:

```
machine urs.earthdata.nasa.gov
    login <usuario de Earthdata>
    password <contraseña de Earthdata>
```

Se admite dejarlo en otra ruta y declararla en `dem.earthdata.ruta_netrc`, pero
**debe quedar fuera del repositorio**: la carpeta del proyecto se comprime y se
entrega como anexo, y ninguna regla de `git` alcanza a un `.zip`. Declarar una
ruta dentro del repositorio es un hallazgo bloqueante de la validación y
detiene la carga de la configuración, de modo que ningún módulo llega a
ejecutarse.

El token de Socrata (`ideam.socrata.token`) es gratuito y opcional. Sin él, la
API de datos.gov.co aplica un límite de peticiones más estricto.

### 2.5 Copiar los datos

```
robocopy <origen>\SIG_Referencia_Nacional C:\SIG_Referencia_Nacional /E /R:1 /W:1
robocopy <origen>\Hidrologia_Script\data\01_crudos C:\Hidrologia_Script\data\01_crudos /E /R:1 /W:1
```

**Los datos crudos se copian, no se vuelven a descargar.** La descarga del
IDEAM no es idempotente: un registro hoy `Preliminar` puede ser `Definitivo` en
la próxima consulta, y el catálogo de estaciones cambia. Quien vuelva a
descargar por su cuenta obtendrá series distintas de las del estudio en curso,
y los descartes ya registrados dejarán de corresponder a los datos.

El resto de directorios de `data/` se regenera ejecutando los módulos. Copiar
`data/03_SIG` ahorra volver a descargar el DEM, que son 456 MB.

---

## 3. Verificación

Tres comprobaciones, en este orden. Si alguna falla, no seguir adelante.

```
.venv\Scripts\python.exe src\M00_configuracion.py
.venv\Scripts\python.exe src\M00c_insumos.py
```

Y la suite de pruebas completa:

```
Get-ChildItem tests\test_*.py | ForEach-Object { .venv\Scripts\python.exe $_.FullName }
```

Las 17 suites deben terminar en `OK`. Qué significa cada fallo típico:

| Síntoma | Causa |
|---|---|
| M00c reporta bloqueante en `suelos.usa_capa_base` | `referencia_nacional.directorio` no apunta a donde quedaron las capas |
| `test_raster` y `test_m10` saltan pruebas | falta `data/03_SIG/raster/dem_recortado.tif` |
| La carga se detiene nombrando una clave | `config.local.yaml` intenta tocar doctrina, o tiene una clave mal escrita |
| `ImportError` de numpy o scipy | se está usando el Python del sistema y no el del `venv` |

---

## 4. Un estudio nuevo

Este repositorio es la **herramienta**: código, pruebas, doctrina técnica de
`data/referencia/` y plantillas. Un **estudio** es un directorio aparte con su
`config/config.yaml`, sus datos y sus productos. La misma instalación corre así
varios proyectos sin que los resultados de uno aparezcan en el otro.

```
py -3.12 C:\Hidrologia_Script\nuevo_estudio.py
```

Pregunta lo imprescindible, lo valida antes de escribir nada y crea el árbol
del estudio con su configuración. Las dos validaciones que importan: que el
código EPSG exista, y que el punto caiga dentro de Colombia una vez
reproyectado. La segunda atrapa el error más común al declarar una coordenada,
que es escribir la latitud antes que la longitud.

La configuración del estudio se deriva de la de la herramienta con los
comentarios intactos. Todo lo que no sea propio del proyecto se hereda tal cual,
de modo que dos estudios de la misma versión parten de la misma doctrina y las
diferencias entre sus resultados son atribuibles a la cuenca.

### Cómo se resuelven las rutas

| Qué | Dónde se busca |
|---|---|
| `data/00_insumos_usuario/`, `data/01_crudos/` … `data/05_resultados/`, `logs/` | siempre en el estudio |
| `data/referencia/`, `templates/`, `docs/`, `config/` | primero en el estudio, si no está en la herramienta |
| `config/config.yaml` | **siempre** en el estudio, nunca cae a la herramienta |

Ese descenso es lo que permite mantener la doctrina en un solo sitio y, a la
vez, que un estudio que necesite apartarse de ella ponga su propia copia en la
misma ruta relativa. Cuando lo haga, debe declararlo en el informe: cambiar una
tabla de doctrina es una decisión con margen.

### Ejecutar contra un estudio

Basta situarse dentro de él:

```
cd D:\Estudios\mi_proyecto
C:\Hidrologia_Script\.venv\Scripts\python.exe C:\Hidrologia_Script\src\M00_configuracion.py
```

O declararlo de forma explícita, que es lo preferible en un script:

```
C:\Hidrologia_Script\.venv\Scripts\python.exe C:\Hidrologia_Script\src\M10_morfometria.py --raiz D:\Estudios\mi_proyecto
```

Un directorio es un estudio si contiene `config/config.yaml`. La variable
`HIDROLOGIA_RAIZ` lo fija de forma explícita cuando ninguna de las dos formas
anteriores conviene.

---

## 5. Cómo se ejecuta

### La cadena completa

```
.venv\Scripts\python.exe ejecutar_cadena.py --raiz D:\Estudios\mi_proyecto
```

Corre los módulos en orden, cada uno con su intérprete. El orden, los
argumentos y el entorno de cada paso se declaran en
[config/cadena.yaml](config/cadena.yaml), no en el programa: cambiar la cadena
no debería exigir tocar código.

Conviene ver antes qué haría, sin ejecutar nada:

```
.venv\Scripts\python.exe ejecutar_cadena.py --raiz D:\Estudios\mi_proyecto --simular
```

Para un tramo, o para módulos sueltos:

```
.venv\Scripts\python.exe ejecutar_cadena.py --raiz ... --desde M05 --hasta M08
.venv\Scripts\python.exe ejecutar_cadena.py --raiz ... --solo M10
```

La cadena se detiene en tres sitios, y en los tres dice por qué:

| Se detiene en | Motivo |
|---|---|
| Un hallazgo bloqueante | El módulo declara su producto inutilizable. Seguir sería construir sobre él |
| El paso manual | La delimitación asistida de HEC-HMS, el único paso con intervención obligatoria |
| Un módulo pendiente | La cadena llega hasta donde llega la herramienta hoy |

El orquestador no añade capacidad: los módulos siguen siendo ejecutables
independientes y se pueden lanzar uno a uno. Lo que quita es la posibilidad de
equivocarse de intérprete, que produce un `ImportError` que no explica nada.

### Un módulo suelto

Un módulo, un script independiente. Se invocan por separado y se comunican por
archivos, nunca por estado en memoria.

Los módulos de análisis, con el `venv`:

```
.venv\Scripts\python.exe src\M10_morfometria.py
```

Los módulos SIG, con el Python de QGIS (desde OSGeo4W Shell, o con la ruta
declarada en `entornos.qgis.python`):

```
"C:\Program Files\QGIS 4.2.0\bin\python-qgis.bat" src\M06_isoyetas.py
```

Cada módulo escribe su log en `logs/`, con las versiones de las librerías, los
parámetros usados y la fecha. Los códigos de salida son comunes:

| Código | Significado |
|---|---|
| 0 | Producido sin hallazgos bloqueantes |
| 1 | Hay hallazgos bloqueantes; el producto no es utilizable |
| 2 | Solo el M00: hay advertencias y se pidió modo estricto |
| 3 | No se pudo leer la configuración o los insumos |

Un módulo se detiene y reporta; nunca produce un resultado incorrecto en
silencio.

### Único paso manual

La delimitación asistida en HEC-HMS. El M09 prepara los insumos con
`--preparar`, el consultor trabaja en HEC-HMS, y el M09 recoge el resultado con
`--importar`. Todo lo demás se ejecuta sin abrir software.

---

## 6. Estructura

```
config/         config.yaml compartido y la plantilla de configuración local
src/            un archivo por módulo, más comun/ y las librerías compartidas
src/comun/      configuración, rutas, logging, adaptadores de formato
data/referencia/ doctrina técnica: tablas y coeficientes, nunca en el código
data/00_insumos_usuario/  lo que aporta el consultor, declarado en MANIFIESTO.yaml
data/01_crudos/ descargas sin tocar
data/02_procesado/ productos intermedios
data/03_SIG/    capas y rásteres
data/05_resultados/ figuras y tablas del informe
docs/referencia/ informe modelo que define la estructura de capítulos
legacy/         rutinas heredadas de R.LTWB, conservadas como referencia
tests/          pruebas, ejecutables una a una sin dependencias externas
logs/           un archivo por ejecución
```

Qué se versiona y qué no: código, configuración, doctrina técnica, plantillas y
figuras. No se versiona lo que se puede volver a descargar, lo que se regenera
al ejecutar un módulo, ni lo que pesa tanto que haría inmanejable el
repositorio. El criterio completo está comentado en [.gitignore](.gitignore).

---

## 7. Al trabajar en equipo

- **El código y la configuración compartida viajan por Git.** Nunca por carpeta
  sincronizada: Git escribe cientos de archivos pequeños con renombrados
  atómicos, y una sincronización a media escritura deja el historial
  inconsistente.
- **Los datos y las capas nacionales se copian una vez** y se quedan quietos en
  cada máquina. No se sincronizan de forma continua.
- **`config/config.local.yaml` no se comparte.** Es de cada máquina.
- Antes de confirmar un cambio en `config/config.yaml`, tener presente que
  afecta a todo el equipo y al resultado del estudio. Los cambios de instalación
  no van ahí.
