# GUI OCT/OCE con cámara USB

Esta es una **variante separada**. La entrada original `run_gui.py` y su GUI
siguen sin cambios. La variante muestra video USB a la derecha del B-scan en
BM, MB y todos los patrones de escaneo. En alineación continua oculta el panel
USB para mostrar en ese espacio el zoom de 20 píxeles alrededor del Z
seleccionado. La cámara se detiene si la grabación está desactivada; si está
activada, se reabre antes de OCT y continúa grabando aunque el panel esté oculto.

## Inicio

```powershell
cd C:\Users\proyecto.pi1081\Desktop\OCT_ASTRA\04_OPTIMIZED_IMPLEMENTATION\PYTHON_GUI
python -m pip install -r requirements-usb.txt
python run_gui_usb.py
```

También se puede abrir `run_gui_usb.bat` con Python 3.11. La cámara conectada
Logitech C525 funcionó en este equipo con el índice USB `0` a 640×480. Si
Windows cambia el índice, seleccione otro en el panel **Cámara USB** y pulse
**Conectar**. La captura y el refresco visual son independientes del hilo NI:
solo se conserva el fotograma USB más reciente, sin acumular una cola.

El interruptor **Grabar video USB al adquirir** está desactivado por defecto.
Al activarlo, se abre un MP4 y se espera a que su primer fotograma esté escrito
**antes** de iniciar el motor OCT. Si la cámara o el codificador falla al abrir,
OCT no empieza. El MP4 se cierra al completar, detener o fallar la adquisición.
Con archivo OCT, se guarda junto al `.bin` como `nombre_usb.mp4`; sin archivo
OCT, se usa `USB_fecha_hora.mp4` en la carpeta de salida. Nunca se sobrescribe
un video existente. En alineación, el video puede grabarse aunque el panel USB
esté oculto por el zoom. El interruptor no inserta fotogramas en el `.bin`.

El video USB no está sincronizado por hardware con PFI12/PFI13: su grabación
comienza antes del OCT, pero la cadencia depende de la cámara USB y del
codificador. Si se activa **Guardar**, el B-scan OCT continúa sin preview como
en la GUI principal, mientras el video USB permanece visible. Para maximizar
el rendimiento de una adquisición extensa sin video, use la GUI principal.

Esta variante hereda los controles OCT de la GUI principal: loop crosshair
BM 10×10 mm (500 A-lines en X y 500 en Y), límites de intensidad en dB,
rango Z del B-scan y tiempo de adquisición en el log. Durante el loop crosshair
el video USB permanece visible; solo la alineación continua MB sustituye el
video por el zoom de Z.
