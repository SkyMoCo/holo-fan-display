# holo-fan-display

Shows a battery percentage on a Missyou "5D HoLo" hologram fan (model F-MINI12), driven by MQTT.

The fan cannot draw live data. It plays pre-made clips from its SD card, so this project has two parts:

- `mini12.py` makes one clip per percentage value (`B000.bin` to `B100.bin`) and an optional alarm clip with sound.
  Numbers are green at 80% and above, red at 25% and below, and fade through yellow in between.
- `holo_bridge.py` listens to an MQTT topic and tells the fan to play the clip nearest to the latest reading.

Tested on one F-MINI12 with firmware 1.1.0. Other models use different layouts.

## Hardware setup

The fan runs its own Wi-Fi network (`5D_xxxx`) and answers HTTP at `192.168.4.1`. The bridge host needs two network paths: Wi-Fi to the fan and another route (Ethernet here) to the MQTT broker. A Raspberry Pi 4 with NetworkManager:

```
nmcli device wifi connect "5D_4859" password "<password>" ifname wlan0
nmcli connection modify "5D_4859" ipv4.never-default yes ipv6.method disabled
```

## Making clips

Requirements: `python3-numpy`, `python3-pil`, and `ffmpeg` only if you use your own alarm sound.

The header is copied from an app-made clip that came with the fan. Header length varies between clips; `mini12.py` expects one with a 740-byte header (`06Jellyfish.bin` on this fan).

```
python3 mini12.py --stock fan-backup/06Jellyfish.bin --out battery --hflip $(seq 0 5 100)
python3 mini12.py --stock fan-backup/06Jellyfish.bin --out battery --hflip --alarm
```

Useful options: `--hflip` (this fan needed it), `--rotation-deg`, `--no-ring`, `--seconds`, `--alarm-text`, `--alarm-audio FILE`.

Copy the clips to the root of the fan's SD card (FAT32). Do the copy on Linux and `sync && umount` before removing the card.

**File limit:** this fan started normally with 21 clips on the card and failed to start with 51 or more. When it fails, `/info` returns an empty model name and `/DCIM` lists nothing. The exact limit is not known.

## Running the bridge

Requirements: `python3-paho-mqtt`.

```
install -m 755 holo_bridge.py /usr/local/bin/
install -m 600 -o kevins holo-bridge.conf.example /etc/holo-bridge.conf
install -m 644 holo-bridge.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now holo-bridge
```

Edit `/etc/holo-bridge.conf` for your broker, battery topic and payload format. The service runs as the user named in `holo-bridge.service`.

### MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| your `battery_topic` | in | a number, or JSON with the value at `json_key` |
| `holo/status` | out | `online` or `offline` (retained, last will) |
| `holo/state` | out | JSON: `healthy`, `model`, `clips`, `soc`, `showing`, `low` |
| `holo/cmd/brightness` | in | `1` to `3` |
| `holo/cmd/volume` | in | `1` to `3` |
| `holo/cmd/display` | in | `on` or `off` |

With `[alarm] below` set, the bridge plays `ALARM.bin` once when the reading drops below the threshold, then returns to the number. It re-arms 2 points above the threshold.

## Clip format (F-MINI12)

Worked out by decoding an app-made clip into a recognisable image.

| Part | Size |
|---|---|
| Header, starts `EE 37` | varies per clip (740 bytes in the one used here) |
| Frame: display data | 27,000 bytes = 300 lines x 5 bit planes x 18 bytes |
| Frame: audio | 1,600 bytes, unsigned 8-bit mono |
| Trailer | 100,000 zero bytes |

- 48 LEDs on one blade, 300 lines per revolution, 20 frames per second.
- Each 18-byte plane holds 48 LEDs x 3 colours, outermost LED first, colour order R, B, G, most significant plane first.
- Audio is assumed to be 32,000 samples per second (1,600 per frame at 20 fps).
- The header is scrambled with a per-file one-byte XOR key; headers from different clips are the same underneath.

## HTTP control

| Request | Purpose |
|---|---|
| `GET /ctrl/session` | open a control session |
| `GET /info` | model and firmware |
| `GET /DCIM` | list clips (names are hex-encoded) |
| `GET /DCIM/<hex name>?act=play` | play a clip |
| `GET /ctrl/get?k=all` | read all settings |
| `GET /ctrl/set?loop=one\|all` | loop mode |
| `GET /ctrl/set?brightness=1..3` | brightness |
| `GET /ctrl/set?volum=1..3` | volume |
| `GET /ctrl/set?switcher=on\|off` | LEDs on or off |

## Credits

- Protocol notes: [daveismith/beancell-holo](https://github.com/daveismith/beancell-holo).
- Brightness lookup table and the starting point for the format work: [zackmcmurrin-dev/holo-fan-linux](https://github.com/zackmcmurrin-dev/holo-fan-linux) (MIT). See `THIRD_PARTY.md`.

This is an unofficial project, not affiliated with Missyou.
