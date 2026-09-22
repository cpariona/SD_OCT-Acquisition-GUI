# Hardware and synchronization

## Instrument and routing

`config/system.toml` is authoritative for device identity, electrical limits,
calibration, routing and hardware timing. The instrument comprises the PCIe-6323,
PCIe-1433, GL2048R line-scan camera, GVS002 galvos and an external OCE excitator.

```text
PCIe-6323 AO X/Y -> galvo X/Y
PCIe-6323 camera counter -> PFI12 -> PCIe-1433 external buffer trigger
PCIe-1433 pattern generator -> CC1 -> GL2048R line exposures
PCIe-6323 OCE counter -> PFI13 -> external OCE system
```

PFI12 triggers a whole acquisition buffer, not one A-line. The frame grabber
produces CC1 timing for all active lines of that buffer. Sync and hold samples are
AO-only. The external trigger uses `IMG_TRIG_ACTION_BUFFER`, active-high polarity,
and the configured external input. Confirm the actual cable mapping in NI MAX.

The camera ICD attributes used by the existing setup are Serial Commands,
Trigger Polarity, Operational Setting, Trigger Mode, CC1 Trigger Width and CC1
Trigger Period. Polarity is applied before the other serial settings because the
installed ICD resets the sensor when it changes. The code reads back trigger mode,
polarity, CC1 period/width, ROI, row stride and pixel format before any scan.
The observed ICD period increment and OPR ranges are encoded as device rules;
the requested laboratory rate and width come from TOML. The effective quantized
rate is recorded in acquisition metadata and must match the DAQ rate readback.

## Timing contract

The AO start trigger is the common reference. Camera configuration precedes AO
preload. Both counter tasks are committed and armed before AO starts. One camera
pulse occurs per frame, and PFI13 occurs once per scheduled OCE event. Its HIGH
width is fixed by the profile and never scaled with the event period. Counter
width/period/delay readbacks are checked before enabling the schedule.

`BFramesDelay` is provisionally a temporal offset in microseconds from the PFI12
associated with an OCE event. Negative values are allowed only when the resulting
PFI13 event does not precede AO start. Its exact physical relationship remains
pending oscilloscope validation. There is no alternate interpretation in code.

The planner reserves only explicit physical timing: sync transition, active lines,
camera phase, camera rearm, and a tail hold if a delayed OCE pulse extends beyond
capture. It does not use the reference application's fixed percentage hold or
change the camera rate to hit a nominal alignment frequency.

`camera.camera_rearm_us` is intentionally absent until measured. Add that key to
`system.toml` after determining the required gap between the end of a frame and
the next trigger. Zero is valid only if physically demonstrated. Planning works
without it; physical acquisition requires it. The session-continuity diagnostic
accepts an explicit `--camera-rearm-us` candidate and applies it only to the
in-memory diagnostic profile; it never edits `system.toml`. Sync may already
supply the needed inter-frame interval; the planner adds only the remaining hold.
A positive camera phase remains within one active A-line tick so no transition is
captured.

Continuous stationary alignment retains the configured camera rate and derives
its trigger cadence from the schedule. Continuous crosshair uses one regenerated
X/Y cycle without OCE. Neither recreates tasks at cycle boundaries. Finite scans
use one preload per AO allocation block. Block boundaries may have setup gaps;
measure those gaps electrically if an experiment requires multiple blocks. The
recorded host setup duration is not an electrical timing measurement.

## Camera session and integrity

Connect opens/configures one NI-IMAQ session and ring. Successful acquisitions
leave that session available with cumulative buffer numbers. There is no idle
expiry. Changing frame height rebuilds the ring through a new session; an aborted
or failed exposure invalidates the session and requires reconnect. Disconnect
closes the frame grabber explicitly.

The external trigger wait is infinite to accommodate intentional idle periods.
The synchronous frame-extraction wait remains finite. Its timeout is configured
before the ring starts, using the configured allowance plus the active frame
capture duration; NI-IMAQ does not permit changing this attribute while the ring
is acquiring. A stop while extraction is blocked may therefore wait until a buffer
arrives or that timeout expires. This GUI stop is not an independent emergency stop.

Each buffer is requested by cumulative number, checked for exact equality,
copied using the reported row stride, and released even on a validation error.
Changes in lost-frame counts also stop acquisition. A full writer queue stops
acquisition rather than dropping spectra. All valid written rows remain readable
as an incomplete file. The session must reconnect before counter numbering reaches
the NI-IMAQ reserved buffer-number values.

NI documents the trigger action and indefinite trigger wait in
[imgSessionTriggerConfigure2](https://www.ni.com/docs/en-US/bundle/ni-imaq-c-api-ref/page/niimaqfunctionreference/imgsessiontriggerconfigure2.html),
and extraction/release behavior in
[Ring Acquisitions](https://www.ni.com/en/support/documentation/supplemental/06/ring-acquisitions.html).

## Galvo positioning and cleanup

Before the first scan, verify that the instrument is at the configured park
position. The initial software position assumes that physical condition. No task
is created merely to park after a camera preflight failure. The first controlled
motion begins only during an explicit acquisition.

The profile's voltage bounds and the conservative GVS002 beam envelope restrict
planned positions, including center and park. The beam envelope uses the restrictive
row already used for unknown beam diameter in the reference application. Verify
JP7, beam clearance, axis signs, analog return wiring and dynamic limits on the
real installation. A static voltage envelope is not a slew/settling certification.

On stop/error, AO and counters stop and all tasks are closed. The held AO command
is derived from the driver's generated-sample count, then a quintic park ramp is
attempted. If that count or task shutdown is unreliable, automatic park is refused
and the error remains visible. Restore the physical park condition and restart
the application before reacquisition. The count-to-output relationship and output
retention across task close must be validated on the installed DAQ driver.

## Manual validation and diagnostics

Normal unit tests never open NI hardware. Before any physical script, verify the
wiring, clear the galvo path, establish park, close other owners of the devices,
and isolate OCE from the specimen until its trigger is characterized.

Two scripts require an explicit `--execute`:

- `tools/diagnostics/oce_pulse.py`: does PFI13 retain the configured HIGH width at
  a chosen event period? It creates no AO task and opens no camera. Specify the
  test interval with `--period-ms`; inspect HIGH/LOW and edge count on a scope.
- `tools/diagnostics/session_continuity.py`: do two finite acquisitions deliver
  consecutive buffers while retaining the same camera session, including idle
  time between runs? Supply the candidate rearm gap explicitly with
  `--camera-rearm-us <value>`; the candidate is not persisted. Default stationary
  geometry stays at the configured park; `--pattern crosshair` exercises X/Y
  ordering. OCE requires `--oce`. The script reports expected PFI12/PFI13 counts
  for comparison with the scope.

Outstanding physical checks:

1. Installed ICD attributes, External input mapping, polarity and first exposure
   latency relative to AO/PFI12. No valid exposure may lie in a sync interval.
2. Minimum camera rearm interval for the intended frame heights and rates.
3. Fixed-width PFI13 and the provisional delay relation, including delayed events
   whose pulse ends after active capture and crosshair X-only BM triggering.
4. Clock coercion, pulse count and continuity for stationary and moving BM/MB,
   bidirectional scans, and both continuous loops.
5. Driver-generated-sample readback on abort, actual held AO voltage, park ramp,
   JP7/beam envelope and settling over the intended spatial range.
6. Disk/ring throughput and the maximum practical allocation size. If blocks are
   needed, measure their actual inter-trigger gaps; software tests cannot prove
   absence of physical dead time.
7. Reuse after an idle interval, frame-height reconfiguration, stop, lost-buffer
   error and disconnect. Confirm that no devices remain reserved after cleanup.

## Findings from the reference comparison

Both applications use the same geometry, NI-IMAQ calls and raw binary envelope.
The first implementation configures/arms/closes DAQ tasks for each segment. The
second batches a bounded number of uniform sweeps and adds a timed camera cache,
but keeps a segment fallback and software gaps between batches. Its arbitrary
percentage rearm margin and period-dependent OCE HIGH are not canonical behavior.
Its different galvo/spectral calibration defaults are superseded by `system.toml`.

The old GUI's claim that each PFI12 pulse is one valid line contradicts its buffer
trigger configuration. Its 50 Hz alignment path also changes the camera line rate
and OCE duty cycle. Neither behavior is used here. Old hardware scripts contain
physical actions and are retained untouched as references, not collected as tests.
