# Inventario para la construcción del informe

Generado por `M15_plantilla.py`. Lista lo que la cadena deja disponible en este estudio.

## Marcadores

| marcador | qué hace |
|---|---|
| `{{figura: nombre \| leyenda}}` | inserta la figura, centrada, con su leyenda numerada por capítulo |
| `{{tabla: ruta \| leyenda}}` | inserta la tabla desde su CSV, con las columnas que se declaren |
| `{{valor: clave}}` | sustituye por el valor calculado, en la frase |
| `{{hallazgos: prefijo}}` | inserta lo que los módulos midieron sobre ese tema, con su severidad |
| `{{decisiones}}` | reúne lo que reclama criterio del consultor |
| `{{pendiente: texto}}` | marca visible de lo que falta escribir |

Los marcadores se escriben en un párrafo propio, salvo `{{valor:}}`, que va dentro de la frase.

## Figuras disponibles

94 figuras en `data/05_resultados/graficos`.


### M03

- `{{figura: M03_estaciones_por_red | }}`

### M04b

- `{{figura: M04b_cobertura | }}`
- `{{figura: M04b_completitud | }}`
- `{{figura: M04b_linea_temporal | }}`
- `{{figura: M04b_sensibilidad | }}`

### M05

- `{{figura: M05_anomalos | }}`
- `{{figura: M05_ciclo_anual | }}`
- `{{figura: M05_complemento | }}`
- `{{figura: M05_correlaciones | }}`
- `{{figura: M05_doble_masa | }}`
- `{{figura: M05_estaciones | }}`
- `{{figura: M05_faltantes | }}`

### M05b

- `{{figura: M05b_ciclo_por_fase | }}`
- `{{figura: M05b_contraste | }}`
- `{{figura: M05b_indice_oni | }}`

### M06

- `{{figura: M06_contraste_fases | }}`
- `{{figura: M06_isoyetas | }}`

### M07

- `{{figura: M07_cuantiles | }}`
- `{{figura: M07_histograma_pdf | }}`
- `{{figura: M07_papel_probabilidad | }}`
- `{{figura: M07_series_pmax | }}`

### M08

- `{{figura: M08_isoyetas_pmax | }}`

### M10

- `{{figura: M10_areas_subcuencas | }}`
- `{{figura: M10_cn_subcuencas | }}`
- `{{figura: M10_curva_hipsometrica | }}`
- `{{figura: M10_delimitacion_cuenca | }}`
- `{{figura: M10_delimitacion_subcuencas | }}`
- `{{figura: M10_distribucion_altimetrica | }}`
- `{{figura: M10_mapa_areas | }}`
- `{{figura: M10_mapa_cn | }}`
- `{{figura: M10_mapa_cobertura | }}`
- `{{figura: M10_mapa_pendiente | }}`
- `{{figura: M10_mapa_rezago | }}`
- `{{figura: M10_pendiente_contraste | }}`
- `{{figura: M10_pendiente_por_resolucion | }}`
- `{{figura: M10_tiempos_subcuencas | }}`
- `{{figura: M10_tiposuelohidrologico | }}`
- `{{figura: M10_zonificacionhidrografica | }}`

### M11

- `{{figura: M11_mapa_precipitacion | }}`
- `{{figura: M11_mapa_zonas | }}`
- `{{figura: M11_precipitacion_subcuencas | }}`
- `{{figura: M11_zonas | }}`

### M11c

- `{{figura: M11c_curvas_arf | }}`

### M12a

- `{{figura: M12a_cambio_climatico | }}`
- `{{figura: M12a_cambio_departamental | }}`
- `{{figura: M12a_idf_comparacion | }}`
- `{{figura: M12a_idf_invias | }}`
- `{{figura: M12a_idf_silva | }}`

### M14

- `{{figura: M14_escenarios_cc | }}`
- `{{figura: M14_hidrograma_J24 | }}`
- `{{figura: M14_hidrograma_J24_referencia | }}`
- `{{figura: M14_hidrograma_J29 | }}`
- `{{figura: M14_hidrograma_J29_referencia | }}`
- `{{figura: M14_hidrograma_Sink-1 | }}`
- `{{figura: M14_hidrograma_Sink-1_referencia | }}`
- `{{figura: M14_qmax_vs_periodo | }}`
- `{{figura: M14_qmax_vs_periodo_referencia | }}`
- `{{figura: M14_sensibilidad_hipotesis | }}`

### M14c

- `{{figura: M14c_media_movil_24h | }}`
- `{{figura: M14c_verificacion_J24 | }}`
- `{{figura: M14c_verificacion_J29 | }}`

### M18

- `{{figura: M18_balance_mensual | }}`
- `{{figura: M18_balance_por_franja | }}`
- `{{figura: M18_caudal_mensual | }}`
- `{{figura: M18_ciclo_adimensional | }}`
- `{{figura: M18_contraste_ena | }}`
- `{{figura: M18_diagrama_budyko | }}`
- `{{figura: M18_etr_comparacion | }}`
- `{{figura: M18_etr_dispersion | }}`
- `{{figura: M18_etr_elevacion | }}`
- `{{figura: M18_lluvia_contra_elevacion | }}`
- `{{figura: M18_mapa_coef_escorrentia | }}`
- `{{figura: M18_mapa_escorrentia | }}`
- `{{figura: M18_mapa_etp | }}`
- `{{figura: M18_mapa_etr | }}`
- `{{figura: M18_mapa_precipitacion | }}`
- `{{figura: M18_mapa_rendimiento | }}`
- `{{figura: M18_serie_etp | }}`
- `{{figura: M18_serie_etr | }}`

### M18a

- `{{figura: M18a_anios_por_estacion | }}`
- `{{figura: M18a_ciclo_anual | }}`
- `{{figura: M18a_etp_comparacion | }}`
- `{{figura: M18a_etp_contra_elevacion | }}`
- `{{figura: M18a_gradiente_altitudinal | }}`
- `{{figura: M18a_gradiente_mensual | }}`
- `{{figura: M18a_isotermas | }}`
- `{{figura: M18a_mapa_temperatura | }}`
- `{{figura: M18a_serie_mensual_cuenca | }}`

### M18b

- `{{figura: M18b_aporte_coeficientes | }}`
- `{{figura: M18b_mapa_coeficiente | }}`
- `{{figura: M18b_reparto_mensual | }}`

### M19

- `{{figura: M19_caudal_ambiental | }}`
- `{{figura: M19_curva_de_duracion | }}`
- `{{figura: M19_irh | }}`

## Tablas disponibles

72 tablas bajo `data/02_procesado`. La ruta del marcador es la relativa a ese directorio, sin extensión.

- `{{tabla: M09_subcuencas_pequenas | }}`
- `{{tabla: balance/balance_mensual | }}`
- `{{tabla: balance/balance_mensual_serie | }}`
- `{{tabla: balance/balance_multianual | }}`
- `{{tabla: enso/ciclo_anual_por_fase | }}`
- `{{tabla: enso/clasificacion_oni | }}`
- `{{tabla: enso/contraste_entre_fases | }}`
- `{{tabla: enso/precipitacion_por_fase | }}`
- `{{tabla: estaciones/M05_anomalos | }}`
- `{{tabla: estaciones/M05_complemento | }}`
- `{{tabla: estaciones/M05_consistencia | }}`
- `{{tabla: estaciones/M05_correcciones | }}`
- `{{tabla: estaciones/M05_discrepancias | }}`
- `{{tabla: estaciones/M05_estado_estaciones | }}`
- `{{tabla: estaciones/inventario_estaciones | }}`
- `{{tabla: estaciones/matriz_sensibilidad | }}`
- `{{tabla: estaciones/sensibilidad_series | }}`
- `{{tabla: frecuencia/ajustes | }}`
- `{{tabla: frecuencia/atipicos_bajos | }}`
- `{{tabla: frecuencia/cuantiles | }}`
- `{{tabla: frecuencia/pmax24h_anual | }}`
- `{{tabla: frecuencia/pmax24h_serie | }}`
- `{{tabla: hidrologia/abstraccion_inicial | }}`
- `{{tabla: hidrologia/balance_subcuencas | }}`
- `{{tabla: hidrologia/embalses | }}`
- `{{tabla: hidrologia/escenarios_cc | }}`
- `{{tabla: hidrologia/hidrogramas | }}`
- `{{tabla: hidrologia/qmax_por_periodo | }}`
- `{{tabla: hidrologia/qmax_por_periodo_referencia | }}`
- `{{tabla: hidrologia/resultados_por_elemento | }}`
- `{{tabla: hidrologia/transito | }}`
- `{{tabla: hidrologia/verificacion_crecientes | }}`
- `{{tabla: infiltracion/coeficientes_por_subcuenca | }}`
- `{{tabla: infiltracion/infiltracion_mensual | }}`
- `{{tabla: morfometria/curva_hipsometrica | }}`
- `{{tabla: morfometria/distribucion_altimetrica | }}`
- `{{tabla: morfometria/parametros | }}`
- `{{tabla: morfometria/pendiente_por_escala | }}`
- `{{tabla: morfometria/subcuencas | }}`
- `{{tabla: morfometria/tiempo_concentracion | }}`
- `{{tabla: morfometria/tiempo_concentracion_por_subcuenca | }}`
- `{{tabla: precipitacion/arf | }}`
- `{{tabla: precipitacion/campos_promediados | }}`
- `{{tabla: precipitacion/estaciones_del_balance | }}`
- `{{tabla: precipitacion/precipitacion_anual_por_subcuenca | }}`
- `{{tabla: precipitacion/precipitacion_mensual_cuenca | }}`
- `{{tabla: precipitacion/precipitacion_por_subcuenca | }}`
- `{{tabla: precipitacion/zonificacion | }}`
- `{{tabla: regimen/curva_de_duracion | }}`
- `{{tabla: regimen/irh_y_caudal_ambiental | }}`
- `{{tabla: regimen/percentiles | }}`
- `{{tabla: series/precipitacion_mensual | }}`
- `{{tabla: series/precipitacion_mensual_anomalos | }}`
- `{{tabla: series/precipitacion_mensual_complementada | }}`
- `{{tabla: series/precipitacion_mensual_origen | }}`
- `{{tabla: series/series | }}`
- `{{tabla: temperatura/gradiente | }}`
- `{{tabla: temperatura/gradiente_mensual | }}`
- `{{tabla: temperatura/isotermas | }}`
- `{{tabla: temperatura/temperatura_etp_serie_anual | }}`
- `{{tabla: temperatura/temperatura_mensual | }}`
- `{{tabla: temperatura/temperatura_mensual_cuenca | }}`
- `{{tabla: temperatura/temperatura_por_estacion | }}`
- `{{tabla: temperatura/temperatura_por_subcuenca | }}`
- `{{tabla: tormenta/asignacion_pluviometros | }}`
- `{{tabla: tormenta/cambio_climatico | }}`
- `{{tabla: tormenta/desagregacion | }}`
- `{{tabla: tormenta/hietograma_resumen | }}`
- `{{tabla: tormenta/hietogramas | }}`
- `{{tabla: tormenta/idf | }}`
- `{{tabla: tormenta/idf_silva_hoja | }}`
- `{{tabla: tormenta/verificacion_idf_24h | }}`

## Familias de hallazgos

210 hallazgos en 27 módulos, agrupados en 54 familias. El prefijo del marcador puede ser la familia entera o una clave concreta.

| familia | hallazgos |
|---|---|
| `{{hallazgos: isoyetas}}` | 22 |
| `{{hallazgos: estaciones}}` | 10 |
| `{{hallazgos: calificador}}` | 10 |
| `{{hallazgos: sensibilidad}}` | 9 |
| `{{hallazgos: temperatura}}` | 9 |
| `{{hallazgos: importar}}` | 8 |
| `{{hallazgos: verificacion}}` | 8 |
| `{{hallazgos: balance}}` | 8 |
| `{{hallazgos: consistencia}}` | 7 |
| `{{hallazgos: adoptado}}` | 6 |
| `{{hallazgos: informe}}` | 6 |
| `{{hallazgos: regimen}}` | 6 |
| `{{hallazgos: frecuencia}}` | 5 |
| `{{hallazgos: subcuencas}}` | 5 |
| `{{hallazgos: idf}}` | 5 |
| `{{hallazgos: red}}` | 4 |
| `{{hallazgos: drenaje}}` | 4 |
| `{{hallazgos: morfometria}}` | 4 |
| `{{hallazgos: cambio_climatico}}` | 4 |
| `{{hallazgos: hietograma}}` | 4 |
| `{{hallazgos: modelo}}` | 4 |
| `{{hallazgos: infiltracion}}` | 4 |
| `{{hallazgos: dem}}` | 3 |
| `{{hallazgos: calibracion}}` | 3 |
| `{{hallazgos: enso}}` | 3 |
| `{{hallazgos: numero_curva}}` | 3 |
| `{{hallazgos: relieve}}` | 3 |
| `{{hallazgos: desagregacion}}` | 3 |
| `{{hallazgos: resultados}}` | 3 |
| `{{hallazgos: car}}` | 2 |
| `{{hallazgos: ideam}}` | 2 |
| `{{hallazgos: cobertura}}` | 2 |
| `{{hallazgos: complemento}}` | 2 |
| `{{hallazgos: maximos}}` | 2 |
| `{{hallazgos: tiempo_concentracion}}` | 2 |
| `{{hallazgos: arf}}` | 2 |
| `{{hallazgos: transito}}` | 2 |
| `{{hallazgos: escenarios}}` | 2 |
| `{{hallazgos: computo}}` | 2 |
| `{{hallazgos: anexos}}` | 2 |
| `{{hallazgos: etp}}` | 2 |
| `{{hallazgos: punto_descarga}}` | 1 |
| `{{hallazgos: catalogo}}` | 1 |
| `{{hallazgos: m04}}` | 1 |
| `{{hallazgos: agregacion}}` | 1 |
| `{{hallazgos: anomalos}}` | 1 |
| `{{hallazgos: m05}}` | 1 |
| `{{hallazgos: tiempo_rezago}}` | 1 |
| `{{hallazgos: tiempo_viaje}}` | 1 |
| `{{hallazgos: altitud}}` | 1 |
| `{{hallazgos: gradiente}}` | 1 |
| `{{hallazgos: zonificacion}}` | 1 |
| `{{hallazgos: meteorologia}}` | 1 |
| `{{hallazgos: planchas}}` | 1 |

## Cosas que tener en cuenta

- Un nombre de figura mal escrito deja el apartado mudo. El M15 lo reporta como insumo ausente, pero conviene revisarlo en el inventario antes.
- Los marcadores `{{hallazgos:}}` pueden repetirse en varios apartados: cada uno inserta lo suyo y no se duplica dentro del mismo apartado.
- El formato de la plantilla manda. Lo que el M15 inserta hereda los estilos del documento, de modo que cambiar el aspecto es cambiarlo en Word una vez.
- Las leyendas se numeran solas con campos de Word. Al abrir el documento hay que responder que sí a la actualización.
- La tabla de contenido y la de ilustraciones se dejan vacías a propósito: Word las rehace al actualizar campos.
