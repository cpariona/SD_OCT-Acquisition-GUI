# Architecture

## Objective

The canonical application is a small modular monolith for deterministic SD-OCT/OCE acquisition. It favors direct ownership of responsibilities over framework layers.

The legacy applications are references during migration; they are not dependencies.

## Data flow

```text
GUI
 │
 ▼
ScanRequest
 │
 ▼
Scan planner ──────► deterministic acquisition schedule
                         │
                         ▼
                  Acquisition engine
                    │             │
                    │             └────► raw storage
                    ▼
               Hardware system
                │           │
                ▼           ▼
             NI-DAQmx     NI-IMAQ
             PCIe-6323    PCIe-1433
```

No OCT signal-processing stage exists in the Python pipeline.

## Configuration model

The implementation should expose three concepts rather than one monolithic configuration object.

### HardwareProfile

Physical system facts and laboratory calibration:

- NI device/interface identities;
- AO/counter/PFI routing;
- camera sensor characteristics;
- galvo conversion and voltage limits;
- spectrometer calibration metadata;
- camera trigger configuration.

Source: `config/system.toml`.

### ScanRequest

Values defining one acquisition:

- BM or MB ordering;
- raster, crosshair, meridian, linear, or stationary geometry;
- A-lines;
- B-scans;
- M repetitions;
- scan dimensions and center;
- sync/transition samples;
- scan direction policy;
- OCE enabled/disabled for the run;
- OCE timing offset (`BFramesDelay` semantics, once confirmed).

These values come from the GUI or another explicit caller and are recorded in acquisition metadata.

### RuntimePolicy

Execution settings that do not describe the physical experiment:

- NI-IMAQ ring depth;
- writer queue depth;
- timeouts;
- parking ramp resolution;
- other bounded buffering/resource choices.

Runtime settings must not alter scan geometry or the scientific meaning of the raw data.

## Module boundaries

### `config.py`

Load `system.toml` and define typed configuration/request models. Validation should be close to the model whose invariant is being protected. Avoid a separate validator layer.

### `scan.py`

Generate deterministic spatial trajectories and timing events. The result should contain enough information for hardware execution without consulting GUI state.

The planner may derive internal blocks when device-memory constraints require them. Blocks are not a user-visible acquisition mode.

### `acquisition.py`

Own application-level acquisition states such as idle, armed, running, stopping, completed, stopped, and error. Coordinate schedule execution and storage without performing OCT reconstruction.

### `storage.py`

Persist raw `uint16` spectra and a versioned metadata header. Preserve incomplete acquisitions when safely possible. The storage format should record the actual scan request, hardware-profile snapshot, effective timing, and data ordering used for the run.

### `hardware/daq.py`

Translate the prepared schedule to PCIe-6323 AO and counter tasks using `nidaqmx` directly.

### `hardware/camera.py`

Own `imaq.dll` calls, camera attributes, ring-buffer setup, acquisition start/stop, buffer numbering, and lost-buffer reporting.

### `hardware/system.py`

Coordinate camera and DAQ as one physical instrument. The hardware session should be explicit: connect once, execute acquisitions, disconnect when the application/session ends or after an unrecoverable hardware error.

### `ui/app.py`

Own Tkinter widgets and user interaction only. It must not create NI tasks, call `imaq.dll`, generate trajectories, or write binary payloads directly.

## Concurrency

Keep concurrency limited to paths that require it:

- hardware acquisition must not be blocked by GUI repainting;
- raw writing may consume a bounded queue if disk I/O can stall;
- the GUI receives coarse events/progress from the acquisition layer.

There is no Python OCT-processing worker in the canonical application.

## Failure behavior

A hardware/acquisition error should converge on one cleanup path:

1. stop issuing new hardware work;
2. stop/abort active DAQ tasks;
3. stop the NI-IMAQ acquisition safely;
4. finalize the raw file as incomplete when applicable;
5. park the galvos only when AO has been activated and parking is safe;
6. return the application to a defined state or require reconnect if the hardware session is no longer trustworthy.

Do not duplicate cleanup logic across GUI callbacks and hardware modules.

## Migration principle

Port behavior, not file structure. For each subsystem, compare both legacy implementations first, retain the behavior that is physically justified, and implement it once in the canonical module.

## Implemented scan and persistence contracts

`config.load_config(path)` returns a `HardwareProfile` and a `RuntimePolicy`.
`ScanRequest` contains experiment values only. `scan.plan(request, profile)` is
pure and returns a schedule whose frames carry logical indices and whose blocks
carry AO waveforms. No widget state or driver object enters the planner.

BM visits B, M, then the active spatial line. MB visits B, spatial position, then
M samples at that fixed position. Crosshair inserts X/Y sweeps after M in BM and
before spatial position in MB. Meridian diameters span a half-turn. Raster Y is
centered when B=1. Linear orientation selects the moving axis. Zero lengths on
both axes mean stationary acquisition; the explicit stationary pattern requires
both lengths to be zero.

Linear bidirectionality is explicit (enabled by default, preserving the previous
linear behavior). It alternates each BM repetition and each MB B-scan. Raster
bidirectionality alternates B-scans. Storage reverses only backward BM frames to
spatial-forward order. MB position groups remain in acquisition order and their
M time axis is never reversed. Crosshair BM has one OCE event on X per X/Y pair;
MB has one OCE event per M group on either sweep.

Every frame has the same scheduled period for a request: transition samples,
active samples, then any necessary endpoint hold. The quintic transition excludes
its endpoints. Position jumps with zero sync are rejected. Timing holds cover
camera phase, the configured physical rearm requirement, and a delayed OCE pulse.
PFI13 HIGH remains the configured fixed width. There is no percentage margin.

The AO allocation budget in `RuntimePolicy` bounds each two-channel float64
waveform. Frame descriptors/raw queues use additional host memory. Blocks preserve
complete crosshair X/Y pairs and acquisition ordering. Each block is configured
once, counters arm first, then AO starts. Block setup can introduce a gap when the
budget forces multiple blocks; this implementation does not claim gapless streaming
across blocks. Metadata records planned/actual block counts and host setup durations.
These host measurements are not physical edge-to-edge gap measurements.

Continuous stationary MB and crosshair use regenerated AO cycles and continuous
counter trains, with no Python rearm between cycles. The cyclic transition joins
the preceding cycle's endpoint. A preparatory ramp reaches that endpoint before
arming the cycle. Continuous acquisition has no finite output file.

`Acquisition` owns one instrument worker and one bounded raw writer. GUI events
are coarse state/progress messages; Tk widgets are touched only on the Tk thread.
A full writer queue is an integrity error, never permission to drop frames. Stop
or error closes the hardware iterator, attempts all cleanup actions, drains valid
queued data, and finalizes the file. Cleanup errors remain visible. Confirmed
progress counts written A-lines, not merely received buffers.

## Binary contract

The format remains `OCTOCE1` major/minor 1.0: the 64-byte little-endian prefix uses
`<8sHHIHHIIQQQII4x`, followed by CRC32-protected UTF-8 JSON in a reserved 65536-byte
header. Payload is C-order little-endian uint16, without transitions or holds.
The prefix contains flags, JSON length/CRC, payload offset, expected/confirmed
A-lines and pixels per A-line. The complete and little-endian flags retain bits
0 and 1. Each append updates the confirmed prefix after writing all bytes.

JSON records the request, physical-profile snapshot, runtime resources, effective
CC1 timing, trigger timing, data direction policy, shape/order, and final integrity
and execution information. Wavelength endpoints remain metadata only; established
`k_start_nm`/`k_end_nm` metadata keys are retained for external readers.

An interrupted process leaves the last confirmed prefix readable even if the JSON
counts are stale. Readers use the minimum of expected, prefix-confirmed, and fully
present A-lines. Normal finalization flushes payload before publishing complete
status. This is recovery from interrupted acquisition, not a guarantee of atomic
header replacement or survival of power loss during a header write. Files with a
bad metadata CRC are rejected rather than guessed.

## Simulation decision

The reference simulator synthesizes fringes for reconstructed previews and inserts
software waits. It demonstrates GUI/processing behavior, not NI timing or integrity.
It is not part of the canonical application. Small controlled sources and driver
substitutes in tests supply known raw markers and failures without a backend/plugin
framework. Offline GUI startup and scan planning remain available.
