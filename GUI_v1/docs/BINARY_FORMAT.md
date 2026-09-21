# Formato OCT/OCE `.bin` v1.0

Todos los enteros del formato son little-endian y tienen tamaño fijo.

## Prefijo de 64 bytes

Struct Python: `<8sHHIHHIIQQQII4x`

| Offset | Tipo | Campo |
|---:|---|---|
| 0 | `char[8]` | magic `OCTOCE1\0` |
| 8 | `u16` | versión mayor |
| 10 | `u16` | versión menor |
| 12 | `u32` | flags: bit 0 complete, bit 1 little-endian |
| 16 | `u16` | dtype: 1 = uint16 |
| 18 | `u16` | reservado |
| 20 | `u32` | longitud del JSON |
| 24 | `u32` | CRC32 del JSON |
| 28 | `u64` | offset del payload, actualmente 65536 |
| 36 | `u64` | A-lines esperadas |
| 44 | `u64` | A-lines confirmadas |
| 52 | `u32` | píxeles por A-line |
| 56 | `u32` | reservado |
| 60 | 4 bytes | padding |

El conteo confirmado del prefijo se actualiza después de cada bloque escrito.
El tamaño físico del archivo impone además un límite: el lector usa el mínimo
entre A-lines confirmadas, esperadas y completas según el tamaño.

## Header JSON

Empieza en el byte 64, se codifica UTF-8 y termina según `json_len`. El resto
hasta `data_offset` es cero. Incluye:

- fecha UTC y versión de software;
- estado completo/incompleto;
- backend y dispositivos;
- dtype, bits válidos, shape y nombres de ejes;
- parámetros A/B/M/sync, `BFramesDelay`, modo y patrón;
- factores V/mm, rangos, park, frecuencia y terminales;
- política y temporización de PFI12/PFI13;
- hash SHA-256 del plan;
- conteos de integridad y causa de una parada;
- valores de inicio/fin λ (nm) usados para la linealización k diagnóstica;
  la calibración medida del espectrómetro y la dispersión siguen pendientes.

## Payload

Muestras espectrales crudas `<u2`, C-order, sin separadores ni puntos sync.

- BM: `[B, M, A, pixel]`.
- MB: `[B, A, M, pixel]`.
- Crosshair BM: `[B, M, sweep_xy=2, A, pixel]`.
- Crosshair MB: `[B, sweep_xy=2, A, M, pixel]`.

Si el archivo está incompleto, puede abrirse como
`[committed_alines, pixels_per_aline]`. Solo un archivo completo se remodela a
su `planned_shape`.

## Compatibilidad y recuperación

`octoce.storage.read_info()` verifica magic, versión mayor, dtype, límites del
JSON y CRC. `open_memmap()` expone el payload sin cargarlo completo en RAM.

La cabecera se reescribe al cierre sin desplazar el payload porque su capacidad
está reservada. Si el proceso se interrumpe antes del cierre, el prefijo conserva
el último bloque confirmado y el flag `complete` permanece apagado.
