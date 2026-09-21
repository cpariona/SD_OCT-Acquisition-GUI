# SD-OCT / OCE Acquisition

Canonical Python application for acquisition and hardware control of the laboratory SD-OCT/OCE system.

## Scope

This repository implementation is responsible for:

- scan definition and trajectory planning;
- NI PCIe-6323 timing and galvo control;
- NI PCIe-1433 / NI-IMAQ camera acquisition;
- OCE trigger generation;
- raw-data persistence and acquisition metadata;
- the acquisition GUI;
- focused hardware diagnostics.

OCT signal processing is intentionally out of scope. Reconstruction, spectral linearization, dispersion compensation, visualization derived from OCT processing, and quantitative analysis live in the MATLAB processing project.

USB-camera acquisition is also out of scope.

## Repository layout

```text
src/octacq/
    config.py
    scan.py
    acquisition.py
    storage.py
    hardware/
        daq.py
        camera.py
        system.py
    ui/
        app.py
config/
    system.toml
tools/
    diagnostics/
tests/
docs/
    architecture.md
    hardware.md
```

`GUI_v1/` and `GUI_v2/` remain in the repository only as legacy references during migration. They are not part of the canonical package and must not be imported by `src/octacq`.

## Configuration

`config/system.toml` is the single source of truth for laboratory hardware identity, routing, calibration, and runtime defaults. Do not duplicate those numeric values in README files or Python constants unless the value is a genuine protocol constant.

Acquisition-specific values such as A-lines, B-scans, M repetitions, scan dimensions, pattern, mode, sync points, and OCE delay belong to the acquisition request, not the hardware profile.

## Python

The canonical package supports 64-bit CPython 3.11 through 3.14. For a fresh laboratory environment, CPython 3.13 is the preferred baseline; keeping 3.11 compatibility avoids forcing an immediate interpreter migration while the hardware code is ported. Dependencies are intentionally minimal. NI-IMAQ access remains direct through the installed NI driver DLL; NI-DAQmx uses NI's `nidaqmx` package.

No compatibility checker, environment wrapper, or startup verifier is part of the framework. Compatibility is managed through `pyproject.toml`, the documented laboratory environment, and physical validation when hardware-facing code changes.

## Development rules

Read `AGENTS.md` before implementing or modifying the canonical package. The architecture and hardware timing contracts are defined in `docs/architecture.md` and `docs/hardware.md`.
