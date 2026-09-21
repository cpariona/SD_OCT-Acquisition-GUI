# Puesta en marcha del hardware NI

Esta lista debe completarse antes de seleccionar **Hardware NI**. El modo de
simulación no abre NI-IMAQ ni NI-DAQmx.

## 1. Software y exclusividad

- Usar Python 3.11 x64.
- Mantener instalados NI-DAQmx y Vision Acquisition Software x64; este equipo
  tiene drivers 25.5.
- Instalar `nidaqmx` con `requirements-hardware.txt`.
- Cerrar cualquier VI, NI MAX grab o programa que tenga reservado `Dev1` o
  `img0`. No ejecutar LabVIEW y Python sobre los mismos dispositivos.

## 2. Conexiones a confirmar

- AO0 → galvo X, con masa analógica correspondiente.
- AO1 → galvo Y, con masa analógica correspondiente.
- PFI12/ctr0 → entrada de trigger del frame grabber PCIe-1433.
- PFI13/ctr1 → entrada del sistema de excitación OCE.
- Masa digital TTL común entre DAQ, frame grabber/cámara y excitador.

J7 del GVS002 es una entrada analógica diferencial. Verificar que AO y su
retorno estén cableados según la Figura 22 del manual y no asumir que una masa
digital sustituye el retorno analógico.

La implementación genera un pulso PFI12 por segmento para iniciar un buffer
NI-IMAQ. El generador de patrón del PCIe-1433 emite entonces los CC1 que producen
las A-lines del buffer. El backend configura `Trigger Mode = Fixed Exp`, la
polaridad CC1 alta, el rango OPR y el periodo/ancho CC1 usando los atributos del
ICD instalado.

`Trigger Each Buffer` queda activado por defecto sobre External0. Confirmar que
el cable procedente de PFI12 llega efectivamente a esa entrada del PCIe-1433.

## 3. Cámara

La interfaz instalada es `img0` y usa
`GL2048R-10A_icd0173rev3.icd` (2048 píxeles, 12 bits). El archivo guardado
muestra `Trigger Mode = Internal`; esto debe cambiarse o verificarse en tiempo
de ejecución.

Hay dos formas previstas:

1. configurar el modo external fixed exposure previamente en NI MAX y dejar
   “cambiar Trigger Mode” desactivado;
2. activar el cambio desde la configuración avanzada. El backend llama a
   `imgSetCameraAttributeString`; primero debe confirmarse el nombre/valor exacto
   expuesto por el ICD instalado.

La frecuencia predeterminada de 50 klps tiene un periodo de 20 µs y el pulso
predeterminado de cámara mide 5 µs. La GUI permite solicitar hasta 147 klps. El
ICD cuantiza el periodo a 0,1 µs: una solicitud de 147 klps usa 6,8 µs y una
tasa efectiva de 147,059 klps, por lo que anchos y retardos deben verificarse de
nuevo.

## 4. Límites de galvo

La DAQ admite el rango configurado ±10 V, pero ese no es automáticamente el
límite seguro del driver/galvo. Antes del primer escaneo real, registrar:

- límites positivos y negativos de cada eje;
- signo de cada eje;
- offsets de centro/park;
- slew rate y aceleración máximos;
- respuesta a una rampa de parqueo.

La GUI impone por ahora `|V| ≤ 9.5 V`; este valor es solo un guardrail de DAQ,
no una certificación mecánica.

El manual GVS002 especifica entrada analógica ±10 V y escala seleccionable
0,5/0,8/1,0 V/°. También indica 300 µs de respuesta de paso pequeña, 175 Hz
para triangular/sawtooth de recorrido completo y ~1 kHz únicamente para seno
de ±0,2°. La GUI muestra advertencias conservadoras cuando los puntos sync o la
tasa de barrido contradicen esas referencias; no las interpreta como una curva
completa de operación segura.

La configuración avanzada incluye `JP7 galvo (V/°)` y `Diámetro haz en
espejos`. JP7 se inicializa en 0,8 V/° porque es el ajuste de fábrica descrito
por el manual. El diámetro queda en 0 (desconocido); al introducir un valor
entre 0 y 5 mm, el software aplica conservadoramente la siguiente fila igual o
mayor de la tabla de ángulos recomendados, incluidos los límites asimétricos de
Y. Mientras permanezca en 0, se aplica la fila más restrictiva de 5 mm.

## 5. Verificación eléctrica sin muestra

Primero usar un plan pequeño y OCE deshabilitado. Medir simultáneamente:

- AO0 o AO1;
- PFI12;
- LVAL/FVAL o Exposure Active de la cámara, si está disponible;
- después PFI13.

Comprobar:

1. cero pulsos PFI12 durante los puntos sync;
2. exactamente una exposición por A-line válida;
3. fase constante entre actualización AO, PFI12 y exposición;
4. en MB, un PFI13 al inicio de cada grupo de M A-lines; en BM, un PFI13 por
   repetición del B-scan (M pulsos totales); para crosshair, X+Y es un único
   B-scan lógico y PFI13 solo inicia el sweep X de cada repetición;
5. números de buffer `solicitado == copiado` y cero pérdidas.

El desfase de PFI12 es configurable en microsegundos. Debe medirse y ajustarse;
no puede deducirse solo del código.

El backend realiza primero el preflight NI-IMAQ. Si este falla antes de que AO
arranque por primera vez, el cierre no escribe ni siquiera la posición de park,
por lo que una configuración de cámara inválida no energiza los galvos.

## 6. Ensayo incremental

1. Simulación con A=16, B=2, M=2, sync=10.
2. Hardware sin muestra y OCE apagado, con las mismas dimensiones.
3. Verificar señales y cierre/parqueo al pulsar Detener.
4. Adquirir señal estática sin barrido grande y comparar estabilidad con
   `LL Grab test.vi` sobre los mismos espectros crudos.
5. Activar un eje, luego el patrón final.
6. Activar OCE solo cuando PFI13 esté caracterizado.

No se debe usar una imagen OCT visualmente correcta como única prueba de
sincronización: la continuidad de buffers y la fase deben medirse por separado.

## Referencias NI

- [PCIe-6323 AO Sample Clock](https://www.ni.com/docs/en-US/bundle/pcie-pxie-6323/page/ao-sample-clock-signal.html)
- [PCIe-6323 counter timing](https://www.ni.com/docs/en-US/bundle/pcie-pxie-6323/page/counter-timing-signals.html)
- [NI-DAQmx Python API](https://nidaqmx-python.readthedocs.io/en/stable/task.html)
- [NI line-scan triggering](https://knowledge.ni.com/KnowledgeArticleDetails?id=kA00Z0000019PYoSAM)
- [Routing a trigger to Camera Link CC](https://knowledge.ni.com/KnowledgeArticleDetails?id=kA03q000000YIUqCAO)
