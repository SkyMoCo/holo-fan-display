#!/usr/bin/env python3
"""Make battery-percentage clips for a Missyou F-MINI12 hologram fan.

Layout measured from an app-made clip for this fan:
  header copied from a stock clip (740 bytes), then per frame
  300 lines x 5 bit planes x 18 bytes (48 LEDs x 3 colours) + 1,600 audio bytes,
  then 100,000 zero bytes. 20 frames per second.
"""
import argparse, colorsys, math, os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

LIGHTS, LINES, DEPTH = 48, 300, 5
HEADER_BYTES, AUDIO_BYTES, TRAILER_BYTES, FPS = 740, 1600, 100_000, 20
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
LUT = np.frombuffer(bytes.fromhex(
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0101010101010101010101020202020202020203030303030304040404040405"
    "05050606060707080809090a0a0b0c0c0d0d0e0e0f1011111213141516171819"
    "1a1b1c1d1d1e1f2021222324252628292b2c2d2f31333537383a3b3d3e3f4042"
    "4446484a4d4f515355585a5d606265676a6c6f7275787b7e8285888c8f92969a"
    "9da0a4a7aaadb0b4b7babec1c4c7cacdd0d3d6d9dcdee0e2e4e6e8eaeceeeff0"
    "f1f2f3f4f5f6f7f8f9f9fafafbfbfcfcfdfdfdfefefefeffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"), dtype=np.uint8)


GREEN_AT, RED_AT = 80, 25


def colour_for(pct):
    """Green at 80 and above, red at 25 and below, fading through yellow in between."""
    t = min(1.0, max(0.0, (pct - RED_AT) / (GREEN_AT - RED_AT)))
    r, g, b = colorsys.hsv_to_rgb(t / 3, 1.0, 1.0)          # hue 0 = red, 1/3 = green
    return (round(r * 255), round(g * 255), round(b * 255))


def draw_image(pct=None, ring=True, big=8, text=None, colour=None):
    """Return a 96x96 RGB array: a number with a gauge ring, or any short text."""
    size = 2 * LIGHTS * big
    img = Image.new("RGB", (size, size), (0, 0, 0))
    d = ImageDraw.Draw(img)
    col = colour or colour_for(pct)
    if text is None:
        text = str(pct)
        if ring and pct > 0:
            d.arc([0, 0, size - 1, size - 1], -90, -90 + 360 * pct / 100, fill=col, width=5 * big)
    else:
        ring = False
    room = (LIGHTS - (8 if ring else 2)) * big          # radius left for the text
    fs = int(size * 0.8)
    while True:
        font = ImageFont.truetype(FONT, fs)
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        if math.hypot((r - l) / 2, (b - t) / 2) <= room * 1.06 or fs <= 12:
            break
        fs -= 4
    d.text(((size - (r - l)) / 2 - l, (size - (b - t)) / 2 - t), text, font=font, fill=col)
    small = img.resize((2 * LIGHTS, 2 * LIGHTS), Image.LANCZOS)
    return np.asarray(small, dtype=np.uint8)


def encode_frame(rgb, rotation_deg=180.0, hflip=False):
    """Polar-sample a square RGB image and pack one display frame."""
    h, w, _ = rgb.shape
    if hflip:
        rgb = rgb[:, ::-1]
    cx, cy = (w - 1) / 2, (h - 1) / 2
    ang = np.arange(LINES) * (2 * math.pi / LINES) + math.radians(rotation_deg)
    rad = np.arange(LIGHTS - 1, -1, -1, dtype=np.float64) + 0.5   # outer LED first
    mx = cx + np.cos(ang)[:, None] * rad[None, :]
    my = cy + np.sin(ang)[:, None] * rad[None, :]
    x0 = np.floor(mx).astype(int); y0 = np.floor(my).astype(int)
    dx = (mx - x0)[..., None]; dy = (my - y0)[..., None]
    c = lambda a, hi: np.clip(a, 0, hi)
    f = rgb.astype(np.float64)
    interp = (f[c(y0, h-1), c(x0, w-1)] * (1-dx) * (1-dy) + f[c(y0, h-1), c(x0+1, w-1)] * dx * (1-dy)
              + f[c(y0+1, h-1), c(x0, w-1)] * (1-dx) * dy + f[c(y0+1, h-1), c(x0+1, w-1)] * dx * dy)
    vals = LUT[interp.astype(np.uint8)][:, :, (2, 1, 0)].reshape(LINES, LIGHTS * 3)   # fan order is B, G, R
    planes = [np.packbits((vals >> (7 - k)) & 1, axis=1, bitorder="big") for k in range(DEPTH)]
    return np.stack(planes, axis=1).tobytes()        # line, plane, 18 bytes


def write_clip(path, header, frames, audio=None):
    """frames: list of packed display frames. audio: unsigned 8-bit mono at 32 kHz, or None."""
    need = len(frames) * AUDIO_BYTES
    pcm = np.full(need, 128, dtype=np.uint8)
    if audio is not None:
        pcm[:min(need, len(audio))] = audio[:need]
    with open(path, "wb") as f:
        f.write(header)
        for i, frame in enumerate(frames):
            f.write(frame)
            f.write(pcm[i * AUDIO_BYTES:(i + 1) * AUDIO_BYTES].tobytes())
        f.write(b"\x00" * TRAILER_BYTES)


def beeps(seconds, tone_hz=1200, level=95):
    """Half a second of tone, half a second of silence, repeated."""
    rate = AUDIO_BYTES * FPS
    t = np.arange(int(seconds * rate)) / rate
    phase = t % 1.0
    gate = np.clip(np.minimum(phase, 0.5 - phase) / 0.01, 0, 1)      # 10 ms fades, no clicks
    return (128 + level * gate * np.sin(2 * math.pi * tone_hz * t)).round().astype(np.uint8)


def load_audio(path):
    """Convert any sound file to the fan's format with ffmpeg."""
    import subprocess
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", os.path.expanduser(path), "-ac", "1",
                          "-ar", str(AUDIO_BYTES * FPS), "-f", "u8", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("values", nargs="*", type=int, help="percentages to make, e.g. 5 57 100")
    p.add_argument("--all", action="store_true", help="make every value from 0 to 100")
    p.add_argument("--stock", required=True, help="an app-made clip from this fan; its header is copied")
    p.add_argument("--out", default="battery", help="output folder")
    p.add_argument("--seconds", type=float, default=3.0, help="clip length")
    p.add_argument("--rotation-deg", type=float, default=180.0)
    p.add_argument("--hflip", action="store_true", help="mirror left-right")
    p.add_argument("--no-ring", action="store_true", help="number only, no gauge ring")
    p.add_argument("--preview", action="store_true", help="also save a PNG of each source image")
    p.add_argument("--alarm", action="store_true", help="also make ALARM.bin: flashing text with sound")
    p.add_argument("--alarm-text", default="LOW")
    p.add_argument("--alarm-seconds", type=float, default=6.0)
    p.add_argument("--alarm-audio", help="sound file for the alarm instead of beeps (any format ffmpeg reads)")
    a = p.parse_args()
    values = list(range(101)) if a.all else a.values
    if not (values or a.alarm) or any(v < 0 or v > 100 for v in values):
        sys.exit("give percentages between 0 and 100, or --all")
    with open(os.path.expanduser(a.stock), "rb") as f:
        header = f.read(HEADER_BYTES)
    if len(header) != HEADER_BYTES or header[:2] != b"\xee\x37":
        sys.exit("the stock clip does not look like an EE 37 file")
    out = os.path.expanduser(a.out)
    os.makedirs(out, exist_ok=True)
    for v in values:
        rgb = draw_image(v, ring=not a.no_ring)
        if a.preview:
            Image.fromarray(rgb).resize((384, 384), Image.NEAREST).save(os.path.join(out, f"B{v:03d}.png"))
        frame = encode_frame(rgb, a.rotation_deg, a.hflip)
        write_clip(os.path.join(out, f"B{v:03d}.bin"), header, [frame] * int(round(a.seconds * FPS)))
    if a.alarm:
        if a.alarm_audio:
            audio = load_audio(a.alarm_audio)
            seconds = max(a.alarm_seconds, len(audio) / (AUDIO_BYTES * FPS))
        else:
            seconds = a.alarm_seconds
            audio = beeps(seconds)
        on = encode_frame(draw_image(text=a.alarm_text, colour=(255, 0, 0)), a.rotation_deg, a.hflip)
        off = encode_frame(np.zeros((2 * LIGHTS, 2 * LIGHTS, 3), dtype=np.uint8))
        n = int(math.ceil(seconds * FPS))
        frames = [on if (i % FPS) < FPS // 2 else off for i in range(n)]
        write_clip(os.path.join(out, "ALARM.bin"), header, frames, audio)
        print(f"wrote ALARM.bin ({seconds:.1f} s)")
    print(f"wrote {len(values)} clip(s) to {out}")


if __name__ == "__main__":
    main()
