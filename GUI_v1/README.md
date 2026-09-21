# OCT / OCE Acquisition — Python

Aplicación de adquisición para el sistema SD-OCT/OCE con NI PCIe-6323,
NI PCIe-1433 y cámara lineal Sensors Unlimited GL2048R. La aplicación separa
el camino crítico de adquisición de la GUI, el procesamiento y el disco.

## Qué incluye esta versión

- GUI en Python/Tkinter con los controles solicitados: A-lines, B-scans,
  repeticiones M, puntos sync y longitudes X/Y.
- Modos BM y MB con orden de datos explícito.
- Patrones raster, crosshair, meridianos/polar y lineal horizontal/vertical.
- Conversión predeterminada `X = 0.2364067 V/mm` y `Y = 0.1941748 V/mm`, editable en
  la configuración de hardware y registrada en cada archivo.
- AO0/AO1 precargados y temporizados por hardware.
- `ctr0 → PFI12` inicia cada buffer en el PCIe-1433; su generador de patrón
  produce los pulsos CC1 de A-line. `ctr1 → PFI13` inicia la excitación OCE.
- PFI13 usa lógica TTL de 5 V con tiempo alto configurable (10 µs por defecto)
  y tiempo bajo igual a nueve veces el alto: 10 % de ciclo útil en un tren de
  pulsos. En una adquisición normal se emite **un pulso finito por evento OCE**;
  los espacios entre eventos dependen del patrón/MB/BM y no tienen duty fijo.
  Para ver un tren breve en el osciloscopio, ejecutar
  `python check_pfi13_scope.py` desde esta carpeta (1000 pulsos, 0,1 s).
  Usar entrada de 1 MΩ y tierra D GND; el nivel físico no se calibra por software.
- Frecuencia solicitada ajustable hasta 147 klps. El periodo CC1 del ICD tiene
  resolución de 0,1 µs, así que DAQ y cámara usan la frecuencia efectiva
  cuantizada, que también se registra en el header.
- Puntos sync con interpolación quintic; no reciben trigger de cámara y no se
  guardan en el payload.
- `BFramesDelay` ajustable en µs: desplaza PFI13 respecto al primer PFI12. Si
  el pulso OCE rebasa la última A-line, AO mantiene la posición los puntos
  necesarios sin adquirir datos para que el pulso no sea truncado.
- Ring NI-IMAQ con ocho buffers, solicitud por número acumulativo y parada ante
  saltos, duplicados o pérdidas.
- Escritura `.bin` en un consumidor separado, con header JSON versionado y
  recuperación de archivos incompletos.
- Preview OCT desacoplado y limitado en frecuencia, con remoción estándar del
  espectro DC medio activada por defecto (conmutable sin alterar el raw),
  colormaps, ventana de intensidad por percentiles, cursores Z/lateral y perfil
  de fase relativa en la profundidad elegida. Si se atrasa, se omite un
  refresco; los datos crudos no se omiten.
- Reconstrucción diagnóstica con remuestreo λ→k mediante valores editables de
  inicio y fin (nm), FFT de 8192 y un solo rango axial visible. Los valores
  iniciales `1453.0`/`1292.69 nm` son provisionales; deben afinarse con la
  calibración espectral real para obtener escala axial y fase cuantitativas.
- Crosshair aparece como un B-scan lógico con cortes X–Z e Y–Z simultáneos y
  una cruz X/Y en la profundidad Z elegida. Se puede seleccionar cada sweep
  para inspeccionar su fase y fijar manualmente los límites verticales del plot.
- La vista OCT usa la mayor parte del panel; el colormap inicial es **Grises**
  y los límites fijos iniciales de fase son **−15 a +15 rad**. En alineación
  aparece a la derecha un zoom de 20 píxeles de profundidad centrado en Z.
- Se aceptan longitudes `X=0`, `Y=0` para adquisición estacionaria. El botón
  **Alineación continua** adquiere MB en el centro con M=1000 hasta pulsar
  Detener, sin archivo ni puntos sync. PFI12 y PFI13 usan contadores continuos
  sincronizados por hardware: a 50 klps solicitados, ambos disparan a 50 Hz y
  PFI13 permanece alto 2 ms (10 % del periodo). Para que NI-IMAQ pueda cerrar
  cada frame antes del siguiente trigger, la cámara se configura solo en esta
  alineación a 52,632 klps efectivos; cada bloque de 1000 A-lines ocupa 19 ms
  y deja 1 ms libre. La GUI muestra ambas tasas en el log. La adquisición normal
  conserva su frecuencia configurada y su temporización por segmentos.
- **Crosshair continuo** repite indefinidamente un B-scan lógico BM de
  1000 A-lines: 500 en X y 500 en Y, recorrido 10×10 mm, M=1. Es solo para
  preview, sin archivo ni OCE; Detener parquea los galvos.
- La ventana de intensidad puede ajustarse por percentiles o por límites
  absolutos negro/blanco en dB diagnósticos (`20·log10(|FFT|+1)`, no dBm).
  El B-scan permite seleccionar `Z inicio` y
  `Z fin` entre los bins FFT 1–4096 (por defecto 1–2048). La zona más lejana
  puede mostrar artefactos conjugados; ampliar la vista no crea datos
  full-range nuevos.
- El log de la GUI y la consola muestran el tiempo de adquisición en segundos
  al completar o detener, medido después del armado del backend.
- Si **Guardar datos crudos** está marcado, no se lanza el trabajador de
  reconstrucción ni se emiten previews. El archivo conserva los valores k en
  su header, pero el payload sigue siendo espectro crudo sin correcciones.
- Backend de simulación para probar todo el flujo sin mover los galvos.
- El cierre no escribe la posición de park si el preflight de cámara falla antes
  del primer arranque de AO.

## Inicio rápido (simulación)

Python 3.11–3.14 de 64 bits es compatible. Instale las dependencias en el mismo
intérprete con el que se inicia la GUI.

```powershell
cd C:\Users\proyecto.pi1081\Desktop\OCT_ASTRA\03_NEW_IMPLEMENTATION\PYTHON_GUI
python -m pip install -r requirements.txt
python run_gui.py
```

En este equipo NumPy, Pillow y Tkinter ya están disponibles, de modo que la GUI
puede ejecutarse directamente. También se puede abrir `run_gui.bat`.

## Habilitar el backend NI

```powershell
python -m pip install -r requirements-hardware.txt
python run_gui.py
```

En la GUI, seleccione **Hardware NI** solo después de completar la lista de
puesta en marcha en [docs/HARDWARE_SETUP.md](docs/HARDWARE_SETUP.md).

Se completó una adquisición de hardware controlada con `Dev1`/`img0`, 32 A-lines
de 2048 píxeles, sin buffers perdidos ni duplicados. Esto valida el transporte,
no la calibración óptica. Por defecto, el backend bloquea AO si `Trigger Mode`
sigue reportando `Internal`. Al armar hardware, la opción predeterminada
configura el rango OPR, `Fixed Exp`, polaridad alta y periodo/ancho CC1 antes de
habilitar AO.

## Semántica de BM y MB

- **BM**: por cada B-scan espacial se repite la línea completa M veces. Orden
  físico y de archivo: `bscan → repetición → A-line`; shape
  `[B, M, A, pixel]`.
- **MB**: en cada posición lateral se adquieren M A-lines antes de mover el
  galvo. Orden: `bscan → A-line → repetición`; shape `[B, A, M, pixel]`.

Estas siglas no son universales. Confirme que esta definición coincide con la
utilizada en el laboratorio antes de adquirir datos definitivos.

## Patrones

- **Raster**: X es el eje rápido y los B-scans se distribuyen en Y.
- **Crosshair**: cada B-scan lógico contiene dos sweeps consecutivos por el
  centro, primero X y luego Y. El archivo incorpora el eje `sweep_xy` de tamaño
  2; cada sweep conserva la cantidad de A-lines indicada.
- **Meridianos**: B diámetros únicos con ángulos en `[0, π)` dentro de la elipse
  definida por las longitudes X/Y.
- **Lineal**: recorre la misma línea horizontal o vertical por el centro en
  ambos sentidos. En BM el sentido alterna en cada repetición M; en MB alterna
  en cada B-scan y nunca invierte las M A-lines temporales de una posición.

Las longitudes son extensiones pico a pico centradas en `(0, 0)`; centro y
límites de tensión forman parte del modelo y pueden ampliarse como controles
cuando se confirme la geometría de la muestra.

## Formato de datos

Cada `.bin` contiene:

1. prefijo binario fijo de 64 bytes;
2. header JSON y padding hasta 64 KiB;
3. espectros crudos `uint16 little-endian` contiguos, sin puntos sync.

El prefijo se actualiza por bloque. Un archivo detenido conserva el conteo de
A-lines válidas y queda marcado `incomplete`. La especificación está en
[docs/BINARY_FORMAT.md](docs/BINARY_FORMAT.md).

Inspección de un archivo:

```powershell
python -m octoce.cli inspect .\data\OCTOCE_20260911_120000.bin
```

Uso desde Python:

```python
from octoce.storage import open_memmap, read_info

info = read_info("acquisition.bin")
raw = open_memmap("acquisition.bin", logical_shape=info.complete)
```

Lectura en MATLAB (sin toolboxes):

```matlab
addpath('C:/Users/proyecto.pi1081/Desktop/OCT_ASTRA/03_NEW_IMPLEMENTATION/PYTHON_GUI/matlab')
[parametros, alines] = leer_octoce_bin('acquisition.bin');
disp(parametros.scan)
espectro = alines{1};  % uint16, pixeles_por_aline x 1
```

`alines` es un vector de celdas `1×N` en orden temporal de adquisición; cada
celda es un espectro crudo. Los raster BM bidireccionales y los sweeps lineales
BM de retorno se reordenan desde el orden espacial guardado para recuperar ese
orden temporal. El lector valida
firma, versión y CRC32, y admite archivos incompletos leyendo solo las A-lines
confirmadas. Para archivos muy grandes, cargar todas las celdas puede consumir
mucha RAM.

## Pruebas

```powershell
python -m unittest discover -s tests -v
```

Las 37 pruebas cubren trayectorias, conversiones V/mm, orden BM/MB, remoción DC, intensidad,
fase diagnóstica, preview,
archivo completo/incompleto, protección contra sobrescritura, secuencia de
armado DAQ y adquisiciones simuladas extremo a extremo para todos los modos y
patrones.

## Decisiones pendientes de confirmar

No se debe afirmar sincronización óptica final hasta resolver y medir:

- la acción exacta configurada en NI-IMAQ para el PFI12 que llega al PCIe-1433
  y cómo se enruta a una adquisición de línea/CC;
- polaridad, ancho y latencia trigger→exposición de la cámara;
- confirmar si la unidad de `BFramesDelay` debe permanecer en microsegundos o
  representa conteos discretos de frame en el sistema externo;
- diámetro real del haz sobre los espejos, posición de JP7 y posición de park;
- raster unidireccional o bidireccional;
- meridianos como diámetros de 180° o radios de 360°;
- calibración longitud de onda→k y compensación de dispersión para que el
  preview sea cuantitativo;
- si el lector existente exige otro contrato `.bin`.

Hasta entonces, el modo de simulación es el predeterminado y el preview se
etiqueta como diagnóstico.
