# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

pi-Stomp is a DIY multi-effects stompbox platform for guitar, bass, and keyboards built on Raspberry Pi. It combines a custom Python service layer with the MOD audio host (moddevices.com) to drive hardware controls (footswitches, encoders, LCD, MIDI) and manage audio effects pedalboards.

**License**: GPL v3 — all Python source files carry the GPL header.

## Running the Application

The main entry point is `modalapistomp.py`, which runs as a systemd service (`mod-ala-pi-stomp.service`) on the target Raspberry Pi:

```bash
# Run with default (mod) host
python3 modalapistomp.py

# Run with debug logging
python3 modalapistomp.py --log debug

# Run in test mode (no MOD dependency)
python3 modalapistomp.py --host test

# Run in generic MIDI mode (no LCD)
python3 modalapistomp.py --host generic
```

There is no formal test suite (pytest/unittest). Manual test scripts exist:
- `testui.py` — LCD/UI testing with simulated encoder input
- `pistomp-testui.py` — alternative UI test

## Installation

Full system installation uses pre-built images from [pi-gen-pistomp](https://github.com/TreeFallSound/pi-gen-pistomp). The `setup.sh` script (now deprecated for v3) orchestrates package/driver/plugin installation for building from scratch.

## Architecture

### Main Event Loop (`modalapistomp.py`)

A polling loop with ~10ms period drives all hardware and UI:

```
poll_controls()       — every 10ms (footswitches, encoders, analog)
poll_indicators()     — every 20ms (LEDs)
poll_lcd_updates()    — every 200ms
poll_modui_changes()  — every 1s (sync with MOD web UI)
poll_wifi()           — every 2s
poll_system_info()    — every 60s
```

### Factory-Driven Initialization

Three singleton factories create the appropriate objects based on `hardware.version` in the YAML config:

| Factory | Creates | Version Mapping |
|---------|---------|-----------------|
| `Hardwarefactory` | Hardware driver | 1.x→Pistomp, 2.x→Pistompcore, 3.x→Pistomptre |
| `Handlerfactory` | Handler/host | 1.x→Mod, 2.x+→Modhandler |
| `Audiocardfactory` | Audio card driver | Based on detected sound card |

All three are singletons that raise an exception if instantiated more than once.

### Hardware Abstraction (`pistomp/`)

`Hardware` (abstract base in `hardware.py`) defines the interface. Three concrete implementations handle different board revisions:

- `pistomp.py` — v1: 3 footswitches, 2 encoders, 128x64 monochrome LCD
- `pistompcore.py` — v2 (Core): 3 footswitches, 2 encoders, 320x240 color LCD
- `pistomptre.py` — v3 (Tre): 4 footswitches, 3 encoders, 320x240 color LCD, WS2812 LED strip

Hardware objects own: footswitches, encoders, analog controls, relay, LCD, LED strip, tap tempo. They are created from YAML config via `create_footswitches()`, `create_analog_controls()`, `create_encoders()`.

### Handler Pattern (`pistomp/handler.py`)

`Handler` is the abstract interface. Implementations:

- `modalapi/modhandler.py` (`Modhandler`) — full MOD audio integration with LCD, pedalboard management, MIDI routing. This is the primary handler for v2+.
- `modalapi/mod.py` (`Mod`) — v1 handler (monolithic, ~1300 lines). Contains core MOD integration logic; `Modhandler` inherits from it indirectly via shared patterns but is a separate singleton.
- `pistomp/generichost.py` — MIDI-only mode, no LCD
- `pistomp/testhost.py` — development/testing

### Callback System

Handlers expose named callbacks via a dictionary, used by config-driven longpress actions:

```python
self.callbacks = {
    "set_mod_tap_tempo": self.set_mod_tap_tempo,
    "next_snapshot": self.preset_incr_and_change,
    "previous_snapshot": self.preset_decr_and_change,
    "toggle_bypass": self.system_toggle_bypass,
    "toggle_tap_tempo_enable": self.toggle_tap_tempo_enable
}
```

Config YAML references these by name (e.g., `longpress: previous_snapshot`), and `handler.get_callback()` resolves them at runtime.

### Current Pedalboard State (`Modhandler.Current`)

`Modhandler` uses an inner `Current` class to hold dynamic data for the active pedalboard (presets, analog controller mappings). This object is replaced entirely on pedalboard change — old one is deleted, new one created via `set_current_pedalboard()`.

### MOD Integration (`modalapi/`)

Communicates with mod-host/mod-ui via HTTP requests to `localhost:80`. Key responsibilities:
- Pedalboard loading/switching and snapshot (preset) management
- Plugin parameter control via lilv (LV2 plugin library)
- MIDI CC mapping between hardware controls and plugin parameters
- Synchronizing state with the MOD web UI

Pedalboard change detection: `poll_modui_changes()` monitors the mtime of `/home/pistomp/data/last.json` rather than using events.

### MIDI CC Control Flow

Hardware controls map to MIDI CC messages. The handler maintains a `controllers` dict keyed by `"channel:cc"` string for parameter control. When a footswitch/encoder/analog sends a CC, the handler looks up the bound plugin parameter and sends an HTTP request to MOD.

### UI Framework (`uilib/`)

Custom widget framework for LCD rendering using Pillow (PIL). `widget.py` is the base class. Includes panels, menus, dialogs, text, icons, and footswitch indicators. UI config is JSON-based (`ui/config.json`). Widgets inherit visual attributes from parents (cached on `show()`/`attach()`). Navigation uses a `PanelStack` for push/pop.

### Configuration System

- YAML config files in `setup/config_templates/` define hardware layout (GPIO pins, ADC channels, MIDI CCs, longpress actions)
- Runtime config loads from `/home/pistomp/data/config/default_config.yml` on the target device
- `pistomp/config.py` validates config against a JSON schema
- Per-pedalboard config overrides are supported via `config.yml` in pedalboard directories; merged via `reinit()`
- `common/token.py` defines all config key constants (FOOTSWITCHES, ENCODERS, MIDI_CC, etc.)

### Key Python Dependencies

```
rtmidi (python-rtmidi)   # MIDI I/O
gpiozero                 # GPIO control
spidev                   # SPI (LCD + ADC MCP3008)
lilv                     # LV2 plugin library
pyyaml                   # YAML config
jsonschema               # Config validation
Pillow (PIL)             # LCD image rendering
```

### System Dependencies

MOD ecosystem: mod-host, mod-ui, JACK audio, ALSA. The application expects these services running on the target Pi.

## Key Directories

| Directory | Purpose |
|-----------|---------|
| `pistomp/` | Hardware drivers, control abstractions, config, factories |
| `modalapi/` | MOD audio host integration, pedalboard/plugin management |
| `uilib/` | LCD UI widget framework |
| `common/` | Shared constants (`token.py`) and utilities (`util.py`) |
| `setup/` | Installation scripts, systemd services, config templates |
| `util/` | Runtime utilities (MIDI monitor, backup/restore, version tools) |
| `images/`, `fonts/` | Graphics assets and TrueType fonts for LCD |

## Hardware Notes

- SPI bus 0, CE1 is shared between the MCP3008 ADC (240kHz) and color LCD (24MHz)
- Footswitches can use either GPIO or ADC input; v3 uses ADC exclusively
- MIDI channel in config is 1-indexed but internally decremented by 1 (compensates for a MOD bug)
- Hardware self-test runs on first boot, creates a `.hardware_tests_passed` sentinel file
- WS2812 LED strip on v3 uses PIO neopixel (Pi5 compatible)
- Relay bypass state persisted via `~/.relay_bypass<pin>` sentinel files
- Footswitches support longpress groups: pressing multiple switches in a group simultaneously (within 0.4s) triggers a group callback
