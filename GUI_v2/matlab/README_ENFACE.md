# Raster en face y medición en MATLAB

La función `raster_enface_medicion.m` selecciona un `.bin` OCT/OCE BM raster,
muestra un B-scan central, reconstruye el plano en face promediando la magnitud
OCT en un rango de Z bins y
permite medir distancias en mm entre dos puntos.

```matlab
addpath('C:/Users/proyecto.pi1081/Desktop/OCT_ASTRA/04_OPTIMIZED_IMPLEMENTATION/PYTHON_GUI/matlab');
raster_enface_medicion();
```

También se puede abrir el archivo de prueba directamente:

```matlab
raster_enface_medicion('C:/Users/proyecto.pi1081/Desktop/OCT_ASTRA/04_OPTIMIZED_IMPLEMENTATION/PYTHON_GUI/data/F_0.5_1x1mm.bin');
```

Defina `Z inicio` y `Z fin` con las cajas o los dos sliders (ambos extremos
son inclusivos). Un clic en el B-scan centra la ventana conservando su ancho.
Las dos líneas de color indican el rango. Después use **Reconstruir en face**:
la intensidad se promedia en magnitud lineal sobre esa profundidad, y solo
después se convierte a dB. Pulse **Medir 2 puntos**, haga dos clics y
lea `dx`, `dy` y la distancia euclidiana en mm al pie de la ventana y en la
consola. **Limpiar medidas** quita las líneas y permite empezar de nuevo.

El archivo de prueba contiene 1000 B-scans × 1000 A-lines, M=1, campo X/Y
de 1×1 mm y 2048 muestras espectrales. La herramienta valida magic/CRC32,
usa el payload en memoria mapeada y mantiene solo un B-scan en RAM cada vez.
Remueve el espectro DC de cada B-scan, invierte el orden del detector, interpola
uniformemente en k con los extremos λ del header y muestra intensidad
`20·log10(|FFT|+1)`. Es una reconstrucción diagnóstica: el `Z bin` no es mm y
la precisión axial/OCE requiere calibración espectral y dispersión medidas.

La reconstrucción completa de ese archivo, verificada con MATLAB R2025b en
este equipo, produjo una imagen 1000×1000 con ejes X/Y de 1×1 mm. Una ventana
axial muy amplia requiere una FFT completa por B-scan y puede tardar más.
En la prueba con `F_0.5_1x1mm.bin`, el rango 100–140 produjo el en face
1000×1000 en aproximadamente 38 s de reconstrucción; el tiempo puede variar
según la computadora y el ancho elegido.
