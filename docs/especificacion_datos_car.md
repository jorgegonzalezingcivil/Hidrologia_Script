# Especificación: incorporación de los datos de la CAR

Qué se ingiere de la información entregada por la Corporación Autónoma Regional
de Cundinamarca, qué se descarta, con qué criterio, y qué debe declarar el
informe. Se redacta **antes de programar** para que el descarte quede decidido
sobre evidencia y no sobre lo que resulte cómodo al escribir el adaptador.

Todas las cifras de este documento están medidas sobre los archivos entregados.

---

## 1. Las dos fuentes entregadas

| | `ae (69).xlsx` | `SERIES HISTORICAS SATELITALES_Completas.rar` |
|---|---|---|
| Escala | **Mensual** | Diaria |
| Periodo | **1932 a 2026** | 2018 a 2024, **falta 2020** |
| Estaciones | 33 | 113 (caudal 2018), 98 (precipitación 2024) |
| Formato | Largo, una fila por dato, 8 columnas | Matricial, una hoja por estación |
| Origen | Estaciones en tierra | **Mezcla tierra y satélite** |
| Volumen | 76.248 filas | 119 archivos |

**No son la misma información ni las mismas estaciones.** Comparten 13
estaciones entre el Excel y el caudal de 2018 del RAR, y **ninguna** entre el
Excel y la precipitación satelital de 2024.

### Se adopta el Excel como fuente

Tres razones, en orden de peso:

1. **Longitud de serie.** De 15 a 85 años por estación, contra 6 años del RAR.
   El análisis de frecuencia de un estudio de crecientes no se sostiene sobre
   seis años.
2. **Origen de la medición.** El Excel es íntegramente de estaciones en tierra.
   En el RAR, la precipitación es `CONV` y `SAT` en 2018 y 2019, y **solo `SAT`
   desde 2021**.
3. **Pertinencia al área.** Las 33 estaciones del Excel están todas en el
   catálogo de la CAR y concentradas sobre esta cuenca: 13 a menos de 9 km y
   cuatro **sobre** ella. Es una exportación dirigida, no la red completa.

---

## 2. Qué se ingiere

Solo lo que algún módulo de la cadena consume.

| Parámetro | Tipo | Filas | Estaciones | Destino |
|---|---|---|---|---|
| PRECIPITACIÓN | TOTALES | 6.285 | | M05, M06 |
| PRECIPITACIÓN | MAXIMA EN 24 HORAS | 5.168 | 10 | M07 |
| CAUDALES | MEDIOS | 7.374 | 23 | M18, M19 |
| CAUDALES | MINIMOS MEDIOS | 7.374 | 23 | M19 (estiaje) |
| CAUDALES | MAXIMOS ABSOLUTOS | 7.273 | 23 | verificación de crecientes |
| TEMPERATURA AMBIENTE | MEDIOS | 7.473 | | M18a |
| EVAPORACIÓN | TOTALES | 1.599 | | M18a |

## 3. Qué se descarta, y por qué

| Se descarta | Motivo |
|---|---|
| **Todo el archivo `.rar`** como fuente de series | Seis años frente a hasta 85 del Excel, y sin caudal después de 2019. Se conserva para **contrastes puntuales**: su caudal medio diario de 2018 fue lo que permitió resolver qué significa `MAXIMOS ABSOLUTOS` (sección 7) |
| **Precipitación satelital** | Valor derivado de píxel, no medida de pluviómetro. Sesgo y soporte espacial distintos; mezclarla con pluviómetros en el mismo IDW no es defendible |
| **NIVELES** (22.063 filas) | Sin curva de gasto no son caudal. Es la misma limitación que ya bloqueaba el uso de las LG y LM del IDEAM |
| Brillo solar, humedad relativa, punto de rocío, radiación solar, tensión de vapor, viento, temperatura del suelo | Ningún módulo de la cadena los consume (7.970 filas en total) |
| Categorías `H` y `HM` del catálogo CAR | Existen solo como satelitales (16 y 34 estaciones); se descartan con el resto de lo satelital |

**Nada de esto se borra.** Los archivos permanecen en
`data/00_insumos_usuario/datos CAR/` y el informe declara por qué no se usaron.
Un descarte que no se puede explicar no es defendible ante interventoría
(CLAUDE.md, sección 7).

---

## 4. Las estaciones y su mezcla con el IDEAM

### Se integran en un solo conjunto

Verificado sobre los dos catálogos:

- `CNE_CAR.shp` trae **434 estaciones**, las mismas que el catálogo incrustado
  en los archivos del RAR.
- **Cero códigos comunes** entre las 434 de la CAR y las 4.521 del IDEAM.
- **Cero duplicados espaciales**: ninguna pareja CAR/IDEAM a menos de 300 m
  dentro de los 15 km de la cuenca.

De modo que integrarlas no puede pesar dos veces la misma estación, que era el
riesgo que había que descartar antes de mezclarlas.

### Cada estación conserva su operador

El campo de entidad operadora viaja con la estación hasta el informe. Sin él no
se puede responder de qué red salió cada dato, y el M05 no podría separar una
inconsistencia entre redes de una inconsistencia de una estación.

### Las categorías coinciden, pero no en el campo que parece

Verificado sobre `CNE_CAR.shp`, que trae **tres** campos de categoría:

| Campo | Contenido | Sirve |
|---|---|---|
| `CATEGORIA_` | código numérico interno (35, 43, 44...) | no |
| `CATEGORI_1` | nombre largo ("PLUVIOMETRICAS") | no |
| **`CATEGORI_2`** | **código corto: `PM`, `PG`, `CP`, `CO`, `LM`, `LG`** | **sí** |

`CATEGORI_2` usa la nomenclatura del IDEAM, de modo que **no hace falta tabla de
homologación**. Es el campo a mapear, y no el nombre largo.

**Lo satelital se filtra por `TIPO_NOMBR`, no por la categoría.** El catálogo
distingue Convencional (315), Satelital (100) y Automática (19). Hay categorías
que ya delatan el origen en su propio código (`CPS` es climatológica principal
SATELITAL, 54 estaciones), pero fiarse de eso sería frágil: el campo que lo
declara es `TIPO_NOMBR` y es el que decide.

Si se mapeara `CPS` a `CP` sin mirar el tipo, **entrarían 54 estaciones
satelitales como si fueran climatológicas convencionales**, y acabarían
sosteniendo la interpolación de lluvia junto a los pluviómetros. Es exactamente
lo que la sección 3 descarta.

### El estado no se puede usar

`ESTADO` vale `'0'` en las 434 estaciones. No distingue activa de suspendida,
que es lo que la precedencia del M03 necesita. Las de la CAR entran sin estado
declarado, y el descarte por antigüedad lo decide el M04b sobre el dato, que es
donde `CLAUDE.md` lo sitúa.

### Lo que la mezcla aporta a este estudio

A 9 km de la cuenca, que es el radio que el M03 adopta hoy por cobertura, la
CAR aporta **7 estaciones convencionales de precipitación** (3 PM, 2 PG, 2 CP)
sobre las 34 del IDEAM. Cuatro estaciones caen **sobre** la cuenca:

| Código | Nombre | Años | Trae |
|---|---|---|---|
| 2120112 | CASITA LA | 51 | precipitación |
| 2120103 | SANTA TERESA | 54 | precipitación |
| 2120872 | PUENTE LA CALERA | 29 | **caudal** |
| 2120989 | SIMAYA | 15 | **caudal** |

---

## 5. La escala mensual: qué permite y qué no

### El máximo anual se deriva sin pérdida

El M07 necesita el máximo **anual** de la precipitación diaria. El Excel entrega
el máximo **mensual** en 24 horas. El mayor de los doce máximos mensuales **es**
el máximo anual: el mayor de los mayores es el mayor. La reducción es exacta y
no introduce aproximación.

### Desviación declarada respecto de CLAUDE.md

La sección 6 de `CLAUDE.md` establece que la precipitación diaria *"se conserva
íntegra para Pmáx24h"*. **Para las estaciones de la CAR no existe serie diaria**:
solo el máximo mensual. Consecuencias, que deben quedar escritas:

- No se puede detectar el equivalente al `ACUMULADO` del IDEAM, es decir, un
  falso máximo producido por varios días sumados en una sola lectura.
- No se puede verificar el hietograma de Huff contra el dato de esas estaciones.
- El análisis de datos anómalos del M05 opera sobre la serie mensual, que es
  justamente donde `CLAUDE.md` lo sitúa, de modo que ahí no hay pérdida.

### El caudal mensual no permite calibrar eventos

HEC-HMS simula crecientes con paso horario. Un caudal medio mensual no puede
calibrar un hidrograma de creciente. **El M14b queda declarado como no viable
con los datos disponibles**, y el informe lo explica en lugar de dejar el
capítulo vacío.

Lo que sí permite el caudal mensual: el balance hídrico del M18 y la curva de
duración, el IRH y el caudal ambiental del M19, que trabajan a esa escala.

---

## 6. Dónde ocurre el filtrado

**No se filtra al ingerir.** Las 33 estaciones entran completas. El descarte
ocurre donde la cadena ya lo tiene programado y medido:

| Filtro | Módulo | Criterio |
|---|---|---|
| Longitud de serie | M04b | Matriz de sensibilidad por umbral y ventana; decide el consultor |
| Datos anómalos | M05 | Sobre la serie mensual |
| Consistencia | M05 | Dobles masas, ahora también **entre redes** |
| Completitud para interpolar | M06, M08 | Solo totales que cubren los doce meses |

El motivo es doctrina explícita del propio M03: *"quitar estaciones antes de ver
los datos es decidir sin evidencia"*. Un filtro por longitud aplicado en el
adaptador dejaría fuera estaciones sin que quede constancia de cuáles ni de por
qué.

**Atención al análisis de consistencia.** Con dos operadores en el mismo campo,
las dobles masas del M05 dejan de ser una formalidad: una inconsistencia
sistemática entre redes es un resultado posible y hay que mirarlo, no darlo por
descartado.

---

## 7. Verificación de crecientes

Sustituye a la calibración del M14b, que los datos no permiten.

### Qué se compara

El caudal pico que el modelo produce por periodo de retorno, contra el análisis
de frecuencia de los **máximos absolutos anuales observados** en las estaciones
que están dentro de la cuenca.

### Dónde se compara

Verificado sobre el modelo actual: **ya existen uniones prácticamente en las dos
estaciones**, de modo que no hace falta redelimitar.

| Estación | Años | Unión del modelo | Distancia |
|---|---|---|---|
| 2120872 PUENTE LA CALERA | 29 | **J24** | 127 m |
| 2120989 SIMAYA | 15 | **J29** | 159 m |

Se exige coincidencia en **las dos a la vez**. Casar en un solo punto se
consigue moviendo un parámetro global; casar en dos puntos con áreas distintas
restringe mucho más y hace la verificación creíble.

### Qué fracción de la cuenca queda verificada

Medido sobre la topología del modelo:

| Punto | Área que controla | Fracción |
|---|---|---|
| J24 PUENTE LA CALERA | 81,31 km² | 36,9 % |
| J29 SIMAYA | 64,35 km² | 29,2 % |
| **Juntas** | **145,66 km²** | **66,1 %** |

**Las dos no están anidadas**: ninguna cae aguas arriba de la otra, de modo que
son dos afluentes independientes y sus áreas son disjuntas. Cubren 78 de las
125 subcuencas. Dos ramas separadas restringen mucho más que dos puntos sobre
el mismo cauce, porque un cambio global que arregle una tiende a estropear la
otra salvo que sea correcto.

### El tercio no aforado no es igual al aforado

| | Aforada (78 subcuencas) | No aforada (47) |
|---|---|---|
| Pendiente de cuenca | 0,22 | 0,20 |
| Área, mediana | 1,35 km² | 1,26 km² |
| **Cota media** | **3.010 m** | **2.757 m** |
| Longitud de cauces | 2,04 km | 2,71 km |

Pendiente y tamaño son comparables. La parte no aforada está **250 m más abajo**
y con cauces más largos: es la porción baja, hacia el cierre.

### Tres reglas para transferir lo ajustado

1. **Los parámetros fisiográficos se transfieren y se declara que se
   transfirieron.** El número de curva y el rezago dependen de suelo, cobertura
   y geometría, y en pendiente y tamaño las dos poblaciones son comparables.

   **No se escalan por área.** Un multiplicador global no es una corrección
   proporcional: es afirmar que el sesgo es uniforme en toda la cuenca, y con
   250 m de diferencia de cota esa afirmación no está sostenida. El número de
   curva es una propiedad física del suelo, no una función del tamaño; y el
   rezago ya escala por sí solo, porque sale del Tc que el M10 calcula con la
   geometría propia de cada subcuenca.

2. **Lo que depende de la cota no se transfiere sin comprobarlo.** La
   precipitación de entrada se distribuye por el gradiente altitudinal del M11.
   Hay que verificar que la zonificación cubre el rango completo y no extrapola
   sobre el tercio bajo.

3. **El caudal en el punto de cierre se reporta como NO verificado.** El modelo
   queda verificado sobre el 66 % aforado. La cifra del cierre es una
   extrapolación defendible, no un valor comprobado, y el informe lo dice con
   ese matiz.

### Verificación por transposición de cuencas

Cuando exista una estación de caudal **aguas abajo** cuya cuenca contenga la del
estudio, se usa como comprobación adicional. Es la única evidencia que puede
decir algo sobre la porción no aforada, y por eso **se busca siempre**, no solo
en este estudio.

En Refugio del Valle, la estación es **EL VERGEL** (2120878, 27 años):

| Punto | Tramos de red arriba | Red | Área |
|---|---|---|---|
| Cierre del estudio | 245 | 347,5 km | 220,31 km² (delimitada) |
| EL VERGEL | 401 | 454,9 km | **≈ 288 km² (estimada)** |

Su cuenca **contiene** la del estudio: el 76 % es el área del estudio y el 24 %
restante son unos 68 km² adicionales aguas abajo.

**El área de EL VERGEL está estimada, no delimitada.** Sale de la relación local
entre longitud de red y área del propio estudio (347,5 km para 220,31 km²), que
es preferible a la densidad genérica de 1,14 km/km² porque aquí esa densidad
sobreestima un 28 %. **Antes de usarla cuantitativamente hay que delimitarla
sobre el terreno**, que es una operación del M02 y es viable porque el modelo de
elevación ya cubre esa zona.

El contraste se hace transponiendo por área:

> Q(cierre) ≈ Q(El Vergel) · (A_cierre / A_vergel)^n

El exponente **n se declara**, no se da por supuesto. Para caudales pico suele
tomarse entre 0,6 y 1,0, y el valor adoptado con su fuente queda escrito en el
informe.

**Es corroboración, no verificación primaria.** Arrastra tres incertidumbres que
J24 y J29 no tienen: el área estimada, el exponente de transposición y los
68 km² que no pertenecen al estudio. Se reporta como tal.

### El criterio de aceptación

**No es un porcentaje fijo.** El caudal de Tr 100 estimado desde 29 años arrastra
un intervalo de confianza propio del orden de ±30 a 50%. Exigir ±5% contra una
cifra así de incierta obliga a ajustar el modelo contra el ruido de la muestra.

> **Se acepta si el pico modelado cae dentro de la banda de confianza del
> análisis de frecuencia de la serie observada.**

La banda sale de la misma maquinaria del M07, que ya ajusta varias
distribuciones y compara por criterios de información.

### Hasta qué periodo de retorno se verifica

Solo hasta donde la muestra sostiene la estimación. Con 29 años:

| Periodo de retorno | Estado |
|---|---|
| 2,33, 5, 10, 25 | Se verifica |
| 50 | Se verifica con reserva declarada |
| 100, 500 | **No se verifica.** Son extrapolación de la distribución ajustada, no observación. Contrastar el modelo contra ellos compara dos extrapolaciones |

### Si no coincide: la búsqueda de parámetros va acotada

Un modelo cuyos parámetros se mueven libremente reproduce cualquier cosa, y un
modelo que reproduce cualquier cosa no demuestra nada. Cada parámetro se mueve
dentro de su rango defendible y **el rango se declara antes de iterar**:

| Parámetro | Rango admisible | Fuente del rango |
|---|---|---|
| Número de curva | El que admite la homologación de suelos y cobertura | Tablas del consultor, M10 |
| Tiempo de rezago | Dentro de la dispersión de las fórmulas de Tc aplicables | Matriz del M10, que ya calcula mediana y dispersión |
| Tránsito | El menos libre: sale de la hidráulica del tramo | M14, Muskingum-Cunge |

Si el ajuste exigiera salir de esos rangos, **no se sale**: se reporta que el
modelo no se pudo verificar con parámetros defendibles, que es un resultado
legítimo y mucho más informativo que un ajuste forzado.

### La distinción que el informe debe hacer

| Situación | Qué es | Qué vale |
|---|---|---|
| El modelo coincide **sin tocar nada** | Verificación | Evidencia fuerte de que el modelo representa la cuenca |
| Hubo que ajustar hasta que coincidiera | **Calibración** | La coincidencia ya **no** es evidencia: es el resultado de haberla buscado |

Las dos son legítimas. Confundirlas no lo es. El informe debe decir cuál de las
dos ocurrió, y si hubo ajuste, qué parámetro se movió, cuánto y por qué.

### Productos

- Copia fechada del modelo **antes** de cualquier ajuste (el M13 ya la hace).
- Copia del modelo **validado**, si hubo ajuste.
- Resumen de cambios: parámetro, valor inicial, valor adoptado, rango admisible
  y justificación.
- Tabla y figura del contraste modelado contra observado, con la banda de
  confianza dibujada.

### Qué es exactamente el dato observado: resuelto y con consecuencia

Quedaba por confirmar si `CAUDALES / MAXIMOS ABSOLUTOS` era el caudal
instantáneo o el máximo de los medios diarios. **Se resolvió midiendo**, y la
respuesta obliga a cambiar la comparación.

La prueba: el `.rar` descartado como fuente sí trae **caudal medio diario** de
2018, de modo que para cada estación y mes se contrastó el máximo mensual del
Excel contra el máximo de los medios diarios de ese mismo mes.

| Resultado sobre 152 meses comparables | |
|---|---|
| Excel **igual** al máximo de los medios diarios | **139 (91,4 %)** |
| Excel mayor | 2 (1,3 %) |
| Excel menor | 11 |
| Razón Excel / diario, mediana | **1,000** |

> `MAXIMOS ABSOLUTOS` es el **máximo de los caudales medios diarios**, no el
> pico instantáneo.

**Consecuencia:** comparar ese valor contra el pico instantáneo de HEC-HMS
haría salir el modelo alto de forma sistemática, porque el pico instantáneo es
siempre mayor o igual que la media diaria que lo contiene. El sesgo no sería del
modelo sino de la comparación.

### Cómo se corrige: se promedia el modelo, no se escala la observación

Hay dos salidas y no valen lo mismo:

| Opción | Problema |
|---|---|
| Convertir la observación a pico con un factor | Introduce un factor **supuesto**, que es justo lo que la sección 7 evita en todo lo demás |
| **Promediar el modelo a escala diaria** | Es exacto: el hidrograma simulado está completo y su media móvil de 24 h se calcula sin suponer nada |

**Se adopta la segunda.** El contraste es:

> máximo de la media móvil de **24 horas** del hidrograma simulado
> **contra**
> análisis de frecuencia del máximo anual de los medios diarios observados

Así se comparan magnitudes homogéneas y no se añade ninguna suposición.

**Limitación que hay que declarar.** Verificar a escala diaria es más débil que
verificar el pico instantáneo: comprueba el volumen de escorrentía y el tiempo
grueso de respuesta, pero **no la atenuación del pico**. El caudal instantáneo
de diseño, que es el que dimensiona la estructura, queda validado solo de forma
indirecta. El informe debe decirlo.

---

## 8. Lo que el informe debe declarar

1. Que se recibieron dos fuentes de la CAR y por qué se adoptó una.
2. Que la precipitación satelital existe y se excluyó de la interpolación, con
   el motivo.
3. Que las series de la CAR son mensuales, y qué implica: sin serie diaria
   íntegra, sin detección de acumulados.
4. Qué estaciones de la CAR entraron, cuáles se descartaron y en qué módulo se
   decidió cada descarte.
5. Que el análisis de consistencia se corrió sobre un campo con dos operadores,
   y qué resultó.
6. Que la calibración del M14b no es viable con estos datos, y por qué.
7. El resultado de la verificación de crecientes, diciendo si hubo ajuste.
8. **Qué fracción de la cuenca quedó verificada** (aquí el 66 %), en qué se
   diferencia el resto, y que la cifra del punto de cierre es extrapolación.
9. El resultado de la transposición desde la estación de aguas abajo, con el
   exponente adoptado y las incertidumbres que arrastra.

---

## 9. Cambios que esta especificación exige en la programación

| Componente | Cambio |
|---|---|
| `config/config.yaml` | Bloque de la fuente CAR: rutas, parámetros y tipos admitidos, categorías excluidas |
| `MANIFIESTO.yaml` | El campo `caudales.origen` admite `car`; se diligencia el bloque |
| M03 | El catálogo de estaciones pasa a ser la unión de `CNE_IDEAM_Final.shp` y `CNE_CAR.shp`, con el operador registrado |
| M04 | Adaptador nuevo para el formato largo de ocho columnas del Excel |
| M07 | Admite el máximo anual derivado de máximos mensuales, declarando el origen |
| M14 o módulo nuevo | Verificación de crecientes según la sección 7 |
| M02 | Delimitar la cuenca de la estación de aguas abajo, para que su área deje de ser estimada |
| `cadena.yaml` | M14b pasa de `pendiente` a `no viable`, con el motivo |

### Cada análisis lleva su figura

**Ningún ejercicio de comparación se da por programado sin su figura**, y la
produce el mismo módulo que hace el análisis, en el mismo paso. Una cifra en una
tabla no permite ver si el desajuste es sistemático o de un solo punto, y el
informe la necesita de todas formas.

Las que exige este trabajo:

| Figura | Qué debe mostrar | Módulo |
|---|---|---|
| Estaciones por operador | IDEAM y CAR distinguidas, sobre la cuenca y su área, con las aforadas marcadas | M03 |
| Cobertura temporal por fuente | Barras por estación y año, coloreadas por red, para ver de un vistazo qué periodo sostiene cada una | M04 |
| Dobles masas entre redes | La consistencia contrastando estaciones de un operador contra el otro, que es donde una inconsistencia sistemática se vería | M05 |
| **Contraste de crecientes** | Frecuencia observada con su **banda de confianza**, y encima los picos modelados por periodo de retorno, en J24 y J29 | verificación |
| **Iteración de parámetros** | Si hubo ajuste: valor inicial, valor adoptado y **rango admisible** de cada parámetro, para que se vea que no se salió de él | verificación |
| Transposición | Lo observado en la estación de aguas abajo, lo transpuesto al cierre y el modelado | verificación |

Las dos marcadas en negrita son las que sostienen el capítulo de verificación:
sin ellas el informe afirma una coincidencia que el lector no puede juzgar.

### Las figuras del informe no son un anexo

Conviene no confundir dos cosas que suenan parecidas:

| | Qué es | Cómo llega |
|---|---|---|
| **Gráficas del informe** | Las que produce cada módulo con matplotlib | Van **dentro** del documento. El M15 las inserta resolviendo las instrucciones `Colocar Figura:` de la plantilla, buscándolas de forma recursiva. **No se entregan como anexo** |
| **Anexo 7, Mapas Temáticos** | Las 29 planchas de QGIS que produce el M16 | Se entregan **como anexo**, en PDF |

Verificado sobre el informe generado: **107 imágenes incrustadas** y una sola
instrucción sin resolver, que ya estaba corregida en la plantilla.

### Después de cada ajuste hay que regenerar las dos cosas

**Un producto que no se regenera queda describiendo un estado anterior sin que
nada lo señale**, que es la misma clase de fallo silencioso que la doctrina
persigue en el resto de la cadena. Medido en este estudio al revisarlo: el
informe en disco era del 23 de agosto y la plantilla se había corregido el 24,
de modo que arrastraba una figura que ya no faltaba.

Tras cualquier cambio que mueva números o añada análisis:

1. **Las gráficas del informe**, corriendo los módulos que las producen.
2. **Las planchas del Anexo 7**, corriendo el M16.
3. **El informe**, corriendo el M15, que es quien las incorpora.

Y para los temas nuevos, como la verificación de crecientes, **la gráfica se
programa junto con el análisis**, no después: la produce el mismo módulo en el
mismo paso, porque una figura que hay que acordarse de generar aparte acaba con
fecha distinta del dato que ilustra.

### Lo que debe quedar programado como regla general

La verificación no es un añadido de este estudio. Al programarla, estas tres
cosas se resuelven **por búsqueda y no por lista fija**, para que sirvan al
siguiente proyecto sin tocar código:

1. **Buscar estaciones de caudal dentro de la cuenca** y emparejarlas con la
   unión del modelo más próxima, declarando la distancia. Si no hay unión
   cerca, reportarlo y sugerir un punto de quiebre en la delimitación.
2. **Calcular qué fracción del área queda aforada** y si los puntos están
   anidados, porque de eso depende cuánto restringe la verificación.
3. **Buscar la estación de aguas abajo más próxima cuya cuenca contenga la del
   estudio**, para la transposición. Si no existe, declararlo: significa que la
   porción baja queda sin ninguna observación.
