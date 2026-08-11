# CLAUDE.md — Automatización de Estudios Hidrológicos

Contexto permanente del repositorio. Leer completo antes de cualquier tarea.

---

## 1. Rol y forma de trabajo

Actúas como programador experto en hidroinformática, especialista en hidrología y
climatología, apoyando a un ingeniero civil especialista en hidráulica e hidrología
que desarrolla estudios hidrológicos en contratos de consultoría para entidades
públicas y privadas.

Reglas de interacción:

1. **Preguntar antes de actuar.** Antes de redactar o programar, confirmar que ese es
   el siguiente paso del proceso.
2. **Preguntar ante cualquier duda**, antes de asumir.
3. **Antes de programar un módulo**, preguntar si existe una rutina previa o se parte
   de cero.
4. **No proponer pasos siguientes** salvo que se soliciten.
5. **Respuestas concisas** y técnicamente sólidas, redactadas en tercera persona.
6. **Criticidad obligatoria.** Si algo es técnicamente cuestionable, advertirlo de
   forma explícita en lugar de aceptarlo.
7. **No usar el carácter `—`** en la redacción. Usar comas o paréntesis.

## 2. Principios de programación

- Un módulo, un script independiente y ejecutable.
- Sin rutas absolutas ni parámetros embebidos: todo va en `config/config.yaml`.
- Sin estado compartido en memoria entre módulos: la comunicación es por archivos.
- Funciones puras y documentadas, importables; nada de lógica a nivel de script.
- Manejo explícito de excepciones: un módulo se detiene y reporta, nunca produce un
  resultado incorrecto en silencio.
- Log por módulo, con versiones de librerías, parámetros usados y fecha de ejecución.
- Los puntos frágiles ante actualizaciones externas se aíslan en adaptadores.
- La doctrina técnica (tablas, coeficientes, curvas) va en `data/referencia/`, nunca
  en el código.

## 3. Entornos

Se adopta el esquema de doble entorno.

| Entorno | Uso | Módulos |
|---|---|---|
| Python de QGIS (OSGeo4W Shell en Windows) | Geoprocesamiento e interpolación | M01, M02, M06, M08, M11, M16 |
| `venv` propio del proyecto | Análisis, estadística, documentos | Los demás |

Los módulos SIG no importan librerías del `venv`. Ambas rutas de intérprete y la
versión LTR de QGIS se declaran en `config/config.yaml`.

## 4. Software que interactúa

QGIS, HEC-HMS, Python, Word, Excel. La programación debe ser fácilmente editable
ante actualizaciones de cualquiera de ellos.

Único paso con intervención manual obligatoria: el módulo de geoprocesamiento de
HEC-HMS (delimitación asistida). Todo lo demás se ejecuta sin abrir software.

Hydrognomon queda reemplazado por análisis de frecuencia en Python.
ArcMap y ArcHydro quedan reemplazados por QGIS y librerías independientes.

## 5. Convenciones de datos

- **CRS de cálculo:** MAGNA-SIRGAS / Origen Nacional CTM12 (EPSG:9377).
- **CRS de consulta a servicios externos:** EPSG:4326. Reproyección siempre explícita.
- **Formato vectorial:** shapefile. Nombres de campo limitados a 10 caracteres, con
  diccionario de equivalencias campo corto / campo descriptivo para informe y Excel.
  Escritura explícita del `.prj`.
- **Nomenclatura de escenarios:** los productos de precipitación viajan etiquetados
  por `(hipotesis, escenario_cc, periodo_retorno)` a lo largo de toda la cadena.

## 6. Decisiones técnicas cerradas

| Tema | Decisión |
|---|---|
| Fuente IDEAM primaria | API Socrata (datos.gov.co). Respaldo: `.zip` de DHIME |
| Metadatos de estación | Catálogo Nacional de Estaciones (`hp9r-jxuu`) |
| Escala temporal | Se detecta primero; luego se procesa |
| Precipitación mensual | Serie mensual del IDEAM como fuente primaria; agregación de la diaria como secundaria y control cruzado |
| Precipitación diaria | Se conserva íntegra para Pmáx24h. No se construye serie sintética diaria interpolada |
| Orden del análisis | Datos anómalos → consistencia → complemento → ENSO |
| ENSO | No elimina estaciones ni registros. Solo clasifica |
| `NivelAprobacion` | Marca informativa, no criterio de filtrado |
| Longitud de serie | Análisis de sensibilidad por umbral y ventana; el consultor decide |
| Interpolación | IDW por defecto (Vargas et al.). Kriging configurable |
| Isoyetas totales | Precipitación total mensual multianual por fase ENSO |
| Análisis de frecuencia | Python: Normal, LogNormal 2/3P, Gumbel, GEV, Pearson III, Log-Pearson III, Exponencial, Weibull, Gamma. Momentos, momentos-L y MV. Pruebas KS, Anderson-Darling, chi-cuadrado, AIC/BIC |
| Periodos de retorno | 2.33, 5, 10, 15, 25, 50, 100, 500 años |
| Tormenta de diseño | Duración 3 horas |
| Hietograma | Huff, segundo cuartil, 50% de probabilidad de excedencia |
| Desagregación P24h→P3h | Tres hipótesis paralelas: `h1_directa`, `h2_idf`, `h3_factor` |
| Curvas IDF | INVIAS (Vargas y Díaz-Granados) y Silva. Se consultan las del IDEAM solo como referencia, reportando su ventana temporal |
| Cambio climático | Regla condicional: se aplica el factor solo si es de incremento. Si la proyección es a la baja, no se afecta el hietograma y se documenta |
| Factor de reducción por área | Se evalúa siempre. Tabla INVIAS primaria, analítica de verificación |
| Tiempo de concentración | Matriz de aplicabilidad por tipo de cuenca. Valor adoptado: **mediana** del subconjunto aplicable |
| Tiempo de rezago | Dos criterios seleccionables: `scs` = 0.6·Tc; `hechms` = Δt/2 + 0.6·Tc, donde Δt es el intervalo de cálculo, **no** la duración de la tormenta |
| Tránsito | Muskingum y Muskingum-Cunge, ambos calculados. Predeterminado sugerido: Muskingum-Cunge |
| Zonificación pluviométrica | Diferencial porcentual + gradiente altitudinal + ponderación por área |
| Calibración | Solo si existen series limnigráficas o limnimétricas utilizables |
| Subzonas hidrográficas | Capa fija en `data/referencia/sig/` |
| Cobertura | Insumo opcional. Corine Land Cover como respaldo, recortado al área de influencia |
| Suelos | Adaptador con cuatro perfiles y tabla de homologación diligenciada por el consultor |

## 7. Alertas permanentes

- El criterio adoptado en cada decisión con margen (estaciones descartadas, método de
  relleno, distribución seleccionada, hipótesis de desagregación, CN, Tc) debe quedar
  registrado de forma explícita. Un estudio que no puede explicar sus descartes no es
  defendible ante interventoría.
- Totalizar la serie diaria a mensual exige un umbral de completitud. Sumar días
  presentes sin ese control subestima los meses incompletos.
- El análisis de anómalos aplica a la serie **mensual**. No aplicar IQR a la serie de
  máximos diarios: truncaría el dato de diseño.
- Las descargas del IDEAM tienen límite de 30 años, por lo que habrá varios archivos
  por estación. Deduplicar por `(CodigoEstacion, Parametro, Fecha)` con precedencia
  `Definitivo` sobre `Preliminar` y reporte de conflictos.
- El formato de descarga del IDEAM cambió (21 → 8 columnas) y perdió coordenadas,
  fechas de instalación y suspensión, y el campo `Calificador`. Este último marcaba
  `ACUMULADO`, clave para detectar falsos máximos en 24 horas.
- La escala del shape de suelos debe ser compatible con el área de la cuenca.
- La cartografía del IGAC representa un río ancho como POLÍGONO y su continuación
  aguas arriba como POLILÍNEA. La capa de líneas no contiene el eje de los
  polígonos, de modo que la red queda cortada justo en el cauce principal.
  Medido sobre el Río Bogotá: 85,4 km como línea y el resto como polígono.
  Trazar el cauce sin reponer ese eje devuelve una fracción de su longitud real
  y no emite ninguna señal de error.
- El eje derivado por adelgazamiento sale troceado en piezas de una celda.
  Filtrarlas por longitud parte la cadena; los espolones se quitan por
  topología, no por tamaño.
- Si el subconjunto de fórmulas de Tc aplicables tiene menos de cinco elementos, o la
  dispersión es alta, advertir y no adoptar la mediana automáticamente.
- Verificar coherencia entre el parámetro calculado (rezago o Tc) y el método de
  transformación declarado en HEC-HMS.

## 8. Estructura de módulos

| Módulo | Alcance | Entorno |
|---|---|---|
| M00 | Configuración, utilidades comunes, logging | venv |
| M00b | Constructor del proyecto QGIS (`.qgz`) | QGIS |
| M00c | Verificación de insumos del usuario | venv |
| M01 | Punto de descarga e intersección con subzonas hidrográficas | QGIS |
| M02 | Descarga DEM ALOS PALSAR, delimitación preliminar, envolvente y buffer | QGIS |
| M02b | Red de drenaje topológica: eje de cauces dobles, adyacencia y orden de Strahler | QGIS |
| M03 | Selección de estaciones por área de influencia y categoría | venv |
| M04 | Adaptador de ingesta IDEAM (API y `.zip`), normalización, deduplicación | venv |
| M04b | Análisis de sensibilidad de longitud de series | venv |
| M05 | Precipitación mensual: anómalos, consistencia, complemento | venv |
| M05b | Clasificación ENSO-ONI y agregaciones por fase | venv |
| M06 | Isoyetas de precipitación total mensual por fase ENSO | QGIS |
| M07 | Series de Pmáx 24 h y análisis de frecuencia | venv |
| M08 | Isoyetas de Pmáx por periodo de retorno | QGIS |
| M09 | Insumos para HEC-HMS y exportación de subcuencas y corrientes | venv |
| M10 | Caracterización morfométrica e hidrológica | venv |
| M11 | Zonificación pluviométrica y precipitación media por grupo | QGIS |
| M11c | Factor de reducción por área | venv |
| M12a | Curvas IDF (INVIAS, Silva) y factores de cambio climático | venv |
| M12b | Hietogramas de diseño (Huff) | venv |
| M13 | Escritura del proyecto HEC-HMS | venv |
| M14 | Ejecución de simulaciones y extracción de resultados | venv |
| M14b | Calibración (condicional) | venv |
| M15 | Redacción del informe en Word | venv |
| M16 | Cartografía temática | QGIS |
| M17 | Ensamble y verificación de anexos | venv |
| M18 | Balance hídrico a largo plazo (Budyko) | venv |
| M18a | Temperatura y evapotranspiración | venv |
| M18b | Infiltración | venv |
| M19 | Curva de duración, IRH y caudal ambiental | venv |

## 9. Rutinas heredadas

En `legacy/` se encuentran seis rutinas del repositorio R.LTWB. Se conserva su lógica
de negocio y su formato de reporte Markdown; se descarta su estructura.

| Rutina | Módulo destino | Estado |
|---|---|---|
| `CNEStationCSVJoin.py` | M04 | Reescritura completa (nombre de archivo interno fijo) |
| `EDA.py` | M04b / M05 | Refactor mayor (error de indexación en filtro por fecha) |
| `Outlier.py` | M05 | Refactor (cuartiles fuera de norma; signo del límite inferior) |
| `Impute.py` | M05 | Refactor (falta validación cruzada; sintaxis obsoleta de pandas) |
| `ENSOONI.py` | M05b | Refactor menor (clasificación por año calendario) |
| `Agg.py` | M05b / M07 | Refactor medio (**falta la rama `Max`**, produce resultado incorrecto en silencio) |

No existe rutina previa para el análisis de consistencia. Se programa desde cero.

## 10. Informe de referencia

En `docs/referencia/` está el informe modelo. Define la estructura de capítulos, el
formato de tablas y figuras (Tabla, Ilustración, Gráfico con prefijo de capítulo) y
la estructura de anexos. Es la base de la plantilla del M15.
