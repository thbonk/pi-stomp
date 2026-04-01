# Tuner Feature — Software Design

## Context

pi-Stomp v3 lacks a built-in instrument tuner. Musicians need to tune between songs without external tools. This feature adds a chromatic tuner that:
- Activates via FS2 extended longpress (2.0s hold of the third footswitch)
- Shows detected pitch, note name, and cent deviation on the 320x240 LCD
- Mutes audio output while active (audience hears nothing during tuning)
- Deactivates via another FS2 extended longpress, restoring normal UI and audio

## Architecture Overview

Three new components integrated into the existing event loop:

```
FS2 extended longpress (2s) → Modhandler.toggle_tuner()
                                 ├── mute output (ALSA volume → min)
                                 ├── start TunerAudio (JACK client + pitch detection thread)
                                 └── push TunerPanel onto LCD PanelStack

poll_lcd_updates() → TunerPanel reads latest pitch from TunerAudio → redraws

FS2 extended longpress (2s) again → Modhandler.toggle_tuner()
                                       ├── stop TunerAudio
                                       ├── pop TunerPanel
                                       └── restore output volume
```

---

## Component 1: Pitch Detection Engine

**New file: `pistomp/tuner.py`**

### Class: `TunerAudio`

```python
class TunerAudio:
    def __init__(self, sample_rate=48000, buffer_size=4096):
        self.client = None          # JACK client
        self.running = False
        self.note_name = None       # e.g. "A"
        self.octave = None          # e.g. 4
        self.cents = 0.0            # -50.0 to +50.0
        self.frequency = 0.0        # Hz
        self.confidence = 0.0       # 0.0 to 1.0

    def start(self):
        # Create JACK client "pistomp-tuner"
        # Register input port
        # Set process callback (accumulates samples into ring buffer)
        # Activate client
        # Connect to "system:capture_1"
        # Start pitch detection background thread

    def stop(self):
        # Stop background thread
        # Deactivate and close JACK client

    def _detection_thread(self):
        # Loop: read buffer_size samples from ring buffer
        # Run YIN pitch detection
        # Update self.note_name, self.octave, self.cents, self.frequency
        # Sleep briefly between iterations (~50ms target update rate)
```

### Pitch Detection Algorithm: YIN (autocorrelation-based)

Chosen because it's well-suited for monophonic instruments (guitar, bass), works well in pure NumPy, and is CPU-efficient.

Steps:
1. **Difference function**: d(τ) = Σ (x[j] - x[j+τ])²
2. **Cumulative mean normalized difference**: d'(τ) = d(τ) / ((1/τ) * Σ d(k) for k=1..τ)
3. **Absolute threshold**: find first τ where d'(τ) < threshold (0.15)
4. **Parabolic interpolation**: refine τ for sub-sample accuracy
5. **Frequency**: f = sample_rate / τ_refined

Buffer sizing:
- 4096 samples at 48kHz = ~85ms window
- Covers lowest guitar note (low E = 82Hz, period = 585 samples, need ≥2 periods = 1170)
- Covers bass low B (31Hz, period = 1548, need ≥2 = 3096) — fits in 4096

### Note Conversion

```
A4 = 440 Hz reference
semitone_from_A4 = 12 * log2(freq / 440)
nearest_note_index = round(semitone_from_A4)
cents = (semitone_from_A4 - nearest_note_index) * 100
note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
```

### Dependencies

- `jack` (python-jack-client) — JACK audio client
- `numpy` — buffer handling and YIN algorithm math
- Both are lightweight; numpy is likely already available on the Pi image

---

## Component 2: Tuner UI Panel

**Modified file: `pistomp/lcd320x240.py`**

### Full-screen TunerPanel (320×240)

```
┌──────────────────────────────────┐
│            TUNER                 │  ← title (26px font)
│                                  │
│              A                   │  ← note name (80px font, centered)
│              4                   │  ← octave (20px, below note)
│                                  │
│   ◄━━━━━━━━━━━|━━━━━━━━━━━►    │  ← cent meter (colored bar)
│  -50         0          +50     │  ← scale labels
│                                  │
│          440.0 Hz                │  ← frequency (16px)
│    Hold FS3 (2s) to exit        │  ← hint text (small, dim)
└──────────────────────────────────┘
```

### Color coding of the cent meter:
- **Green** (±5 cents): in tune
- **Yellow** (±5–15 cents): close
- **Red** (±15–50 cents): out of tune

### Implementation approach:

Add methods to the existing `Lcd` class in `lcd320x240.py`:

- `tuner_show()` — creates the tuner panel with widgets, pushes onto PanelStack
- `tuner_update(note, octave, cents, frequency, confidence)` — updates widget text/graphics
- `tuner_hide()` — pops tuner panel from PanelStack

The cent meter will be drawn using PIL directly on a widget's draw method (a custom widget or using the existing Widget `_draw()` override pattern).

### Update cycle:

The existing `poll_lcd_updates()` runs every 200ms. When tuner is active, `Modhandler.poll_lcd_updates()` will read the latest pitch data from `TunerAudio` and call `lcd.tuner_update(...)`. This gives ~5 updates/second on the display, which is adequate for a tuner.

---

## Component 3: Handler Integration

**Modified file: `modalapi/modhandler.py`**

### New state and methods on `Modhandler`:

```python
# New instance variables in __init__:
self.tuner_active = False
self.tuner_audio = None
self.tuner_saved_volume = None

# New callback registered in self.callbacks dict:
"toggle_tuner": self.toggle_tuner

# New methods:
def toggle_tuner(self):
    if self.tuner_active:
        self._tuner_deactivate()
    else:
        self._tuner_activate()

def _tuner_activate(self):
    self.tuner_active = True
    # Mute output: save current volume, set to minimum
    self.tuner_saved_volume = self.audiocard.get_volume_parameter(self.audiocard.MASTER)
    self.audiocard.set_volume_parameter(self.audiocard.MASTER, -100, store=False)
    # Start pitch detection
    self.tuner_audio = TunerAudio()
    self.tuner_audio.start()
    # Show tuner UI
    self.lcd.tuner_show()

def _tuner_deactivate(self):
    self.tuner_active = False
    # Stop pitch detection
    if self.tuner_audio:
        self.tuner_audio.stop()
        self.tuner_audio = None
    # Hide tuner UI
    self.lcd.tuner_hide()
    # Restore output volume
    if self.tuner_saved_volume is not None:
        self.audiocard.set_volume_parameter(self.audiocard.MASTER, self.tuner_saved_volume, store=False)
```

### Modified `poll_lcd_updates()`:

When `self.tuner_active` is True, read pitch data from `self.tuner_audio` and pass to LCD:

```python
def poll_lcd_updates(self):
    if self.tuner_active and self.tuner_audio:
        self.lcd.tuner_update(
            self.tuner_audio.note_name,
            self.tuner_audio.octave,
            self.tuner_audio.cents,
            self.tuner_audio.frequency,
            self.tuner_audio.confidence
        )
    if self.lcd:
        self.lcd.poll_updates()
```

---

## Component 4: Configurable Longpress Timing

The tuner uses a 2-second longpress to avoid accidental activation. Currently the longpress threshold is a global 0.5s constant in `analogswitch.py`. This component makes it configurable per footswitch, which is a generally useful enhancement beyond just the tuner.

### Modified file: `common/token.py`

Add new config key constant:

```python
LONGPRESS_TIME = 'longpress_time'
```

### Modified file: `pistomp/analogswitch.py`

Make the longpress threshold an instance parameter instead of a global constant:

```python
LONG_PRESS_TIME = 0.5  # default, kept for backward compat

class AnalogSwitch(analogcontrol.AnalogControl):
    def __init__(self, spi, adc_channel, tolerance, callback,
                 taptempo=None, longpress_time=None):
        ...
        self.longpress_time = longpress_time if longpress_time else LONG_PRESS_TIME

    def refresh(self):
        ...
        if self.duration >= self.longpress_time:   # was: LONG_PRESS_TIME
            self.state = switchstate.Value.LONGPRESSED
            self.callback(switchstate.Value.LONGPRESSED)
```

### Modified file: `pistomp/footswitch.py`

Pass `longpress_time` through to the AnalogSwitch:

```python
def __init__(self, id, led_pin, pixel, midi_CC, midi_channel, midiout,
             refresh_callback, gpio_input=None, adc_input=None, spi=None,
             taptempo=None, longpress_time=None):
    ...
    if adc_input is not None:
        self.adc_switch = analogswitch.AnalogSwitch(
            spi, adc_input, 800, self.pressed,
            taptempo=self.taptempo,
            longpress_time=longpress_time)
```

Also add `"toggle_tuner"` to `all_longpress_groups` dict in `Footswitch.init()`:

```python
cls.all_longpress_groups = {
    "next_snapshot": LongpressInfo(),
    "previous_snapshot": LongpressInfo(),
    "toggle_bypass": LongpressInfo(),
    "set_mod_tap_tempo": LongpressInfo(),
    "toggle_tap_tempo_enable": LongpressInfo(),
    "toggle_tuner": LongpressInfo()           # ← NEW
}
```

### Modified file: `pistomp/hardware.py` — `create_footswitches()`

Read `longpress_time` from config and forward to Footswitch constructor:

```python
longpress_time = Util.DICT_GET(f, Token.LONGPRESS_TIME)

fs = Footswitch.Footswitch(
    id if id else idx, gpio_output, pixel, midi_cc, midi_channel,
    self.midiout, refresh_callback=self.refresh_callback,
    adc_input=adc_input, spi=self.spi,
    taptempo=taptempo,
    longpress_time=longpress_time)        # ← NEW
```

### Modified file: `setup/config_templates/default_config_pistomptre.yml`

Add longpress and custom timing to FS2:

```yaml
- id: 2
  adc_input: 2
  ledstrip_position: 2
  midi_CC: 62
  longpress: toggle_tuner
  longpress_time: 2.0          # ← 2 second hold to activate tuner
```

All other footswitches remain at the 0.5s default (no `longpress_time` key needed).

---

## Component 5: Handler & Abstract Interface

### Modified file: `pistomp/handler.py`

Add abstract method stub (for interface consistency):

```python
def toggle_tuner(self):
    raise NotImplementedError()
```

---

## Files Summary

| File | Action | Purpose |
|------|--------|---------|
| `pistomp/tuner.py` | **Create** | JACK client + YIN pitch detection engine |
| `modalapi/modhandler.py` | Modify | Tuner state, activate/deactivate, poll integration |
| `pistomp/lcd320x240.py` | Modify | Tuner panel UI (show/update/hide) |
| `pistomp/analogswitch.py` | Modify | Per-switch configurable `longpress_time` |
| `pistomp/footswitch.py` | Modify | Pass `longpress_time`, add `toggle_tuner` group |
| `pistomp/hardware.py` | Modify | Read `longpress_time` from config, forward to Footswitch |
| `common/token.py` | Modify | Add `LONGPRESS_TIME` constant |
| `setup/config_templates/default_config_pistomptre.yml` | Modify | Add `longpress: toggle_tuner` + `longpress_time: 2.0` to FS2 |
| `pistomp/handler.py` | Modify | Add `toggle_tuner` abstract method |

---

## Verification Plan

Since there's no automated test suite:

1. **Unit-level check**: Run `python3 -c "from pistomp.tuner import TunerAudio"` to verify the module imports without errors (on a dev machine without JACK, the import should succeed; `start()` would fail gracefully)

2. **UI test**: Use `testui.py` pattern — instantiate the LCD and tuner panel without hardware to verify rendering

3. **On-device test**:
   - Deploy to Pi with `--host mod`
   - Hold FS2 for 2s → tuner panel should appear, output should mute
   - Pluck guitar string → note name and cent meter should respond
   - Hold FS2 for 2s again → normal UI restores, audio unmutes
   - Short press or 0.5s hold of FS2 should NOT activate tuner
   - Verify normal footswitch operation (FS0, FS1, FS3) is unaffected

4. **Edge cases to test**:
   - Activate tuner with no audio input (should show no note / silence state)
   - Activate during pedalboard change
   - JACK client connection failure (graceful error, don't crash)
