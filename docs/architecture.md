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
