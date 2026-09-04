# Traspaso a otra máquina y a otro usuario

Qué hay que mover, en qué orden y qué comprobar antes de dar por buena la
instalación. El `README.md` explica cada pieza con detalle; esto es la
secuencia.

---

## 0. El flujo, en una mirada

Tres cosas distintas se mueven a la máquina nueva, por dos caminos distintos,
y una de ellas puede no ser necesaria. Confundir estas tres capas es lo que
vuelve confuso el traspaso.

| Capa | Qué es | Tamaño | Camino | ¿Se necesita para un estudio nuevo? |
|---|---|---|---|---|
| **1. La herramienta** | Código, doctrina de `data/referencia/`, plantillas de informe y planchas | 120 MB | `git push` / `git clone` | Sí, siempre |
| **2. El insumo nacional** | Capas del IGAC y del IDEAM que usa cualquier estudio, en `SIG_Referencia_Nacional` | 3,8 GB | Copia directa (USB, disco externo, red) | Sí, siempre. No cambia entre estudios: se copia una sola vez por máquina |
| **3. El estudio** | Descargas del IDEAM y resultados de un proyecto concreto, como Refugio del Valle | Variable | Copia directa, y solo si se quiere | **No.** Un estudio nuevo se genera vacío en la máquina nueva y descarga sus propios datos. Solo se copia si además se quiere llevar Refugio del Valle como referencia |

En orden:

1. **Capa 1**, desde esta máquina: `git push` al repositorio privado. En la
   máquina nueva, `git clone` crea el árbol completo de la herramienta.
2. **Capa 2**, copia directa (no por `git`): llevar `SIG_Referencia_Nacional`
   a `C:\SIG_Referencia_Nacional` en la máquina nueva. Una sola vez; sirve
   para todos los estudios que se hagan allí después.
3. **Capa 3**, copia directa y **opcional**: solo si además de trabajar en el
   estudio nuevo se quiere tener Refugio del Valle como referencia en la
   máquina nueva, llevar esa carpeta completa a `C:\Estudios\refugio_del_valle`.
   Un estudio nuevo no depende de esto.

La credencial de Earthdata (sección 3.4) no es una capa que se mueva: cada
usuario crea la suya en la máquina nueva, nunca se copia la de otro.

Para un estudio **nuevo**, lo estrictamente necesario es la Capa 1 (por git)
y la Capa 2 (copiada aparte). El resto de este documento detalla cada paso.

---

## 1. Antes de empezar: qué se mueve y qué no

El repositorio pesa **120 MB** y lleva el código, la doctrina técnica de
`data/referencia/` y las dos plantillas: la del informe y la de las planchas.
Eso es lo que viaja por `git`.

**Lo que NO viaja y hay que copiar aparte:**

| Qué | Tamaño | De dónde a dónde |
|---|---|---|
| Capas nacionales del IGAC y del IDEAM | 3,8 GB | `C:\SIG_Referencia_Nacional` |
| Descargas crudas del IDEAM | 2,9 GB | `<repo>\data\01_crudos` |
| Estudios ya ejecutados | variable | `C:\Estudios\<nombre>` |

**Los datos crudos se copian, no se vuelven a descargar.** La consulta al IDEAM
no es idempotente: un registro hoy `Preliminar` puede ser `Definitivo` en la
próxima consulta, y el Catálogo Nacional de Estaciones cambia. Quien vuelva a
descargar obtendrá series distintas de las del estudio en curso, y los
descartes ya registrados dejarán de corresponder a los datos que los
justificaron.

---

## 2. El repositorio remoto

**Ya está publicado**, en un repositorio privado de GitHub:

```
https://github.com/jorgegonzalezingcivil/Hidrologia_Script.git
```

Privado, no público: la doctrina técnica transcrita de las tablas del
consultor y las plantillas de informe son trabajo propio.

**ANTES DE TRASPASAR, COMPROBAR QUE LO LOCAL ESTÉ ENVIADO.** Es el error que
deja al nuevo usuario trabajando sobre una versión vieja sin saberlo, porque el
`git clone` funciona igual y no avisa de nada.

**Primero `git fetch`, y después mirar.** No al revés:

```bash
git fetch origin
git status -sb
```

`git status` **no consulta al servidor**: compara contra `origin/main`, que es
una copia local de dónde estaba el remoto la última vez que se habló con él.
Sin refrescarla puede mentir en las dos direcciones. Pasó aquí, y en la
dirección que menos se espera: la referencia local se había quedado 21 commits
atrás y el estado anunciaba `[ahead 21]` sobre un trabajo que **ya estaba
publicado**. El `git push` respondió `Everything up-to-date` y el susto fue
gratis; al revés, con la referencia adelantada respecto de lo enviado, el
estado habría dicho que todo estaba al día y el traspaso se habría dado por
bueno con trabajo sin subir.

Tras el `fetch`, la primera línea dice `## main...origin/main` y, si hay
trabajo sin enviar, añade `[ahead N]`. Con `N` distinto de cero:

```bash
git log origin/main..main --oneline
git push
```

Y la comprobación que no depende de ninguna referencia local, porque pregunta
al servidor: las dos órdenes deben devolver el mismo identificador.

```bash
git ls-remote origin main
git rev-parse main
```

Si el servidor rechazara el envío por tamaño, el culpable serían las capas de
`data/referencia/sig/`, que suman unos 30 MB. GitHub avisa por encima de 50 MB
**por archivo**, y aquí el mayor es `CNE_IDEAM_Final.dbf`, de 15 MB.

---

## 3. En la máquina nueva

### 3.1 Lo que hay que tener instalado

- **QGIS 4.2.0** o la versión que se declare. El estudio se ejecutó con 4.2.0,
  que no es LTR; esa desviación respecto de `CLAUDE.md` está declarada en
  `config.yaml` con su motivo.
- **HEC-HMS 4.13**
- **Python 3.12** para el entorno de análisis
- **Git**

### 3.2 Traer el repositorio

```bash
git clone https://github.com/jorgegonzalezingcivil/Hidrologia_Script.git C:\Hidrologia_Script
cd C:\Hidrologia_Script
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe setup_estructura.py
```

**El `setup_estructura.py` no es opcional.** Git no versiona directorios
vacíos, de modo que un clon recién hecho no trae las carpetas de productos que
la configuración declara. Sin ese paso, la propia suite de verificación falla
con `data/02_procesado/enso` ausente. Comprobado sobre un clon limpio del
remoto: 40 carpetas ya presentes y 1 creada, y con ella la suite pasa entera.

Y comprobar que lo clonado es lo último, preguntándole al servidor en lugar de
fiarse de la copia local de la referencia:

```bash
git fetch origin
git ls-remote origin main
git rev-parse main
```

Los dos identificadores deben coincidir. Si no, el clon quedó de una versión
anterior y hay que traer lo que falta antes de ejecutar nada.

### 3.3 Declarar lo propio de esa máquina

`config/config.yaml` **no se edita** para acomodar una instalación. Es doctrina
del estudio y es igual para todo el equipo.

```bash
copy config\config.local.ejemplo.yaml config\config.local.yaml
```

Abrir la copia y ajustar dónde están QGIS, HEC-HMS y las capas nacionales. Ese
archivo no se versiona.

**El archivo local solo puede sobrescribir claves de máquina.** Si el nuevo
usuario intenta cambiar desde ahí un periodo de retorno, un umbral o un método,
la carga se detiene con un mensaje explícito. La restricción es deliberada: un
parámetro técnico distinto en cada equipo produciría resultados distintos del
mismo estudio sin dejar rastro. Todo lo que se sustituya queda escrito en el log
de cada módulo, con el valor compartido y el propio uno al lado del otro.

### 3.4 Las credenciales son de cada quien

El nuevo usuario crea **su propia** cuenta de Earthdata y su propio archivo
`C:\Users\<su usuario>\.netrc`:

```
machine urs.earthdata.nasa.gov
    login <su usuario de Earthdata>
    password <su contraseña>
```

**Las credenciales nunca van a un archivo del repositorio.** La carpeta del
estudio se comprime y se entrega como anexo, y ninguna regla de `git` alcanza a
un `.zip`. Declarar una ruta de `netrc` dentro del repositorio es un hallazgo
bloqueante que detiene la carga de la configuración.

El token de Socrata es gratuito y opcional; sin él la API aplica un límite de
peticiones más estricto.

### 3.5 Copiar los datos

```bash
robocopy <origen>\SIG_Referencia_Nacional C:\SIG_Referencia_Nacional ^
    /E /R:1 /W:1

robocopy <origen>\Hidrologia_Script\data\01_crudos ^
    C:\Hidrologia_Script\data\01_crudos /E /R:1 /W:1
```

---

## 4. Comprobar antes de trabajar

En este orden. Si algo falla, no seguir: cada paso supone el anterior.

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Deben pasar las **1.463** pruebas. Es la comprobación más completa y no toca
ningún dato.

**En una máquina recién instalada se saltan unas 48**, y eso es lo correcto:
son las que necesitan QGIS, GRASS, el DEM recortado o una capa de cuenca, es
decir, datos que todavía no están ahí. Sobre una máquina con el estudio
cargado bajan a 23. Un número de saltadas mucho mayor apunta a que el
intérprete de QGIS no responde o a que las capas nacionales no se copiaron.

```bash
.venv\Scripts\python.exe src\M00_configuracion.py
```

Verifica que la configuración carga, que las rutas declaradas existen y que la
superposición local solo toca claves de máquina.

```bash
set QGIS_PY="C:\Program Files\QGIS 4.2.0\bin\python-qgis.bat"
%QGIS_PY% -c "import qgis.core; print(qgis.core.Qgis.QGIS_VERSION)"
```

Confirma que el intérprete de QGIS responde. Es el que ejecuta M01, M02, M06,
M08, M11 y M16.

---

## 5. Un estudio nuevo

El estudio **no vive dentro del repositorio**. La herramienta y el estudio son
cosas distintas, y esa separación es lo que permite que un mismo código sirva a
varios contratos.

```bash
py -3.12 C:\Hidrologia_Script\nuevo_estudio.py
```

Pregunta lo imprescindible, lo valida antes de escribir nada y crea el árbol del
estudio con su configuración. Las dos validaciones que importan: que el código
EPSG exista, y que el punto caiga dentro de Colombia una vez reproyectado. La
segunda atrapa el error más común al declarar una coordenada, que es escribir la
latitud antes que la longitud.

A partir de ahí, todos los módulos se ejecutan con `--raiz C:\Estudios\<nombre>`,
o situándose dentro del estudio.

Lo que el consultor debe poner en `data/00_insumos_usuario/` antes de empezar:

- **`logos/`** con el logo del contratante y el del consultor. Los nombres se
  declaran en `planchas.logos` del `config.yaml` del estudio.
- **`suelos/`** y su tabla de homologación
- **`cobertura/`** si se aporta una propia; si no, se usa Corine
- **`caudales/`** si existen series para calibrar
- **`topografia/`** los planos del sitio, que son el Anexo 8

El **único paso manual obligatorio** de toda la cadena es la delimitación
asistida en HEC-HMS. Va **antes del análisis de precipitación**, entre el M02c y
el M03: la cadena se detiene en el M09a con las instrucciones, el ingeniero
delimita en HEC-HMS, y al relanzar desde el M09b la rutina deriva de esas
subcuencas el área de influencia definitiva y sigue sola.

Ese orden es deliberado. La precipitación (descarga del IDEAM, análisis de
frecuencia, isoyetas) es la parte cara de la cadena, y corriéndola después de
la delimitación se ejecuta sobre la cuenca real y no sobre un área estimada que
la sobredimensiona varias veces. Todo lo demás se ejecuta sin abrir software.

---

## 5b. Un estudio de antes

La configuración de un estudio **no se fusiona** con la de la herramienta: es
doctrina congelada del proyecto, y por eso dos estudios de la misma versión
parten de lo mismo. El precio es que, cuando la herramienta añade o mueve una
clave, un estudio anterior se detiene con `clave ausente`.

Antes de volver sobre un estudio hecho con una versión anterior:

```bash
py -3.12 migrar_estudio.py --raiz C:\Estudios\<nombre> --simular
```

Muestra qué haría sin escribir nada. Si convence, se repite sin `--simular`.

Aplica las recetas de `config/migraciones.yaml`, que declaran clave por clave
qué cambió y por qué. Copia las claves nuevas **con sus comentarios**, ajusta
las que cambiaron de significado y renombra en disco los productos que
cambiaron de nombre, porque una cosa sin la otra deja la configuración
apuntando a un archivo que no existe.

**No pisa un valor que el consultor haya cambiado a mano.** Si encuentra algo
distinto de lo que la receta esperaba, lo deja intacto y lo reporta: una
diferencia así es una decisión del estudio, y sustituirla en silencio la
borraría. Deja además una copia del archivo anterior.

---

## 6. Lo que el ingeniero tendrá que ajustar a mano

Conviene que el nuevo usuario lo sepa de entrada, para que no lo lea como un
fallo:

**La colocación de la leyenda y la rosa náutica en las planchas.** El M16 deja
las 29 correctas en contenido, encuadre y escala, pero dónde cae la caja de
convenciones dentro de cada lienzo depende de la forma de la cuenca y es
criterio visual. Se ajusta abriendo `templates/planchas.qgz` en QGIS, moviendo
las dos cajas y guardando. **Lo que se guarde ahí queda fijo para todos los
estudios siguientes**, así que se hace una vez.

Para retocar los mapas de UN estudio sin tocar la plantilla, el paquete de
entrega lleva el **anexo 10, «Proyecto SIG editable»**: los dos proyectos de
QGIS con sus capas, conservando el árbol `data/03_SIG/...` porque los
proyectos las referencian por ruta relativa y aplanarlas los dejaría sin
ninguna. La composición de planchas resuelve sus 51 capas dentro del paquete;
el proyecto de trabajo resuelve 9 de 10, y la que falta, la zonificación
hidrográfica nacional, va en `capas_nacionales/` y se relaciona a mano.

**La redacción del informe.** El M15 resuelve lo mecánico: en el estudio de
referencia, 254 figuras y 30 tablas. Las instrucciones de redacción se
resuelven con el texto declarado en `config/analisis.yaml` **del estudio**, que
es donde vive porque habla de los números de ese proyecto. Un estudio nuevo
empieza sin ese archivo y las 73 instrucciones quedan en verde hasta que se
redacten.

Lo que no se automatiza y hay que revisar en cada estudio:

- Los párrafos que la cadena redacta pero a los que les falta un dato externo.
  Entran **resaltados en rosa** y el módulo los enumera en su reporte.
- Los párrafos de la modelación hidráulica y del análisis de socavación, que la
  plantilla trae del informe de referencia y esta cadena no produce. También en
  rosa, y no se borran porque sirven de modelo de redacción.
- Abrir el documento en Word y aceptar la actualización de campos.

**La calibración del modelo (M14b).** Está declarada como `no_viable`, que **no
es lo mismo que pendiente**: no le falta a la herramienta, no se puede hacer
con los datos de este estudio: las estaciones LG y LM del área registran nivel, no caudal.
Si el estudio nuevo tiene series de caudal utilizables, hay que programarla. Lo
que sí corre es el **M14c**, que contrasta el modelo contra los caudales
observados sin ajustar ningún parámetro: es una verificación, no una
calibración, y el informe debe declarar cuál de las dos ocurrió.

---

## 6b. El diagrama de la rutina

Antes de tocar nada, conviene mirar
[docs/diagrama_cadena.pdf](docs/diagrama_cadena.pdf): las diez etapas, los 35
pasos, con qué intérprete corre cada uno, dónde está el único paso manual y qué
herramientas corren fuera de la cadena. Se regenera con:

```bash
.venv\Scripts\python.exe tools\diagrama_cadena.py
```

Se dibuja leyendo `config/cadena.yaml`, de modo que no puede desfasarse cuando
se añade o se cambia un módulo. Un diagrama que se dibuja aparte se desfasa a
la primera, y un diagrama desfasado es peor que ninguno: se cree.

---

## 6c. La descarga del IDEAM se hace una vez

Es la regla que más veces se malinterpreta al llegar a una máquina nueva.

**La cadena no descarga por omisión.** Trabaja con lo que hay en
`data/01_crudos`. Para el primer llenado de un estudio nuevo:

```bash
.venv\Scripts\python.exe ejecutar_cadena.py --raiz C:\Estudios\<nombre> --descargar
```

Después de esa vez, ninguna corrida vuelve a pedir nada. La razón es la de
siempre: la consulta **no es idempotente**, y repetirla cambiaría la serie bajo
un informe ya redactado.

Dos cosas que conviene saber para no leer mal el log:

- El M04 **salta** las estaciones cuyo `.zip` ya existe, sin consultar al
  servicio. Por eso reporta cuántas peticiones omitió: `0 archivos nuevos` no
  significa que el IDEAM no tenga nada nuevo, significa que no se le preguntó.
- Las combinaciones de estación y serie que el servicio responde sin datos
  quedan anotadas en `sin_datos.csv`, junto a los `.zip`, con la fecha. Sin ese
  registro, cada corrida volvía a preguntar por ellas: son 158 en el estudio de
  referencia y costaban media hora por pasada.

Para traerlo todo otra vez, ignorando ese registro, `M04 --redescargar`.

---

## 6d. Cerrar la entrega

```bash
.venv\Scripts\python.exe tools\verificar_informe.py
.venv\Scripts\python.exe tools\empaquetar_entrega.py --raiz C:\Estudios\<nombre>
```

El primero contrasta las cifras que el texto del informe cita contra los
productos de la cadena. Hace falta porque los análisis citan números concretos:
si la cadena se vuelve a correr y alguno cambia, el texto seguiría diciendo el
viejo y nada lo advertiría.

El segundo comprueba que el entregable esté completo (informe, acta de entrega
con la huella de cada anexo, ningún hallazgo bloqueante del M15 ni del M17) y
arma el comprimido con un `LEEME.md` que lleva el commit de la herramienta que
lo produjo.

**La tabla de cifras que verifica el primero es del estudio de referencia.**
Al abrir un estudio nuevo hay que revisarla: `tools/verificar_informe.py`
declara la ruta del estudio y las cifras esperadas al principio del archivo.

---

## 7. Trabajar en paralelo sin pisarse

Si dos personas van a tocar el repositorio, cada una en su rama y con revisión
antes de fusionar. Lo que **nunca** debe divergir sin acuerdo explícito:

- `config/config.yaml`, que es doctrina compartida
- `data/referencia/`, que son las tablas técnicas
- `templates/`, que son las dos plantillas

Un cambio ahí afecta a todos los estudios, pasados y futuros. Un cambio en
`config.local.yaml` no afecta a nadie más, y por eso es el único sitio donde
cada quien decide solo.
