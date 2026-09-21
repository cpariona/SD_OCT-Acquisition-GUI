# AGENTS.md

This file defines the implementation contract for coding agents and human contributors working on the canonical SD-OCT/OCE acquisition application.

## 1. Read legacy implementations before coding

Before implementing a subsystem, inspect both `GUI_v1/` and `GUI_v2/` for the corresponding behavior. Treat them as reference material only.

- `GUI_v1/` and `GUI_v2/` are read-only during the migration unless the user explicitly requests otherwise.
- Do not import legacy modules from the canonical package.
- Do not copy an entire legacy module without first reducing it to the responsibilities defined here.
- When the two legacy versions disagree, identify the difference explicitly before choosing behavior.

## 2. Canonical scope

Canonical code lives only under `src/octacq/`.

Included:

- hardware configuration;
- scan requests and trajectory planning;
- acquisition scheduling and lifecycle;
- PCIe-6323 control;
- PCIe-1433 / NI-IMAQ control;
- OCE triggering;
- raw storage and metadata;
- acquisition GUI;
- focused hardware diagnostics.

Excluded:

- OCT reconstruction or signal processing;
- wavelength-to-k resampling code;
- dispersion compensation;
- FFT/B-scan processing;
- USB-camera preview or recording;
- compatibility wrappers around legacy APIs.

Processing belongs to the external MATLAB project.

## 3. Naming

Canonical names describe what a component is now, not its history.

Do not introduce names containing migration/history qualifiers such as:

- `new`
- `old`
- `before`
- `after`
- `legacy`
- `optimized`
- `v1`, `v2`, or similar version suffixes

This prohibition applies to canonical modules, classes, functions, variables, and implementation strategies. The existing directories `GUI_v1/` and `GUI_v2/` keep their names only because they are historical references.

When better behavior replaces previous behavior, replace it rather than keeping parallel implementations with historical names.

## 4. Module responsibilities

Keep the package small and explicit.

### `config.py`
Owns the typed configuration model and loading of `config/system.toml`.

Use three conceptual groups:

- `HardwareProfile`: physical device identity, routing, calibration, and safety-relevant hardware settings;
- `ScanRequest`: acquisition-specific user input;
- `RuntimePolicy`: buffering and non-physical execution settings.

Do not mix these groups into one large configuration object.

### `scan.py`
Owns scan geometry and transforms a `ScanRequest` into a deterministic acquisition schedule.

It must not access GUI widgets, disk, NI drivers, or MATLAB processing.

### `acquisition.py`
Owns acquisition state and lifecycle. It executes a prepared schedule, coordinates raw-data delivery to storage, propagates stop/error state, and exposes events to the GUI.

It must not perform OCT signal processing.

### `storage.py`
Owns the raw binary file contract, metadata/header serialization, incomplete-file finalization, and raw-data reading needed for integrity or inspection.

It must not reconstruct OCT images.

### `hardware/daq.py`
Direct NI-DAQmx implementation for AO0/AO1 and counter timing. Do not add a wrapper layer around `nidaqmx` solely for abstraction.

### `hardware/camera.py`
Direct NI-IMAQ implementation for the PCIe-1433 and line-scan camera. Keep DLL interaction here.

### `hardware/system.py`
Coordinates DAQ and camera lifecycle and synchronized acquisition. It is not a generic plugin/backend framework.

### `ui/app.py`
GUI only. Convert widget state into `ScanRequest`; display state/progress/errors; invoke acquisition actions. Hardware sequencing does not belong in the UI.

### `tools/diagnostics/`
Small, explicit, manually executed hardware diagnostics. A diagnostic should answer one physical question. Do not turn diagnostics into a second application framework.

## 5. Timing contract

The target is not mathematically zero elapsed time; it is zero unnecessary software-inserted idle time between valid hardware events.

Design rules:

1. Timing-critical acquisition is hardware-timed.
2. Python prepares and arms hardware; Python is not the per-A-line clock.
3. NI-IMAQ should remain connected for the active hardware session instead of being reopened for every acquisition.
4. DAQ tasks must not be created and destroyed for every logical sweep when one prepared hardware schedule can cover multiple sweeps.
5. Counter tasks arm before the AO master starts.
6. AO start timing is the common hardware reference for synchronized counter output.
7. PFI12 starts the PCIe-1433 acquisition buffer; the frame-grabber/camera configuration generates the line timing for the active buffer.
8. PFI13 starts the OCE excitation event according to the schedule.
9. Sync/transition samples move the galvos and are not stored as acquired OCT samples.
10. Required physical hold/rearm intervals must be explicit schedule elements, not hidden sleeps.
11. If memory/device limits require blocks, block boundaries are an internal scheduling detail. Do not expose a separate acquisition mode for them.
12. Do not add arbitrary margins such as a fixed percentage hold unless measurements or hardware requirements justify them.

Read `docs/hardware.md` before changing timing behavior.

## 6. Single source of truth

`config/system.toml` is authoritative for laboratory hardware values.

Do not repeat calibration values or device routing in:

- README prose;
- docstrings;
- GUI defaults;
- tests;
- multiple Python modules.

Code may derive values from the profile. Acquisition metadata may snapshot the values used for a run, but snapshots are not configuration sources.

## 7. Dependencies and Python

Use the Python range and dependencies declared in `pyproject.toml`.

Do not add:

- dependency-check scripts;
- runtime package-version gates;
- compatibility helper modules;
- environment wrappers;
- duplicated `requirements*.txt` files unless the user explicitly requests them.

If a dependency change is necessary, update `pyproject.toml` directly and document the reason in the commit/PR.

## 8. Tests

Tests protect contracts, not implementation trivia.

Prefer a small suite covering:

- scan ordering and geometry;
- hardware schedule timing calculations;
- configuration separation and safety-critical constraints;
- storage complete/incomplete round trips;
- acquisition lifecycle/error cleanup with controlled substitutes;
- one GUI smoke test if practical.

Avoid multiple tests that assert the same behavior through different private functions. Do not test private helpers merely because they exist.

Hardware diagnostics are not unit tests and belong in `tools/diagnostics/`.

## 9. Simplicity rules

- Prefer direct modules over framework layers.
- Do not create `helpers.py`, `utils.py`, `wrappers.py`, `validators.py`, `managers.py`, or `services.py` as generic dumping grounds.
- A small local private function is preferable to a new cross-project abstraction used once.
- Add a module only when it owns a clear responsibility.
- Keep public data models explicit and typed.
- Prefer derived properties over storing duplicated derived values.
- Do not maintain two implementations of the same behavior for compatibility unless explicitly requested.

## 10. Change procedure

For each implementation step:

1. inspect the relevant legacy code;
2. state the behavior that will be preserved, changed, or dropped;
3. implement only within the canonical framework;
4. add the minimum tests needed for the contract changed;
5. update documentation only if architecture or hardware facts changed;
6. do not edit `main` directly unless the user explicitly authorizes it.

The first coding task after this framework is created should be a structured audit of `GUI_v1/` and `GUI_v2/`, mapped to the canonical modules above, before porting production code.
