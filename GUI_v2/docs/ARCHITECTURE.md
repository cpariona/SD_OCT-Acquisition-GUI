# Arquitectura

## Camino crítico

La GUI solo valida parámetros y envía comandos al `AcquisitionEngine`. El hilo
de adquisición no actualiza widgets, no hace FFT y no escribe al disco.

```text
GUI
 │
 ├─ plan inmutable ──> ScanPlanner
 │
 └─ Start ──> AcquisitionEngine
                │
                ├─ arma ring NI-IMAQ
                ├─ por segmento:
                │    1. precarga AO0/AO1
                │    2. arma ctr0/PFI12 y ctr1/PFI13
                │    3. inicia AO (AO StartTrigger libera contadores)
                │    4. copia exactamente el buffer acumulativo esperado
                │    5. valida número copiado y pérdidas
                │
                └─ Queue acotada ──> Writer ──> .bin
                                      └─ Queue best-effort (1) ──> Preview ──> GUI
```

El contador de cámara usa un `initial_delay` igual al número de puntos sync más
el desfase configurado. Por eso AO recorre la transición y PFI12 emite un único
pulso al inicio de cada segmento válido. Ese pulso inicia el buffer NI-IMAQ; el
generador de patrón del PCIe-1433 produce después un CC1 por cada A-line, con
periodo cuantizado a 0,1 µs según el ICD instalado. AO usa esa misma frecuencia
efectiva. El trigger OCE se agenda respecto al mismo
`AO StartTrigger`; `BFramesDelay` desplaza PFI13, en microsegundos, respecto al
primer PFI12 válido.

En MB cada segmento contiene las M A-lines de una posición y recibe un PFI13.
En BM cada repetición completa del B-scan recibe un PFI13. Crosshair conserva
un único B-scan lógico con sweeps X/Y, pero se divide en dos segmentos físicos
para poder insertar una transición sync sin disparos; PFI13 se emite al iniciar
X y no se repite al iniciar Y.

El patrón lineal es siempre bidireccional. En BM alterna el vector AO de cada
repetición M y el writer normaliza cada retorno al orden espacial antes del
preview/`.bin`. En MB alterna el orden de posiciones en cada B-scan, pero
mantiene intacto el orden temporal de las M A-lines adquiridas en cada punto.
Los puntos sync unen extremos coincidentes, evitando un flyback de extremo a
extremo entre barridos lineales consecutivos.

En esta versión, las adquisiciones finitas MB/BM con `sync>0`, y MB estacionario
con `sync=0`, agrupan hasta 64 sweeps por lote (acotados también a ~1 millón de
muestras AO). El planificador conserva el orden lógico; AO concatena las
trayectorias y un único armado de ctr0/ctr1 emite trenes finitos de N pulsos
separados por el mismo periodo de sweep. En BM crosshair el lote siempre
contiene pares X/Y completos: ctr0 emite cada sweep y ctr1 cada dos sweeps,
comenzando por X. PFI13 tiene 10 % de ciclo útil respecto a su periodo OCE,
y `BFramesDelay` conserva su desplazamiento
respecto a PFI12. Los puntos de hold adicionales para rearmar NI-IMAQ o cerrar
el pulso OCE no generan CC1 y no se guardan. El motor sigue comprobando cada
número de buffer y aborta el lote ante discontinuidad o parada del usuario.
MB móvil con `sync=0` conserva la ruta por segmento. La ruta por lotes pasó
pruebas físicas acotadas MB y BM crosshair (buffers consecutivos, cero pérdidas
y número correcto de pulsos PFI13); no sustituye una validación de series largas.
Tras una adquisición finita completa, la GUI optimizada retiene NI-IMAQ hasta
30 s para reutilizar el ring si ROI y configuración coinciden. Un error o una
parada cierran la sesión. La primera apertura sigue costando ~6,4 s medidos en
`imgRingSetup`; la segunda adquisición MB de prueba tardó 0,112 s totales.

La alineación continua MB estacionaria es una ruta especial: AO0/AO1 quedan en
0 V, no se usan puntos sync ni se rearman tareas entre bloques. Dos contadores
continuos arrancan sobre el mismo `ao/StartTrigger`, emiten PFI12/PFI13 cada
20 ms por defecto, y PFI13 tiene 2 ms de nivel alto (10 % real a 50 Hz). La
cámara corre a 52,632 klps efectivos solo en esta ruta, por lo que los 1000
A-lines ocupan 19 ms y NI-IMAQ dispone de 1 ms para completar/rearmar el buffer.
El lector sigue exigiendo números de buffer consecutivos y cero pérdidas.

Las tareas de contador se inician primero y quedan esperando el trigger. AO se
inicia al final y actúa como maestro del segmento. El criterio de término es la
tarea AO finita más la recepción del buffer NI-IMAQ exacto; nunca el contador
continuo del programa LabVIEW heredado.

## Aislamiento de consumidores

La cola de almacenamiento es deliberadamente acotada. Si el disco no sostiene
la tasa requerida, el motor deja de iniciar nuevos segmentos y conserva el
galvo en la última posición estable. No deja que el ring de cámara se sobreescriba
silenciosamente.

El preview se genera en un hilo independiente y se limita a la frecuencia configurada.
En BM se reconstruye el bloque `[A, pixel]`; en MB se acumula un B-scan usando
el promedio de las M repeticiones únicamente para visualización. El archivo
siempre recibe las M repeticiones crudas.

## Parada y errores

Cualquier timeout, buffer inesperado, pérdida NI-IMAQ o error de disco:

1. detiene el inicio de nuevos segmentos;
2. detiene/cierra las tareas DAQ;
3. si AO llegó a activarse, ejecuta una rampa temporizada hacia `park`;
4. detiene/cierra NI-IMAQ;
5. finaliza el archivo como `incomplete` con el motivo.

El backend de simulación respeta el mismo contrato para que este camino pueda
probarse sin activar el hardware.
