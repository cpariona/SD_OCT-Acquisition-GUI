# Hardware and synchronization audit

This document records the current understanding of the physical acquisition chain and defines the synchronization contract for the canonical implementation. Numeric laboratory settings live in `config/system.toml` and are not duplicated here.

## Hardware chain

| Component | Role in acquisition |
| --- | --- |
| NI PCIe-6323 | Generates X/Y galvo AO and hardware-timed camera/OCE trigger signals. |
| NI PCIe-1433 | Camera Link frame grabber. NI-IMAQ receives externally triggered acquisition buffers. |
| Sensors Unlimited GL2048R-10A | 2048-pixel, 12-bit line-scan OCT camera. |
| Thorlabs GVS002 | Galvanometer system driven by PCIe-6323 AO. |
| OCE excitation system | Receives the PFI13 trigger; the exact downstream actuator is outside this repository. |

The optional USB camera present in the legacy code is not part of the canonical system.

## Physical routing

The authoritative device names and terminals are defined in `config/system.toml`.

Conceptually:

```text
PCIe-6323 AO0 ─────────────► Galvo X
PCIe-6323 AO1 ─────────────► Galvo Y

PCIe-6323 ctr0 ── PFI12 ───► PCIe-1433 external buffer trigger
PCIe-1433 / camera timing ──► GL2048R CC1 line acquisition

PCIe-6323 ctr1 ── PFI13 ───► OCE excitation trigger
```

## What each timing variable means

### Requested line rate

A camera/hardware property used to derive the line period. The legacy camera configuration quantizes the CC1 period to the increment exposed by the installed ICD, so the effective line rate may differ slightly from the requested value. The actual/effective rate used for an acquisition must be recorded in metadata.

### Camera trigger width

Width of the camera-side CC1 trigger configuration. It is a hardware timing value, not a scan parameter.

### Camera phase offset

Relative offset used when scheduling the PFI12 camera event against the AO timing reference. It belongs to the hardware synchronization profile.

### Sync points

Unacquired AO samples used to move smoothly from the previous galvo position to the next active trajectory. They are part of an acquisition request because they change the timing/transition policy of that run, but they are not stored as OCT samples.

### BFramesDelay

Legacy name for the relative OCE timing offset between the camera event and PFI13. It is acquisition-specific, not a hardware-profile constant. Before the canonical UI is finalized, confirm experimentally that the unit and physical meaning are truly microseconds and not external frame counts.

### NI-IMAQ ring buffers

Runtime buffering. This protects transport from short consumer latency but does not define scan geometry or scientific timing.

### Writer queue size

Runtime/disk decoupling only. It must not change hardware event timing.

### Wavelength endpoints

Spectrometer calibration metadata for the external MATLAB processing pipeline. They are not used to clock acquisition hardware in the canonical Python application.

## Synchronization observed in the legacy implementations

The strongest reusable behavior is:

1. open/configure NI-IMAQ before starting motion/acquisition;
2. prepare the AO waveform and camera/OCE counter schedules;
3. arm counter tasks first;
4. start AO last;
5. use the AO start trigger as the common timing reference;
6. request NI-IMAQ buffers by monotonically increasing buffer number;
7. fail on discontinuity or newly reported lost frames;
8. store only active OCT samples, not transition/hold samples.

In the later legacy implementation, multiple logical sweeps are concatenated so AO and counters are not re-created for every sweep. That direction is correct. The canonical implementation should make this the normal execution model rather than expose a separate strategy.

## Source of avoidable delay

The principal software-inserted delay in the earlier implementation is repeated setup/teardown around logical segments:

```text
configure task → arm → run one segment → wait → close/recreate → next segment
```

NI-IMAQ setup also has a substantial fixed cost in the legacy measurements. The later implementation partially avoids it by temporarily retaining the camera session.

The canonical lifecycle should instead be:

```text
connect hardware
    ↓
keep NI-IMAQ session configured
    ↓
plan complete acquisition
    ↓
prepare/arm hardware schedule
    ↓
run hardware-timed acquisition
    ↓
return results while hardware session remains connected
    ↓
next acquisition or disconnect
```

The hardware session should normally live for the application's connected lifetime, not for an arbitrary idle timeout.

## Scheduling contract for the canonical implementation

- Python must not sleep between valid scan events to create timing.
- AO/counters define event timing in hardware.
- Physical transition or hold samples are allowed when they are required by galvo motion, camera/frame-grabber behavior, or OCE timing.
- Such samples must be represented explicitly by the schedule and excluded from stored OCT payload unless they are actual requested measurements.
- If a full acquisition cannot fit in one finite hardware buffer, divide it into internal blocks according to hardware/resource limits. Do not create a user-facing acquisition mode for this.
- Do not preserve the legacy fixed-percentage rearm margin merely because it exists; retain only margins supported by hardware requirements or measurements.
- Keep the camera session open while the instrument is connected so repeated acquisitions do not pay unnecessary NI-IMAQ setup cost.

## Legacy behavior that must be audited before porting

Codex should resolve these points from both implementations and, where code cannot answer them, mark them for a single physical test rather than create speculative compatibility logic:

1. exact NI-IMAQ external-trigger action used for PFI12 and the resulting buffer/exposure latency;
2. exact camera trigger polarity and trigger-to-exposure timing for the installed GL2048R ICD;
3. physical meaning and unit of `BFramesDelay`;
4. physical GVS002 JP7 setting and any galvo limits that depend on it;
5. whether all moving MB cases can be scheduled without software rearm gaps by using a continuous AO/counter schedule;
6. maximum practical AO/counter schedule size before internal block streaming is required;
7. required OCE hold behavior when a delayed PFI13 event extends beyond the last active A-line.

These are hardware facts, not reasons to add generic validators or alternate acquisition engines.
