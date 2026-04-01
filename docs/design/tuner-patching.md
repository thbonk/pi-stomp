# Tuner Feature — Deployment Plan

## Goal

Deploy the tuner feature (from the `pistomp-v3-tuner` branch) to the running pi-stomp device at `pistomp.local` by patching the installed code in place.

## Prerequisites

- Device reachable at `pistomp.local` (192.168.178.158)
- SSH credentials: `pistomp` / `pistomp`
- Installed code lives at `/home/pistomp/pi-stomp/`
- Service: `mod-ala-pi-stomp.service` (systemd, auto-restarts)
- Runtime config: `/home/pistomp/data/config/default_config.yml`

All commands below run from the repo root on the dev Mac.

---

## Step 0: Set Up SSH Key Auth (one-time)

```bash
ssh-copy-id pistomp@pistomp.local
```

Enter password `pistomp` once when prompted.

Verify passwordless access:

```bash
ssh pistomp@pistomp.local "echo ok"
```

---

## Step 1: Backup Installed Code

```bash
ssh pistomp@pistomp.local "tar czf /home/pistomp/pi-stomp-backup-\$(date +%Y%m%d-%H%M%S).tar.gz -C /home/pistomp pi-stomp/"
```

```bash
ssh pistomp@pistomp.local "cp /home/pistomp/data/config/default_config.yml /home/pistomp/data/config/default_config.yml.bak"
```

Verify:

```bash
ssh pistomp@pistomp.local "ls -lh /home/pistomp/pi-stomp-backup-*.tar.gz /home/pistomp/data/config/default_config.yml.bak"
```

---

## Step 2: Stop the Service

```bash
ssh pistomp@pistomp.local "sudo systemctl stop mod-ala-pi-stomp"
```

```bash
ssh pistomp@pistomp.local "systemctl is-active mod-ala-pi-stomp || true"
```

Expected output: `inactive`

---

## Step 3: Check Python Dependencies

```bash
ssh pistomp@pistomp.local "python3 -c 'import numpy; print(\"numpy\", numpy.__version__)'"
```

```bash
ssh pistomp@pistomp.local "python3 -c 'import jack; print(\"jack ok\")'"
```

If either fails, install:

```bash
ssh pistomp@pistomp.local "pip3 install numpy python-jack-client"
```

---

## Step 4: Deploy Changed Files

9 files total (8 modified, 1 new):

```bash
scp pistomp/tuner.py pistomp/analogswitch.py pistomp/footswitch.py pistomp/handler.py pistomp/hardware.py pistomp/lcd320x240.py pistomp@pistomp.local:/home/pistomp/pi-stomp/pistomp/
```

```bash
scp common/token.py pistomp@pistomp.local:/home/pistomp/pi-stomp/common/
```

```bash
scp modalapi/modhandler.py pistomp@pistomp.local:/home/pistomp/pi-stomp/modalapi/
```

Verify syntax on device:

```bash
ssh pistomp@pistomp.local "python3 -c 'import py_compile; py_compile.compile(\"/home/pistomp/pi-stomp/pistomp/tuner.py\", doraise=True)' && echo 'tuner.py OK'"
```

```bash
ssh pistomp@pistomp.local "python3 -c 'import py_compile; py_compile.compile(\"/home/pistomp/pi-stomp/modalapi/modhandler.py\", doraise=True)' && echo 'modhandler.py OK'"
```

```bash
ssh pistomp@pistomp.local "python3 -c 'import py_compile; py_compile.compile(\"/home/pistomp/pi-stomp/pistomp/lcd320x240.py\", doraise=True)' && echo 'lcd320x240.py OK'"
```

```bash
ssh pistomp@pistomp.local "python3 -c 'import py_compile; py_compile.compile(\"/home/pistomp/pi-stomp/pistomp/footswitch.py\", doraise=True)' && echo 'footswitch.py OK'"
```

---

## Step 5: Patch Runtime Config

The original FS2 block in the runtime config looks like:

```yaml
  - id: 2
    adc_input: 2
    ledstrip_position: 2
    midi_CC: 62
```

We need to insert `longpress: toggle_tuner` and `longpress_time: 2.0` after the `midi_CC: 62` line.

First, confirm the current state matches expectations:

```bash
ssh pistomp@pistomp.local "grep -A4 'id: 2' /home/pistomp/data/config/default_config.yml"
```

Expected: the block above, with NO existing `longpress` or `longpress_time` lines for FS2.

Apply the patch (insert two lines after `midi_CC: 62`):

```bash
ssh pistomp@pistomp.local "sed -i '/    midi_CC: 62$/a\\    longpress: toggle_tuner\n    longpress_time: 2.0' /home/pistomp/data/config/default_config.yml"
```

Verify the result:

```bash
ssh pistomp@pistomp.local "grep -A6 'id: 2' /home/pistomp/data/config/default_config.yml"
```

Expected output:

```
  - id: 2
    adc_input: 2
    ledstrip_position: 2
    midi_CC: 62
    longpress: toggle_tuner
    longpress_time: 2.0
```

Validate YAML parses correctly:

```bash
ssh pistomp@pistomp.local "python3 -c \"import yaml; yaml.safe_load(open('/home/pistomp/data/config/default_config.yml')); print('YAML OK')\""
```

---

## Step 6: Restart the Service

```bash
ssh pistomp@pistomp.local "sudo systemctl start mod-ala-pi-stomp"
```

```bash
sleep 3
```

```bash
ssh pistomp@pistomp.local "systemctl is-active mod-ala-pi-stomp"
```

Expected output: `active`

```bash
ssh pistomp@pistomp.local "journalctl -u mod-ala-pi-stomp -n 30 --no-pager"
```

Look for: successful startup, no tracebacks, no import errors.

---

## Step 7: Test on Device

1. **Normal operation** — verify footswitches, encoders, LCD work as before
2. **Tuner activation** — hold FS2 (third footswitch) for 2 seconds; tuner panel should appear, audio should mute
3. **Pitch detection** — pluck a string; note name and cent meter should respond
4. **Tuner deactivation** — hold FS2 for 2 seconds again; normal UI restores, audio unmutes
5. **No false triggers** — short press of FS2 should behave normally

Live log monitoring during test:

```bash
ssh pistomp@pistomp.local "journalctl -u mod-ala-pi-stomp -f --no-pager"
```

---

## Rollback

If anything goes wrong, restore from backup:

```bash
ssh pistomp@pistomp.local "sudo systemctl stop mod-ala-pi-stomp"
```

```bash
ssh pistomp@pistomp.local "ls /home/pistomp/pi-stomp-backup-*.tar.gz"
```

Use the timestamp from the listing:

```bash
ssh pistomp@pistomp.local "cd /home/pistomp && tar xzf pi-stomp-backup-YYYYMMDD-HHMMSS.tar.gz"
```

```bash
ssh pistomp@pistomp.local "cp /home/pistomp/data/config/default_config.yml.bak /home/pistomp/data/config/default_config.yml"
```

```bash
ssh pistomp@pistomp.local "sudo systemctl start mod-ala-pi-stomp"
```
