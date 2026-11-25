#!/usr/bin/env python3
"""MQTT bridge for a Missyou 5D HoLo fan: shows a battery percentage by playing
the nearest pre-made clip (B000.bin ... B100.bin) and reports the fan's state.

Settings are read from /etc/holo-bridge.conf (or the path given as argument 1).
"""
import configparser, json, re, sys, threading, time, urllib.request
import paho.mqtt.client as mqtt

CONF = sys.argv[1] if len(sys.argv) > 1 else "/etc/holo-bridge.conf"
cfg = configparser.ConfigParser()
if not cfg.read(CONF):
    sys.exit(f"cannot read {CONF}")
M, F = cfg["mqtt"], cfg["fan"]
FAN = "http://" + F.get("host", "192.168.4.1")
BASE = M.get("base_topic", "holo").rstrip("/")
SOC_TOPIC = M["battery_topic"]
JSON_KEY = M.get("json_key", "").strip()
POLL = F.getint("poll_seconds", 30)
A = cfg["alarm"] if cfg.has_section("alarm") else {}
ALARM_BELOW = float(A.get("below", 0))            # 0 disables the alarm
ALARM_CLIP = A.get("clip", "ALARM.bin")
ALARM_SECONDS = float(A.get("seconds", 6))
ALARM_REPEAT = float(A.get("repeat_minutes", 0)) * 60
ALARM_VOLUME = int(A.get("volume", 3))

lock = threading.Lock()
state = {"healthy": False, "model": "", "clips": [], "soc": None, "showing": None, "low": False, "alarm_clip": False}
clips = {}          # percentage -> hex-encoded file name
alarm_hex = None    # hex-encoded name of the alarm clip, if it is on the card
alarm_until = 0.0   # the alarm clip is playing until this time
last_alarm = 0.0
client = None


def log(*a):
    print(*a, flush=True)


def fan_get(path):
    with urllib.request.urlopen(FAN + path, timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def refresh():
    """Open a session, read model and clip list. Returns True if the fan is up."""
    global clips, alarm_hex
    try:
        fan_get("/ctrl/session")
        info = fan_get("/info")
        files = fan_get("/DCIM").get("files", [])
    except Exception as e:
        state.update(healthy=False, model="", clips=[])
        log("fan not reachable:", e)
        return False
    found, alarm = {}, None
    for h in files:
        try:
            name = bytes.fromhex(h).decode("utf-8", "replace")
        except ValueError:
            continue
        m = re.fullmatch(r"B(\d{3})\.bin", name)
        if m:
            found[int(m.group(1))] = h
        if name == ALARM_CLIP:
            alarm = h
    clips, alarm_hex = found, alarm
    state["alarm_clip"] = alarm is not None
    state.update(model=info.get("model", ""), clips=sorted(found))
    state["healthy"] = bool(state["model"]) and bool(found)
    return state["healthy"]


def show(force=False):
    """Play the clip nearest to the current reading, if it differs from what is showing."""
    soc = state["soc"]
    if soc is None or not state["healthy"] or time.time() < alarm_until:
        return
    target = min(clips, key=lambda v: (abs(v - soc), -v))
    if target == state["showing"] and not force:
        return
    try:
        fan_get("/ctrl/session")
        fan_get("/ctrl/set?loop=one")
        reply = fan_get(f"/DCIM/{clips[target]}?act=play")
        if reply.get("code") == 0:
            state["showing"] = target
            log(f"battery {soc:.1f}% -> clip {target}")
        else:
            log("fan refused play:", reply)
    except Exception as e:
        state["healthy"] = False
        log("play failed:", e)


def check_alarm():
    """Play the alarm clip when the reading drops below the threshold, then go back to the number."""
    global alarm_until, last_alarm
    soc = state["soc"]
    if not ALARM_BELOW or soc is None:
        return
    now = time.time()
    if soc >= ALARM_BELOW + 2:                      # 2-point margin so it does not chatter
        state["low"] = False
        return
    if soc >= ALARM_BELOW:
        return
    due = not state["low"] or (ALARM_REPEAT and now - last_alarm >= ALARM_REPEAT)
    state["low"] = True
    if not due or not state["healthy"] or alarm_hex is None or now < alarm_until:
        return
    try:
        fan_get("/ctrl/session")
        fan_get(f"/ctrl/set?volum={ALARM_VOLUME}")
        fan_get("/ctrl/set?loop=one")
        if fan_get(f"/DCIM/{alarm_hex}?act=play").get("code") == 0:
            last_alarm, alarm_until = now, now + ALARM_SECONDS
            state["showing"] = "alarm"
            log(f"battery {soc:.1f}% is below {ALARM_BELOW:g}% -> alarm")
            threading.Timer(ALARM_SECONDS + 0.2, after_alarm).start()
    except Exception as e:
        log("alarm failed:", e)


def after_alarm():
    with lock:
        show(force=True)
        publish_state()


def publish_state():
    client.publish(f"{BASE}/state", json.dumps(state), retain=True)


def parse(payload):
    text = payload.decode("utf-8", "replace").strip()
    if JSON_KEY:
        value = json.loads(text)
        for part in JSON_KEY.split("."):
            value = value[part]
    else:
        value = text
    return max(0.0, min(100.0, float(value)))


def on_connect(c, userdata, flags, rc, props=None):
    log("mqtt connected")
    c.subscribe([(SOC_TOPIC, 0), (f"{BASE}/cmd/#", 0)])
    c.publish(f"{BASE}/status", "online", retain=True)


def on_message(c, userdata, msg):
    with lock:
        try:
            if msg.topic == SOC_TOPIC:
                state["soc"] = parse(msg.payload)
                check_alarm()
                show()
            elif msg.topic == f"{BASE}/cmd/brightness":
                level = int(msg.payload)
                if 1 <= level <= 3:
                    fan_get("/ctrl/session"); fan_get(f"/ctrl/set?brightness={level}")
            elif msg.topic == f"{BASE}/cmd/volume":
                level = int(msg.payload)
                if 1 <= level <= 3:
                    fan_get("/ctrl/session"); fan_get(f"/ctrl/set?volum={level}")
            elif msg.topic == f"{BASE}/cmd/display":
                word = msg.payload.decode().strip().lower()
                if word in ("on", "off"):
                    fan_get("/ctrl/session"); fan_get(f"/ctrl/set?switcher={word}")
            else:
                return
        except Exception as e:
            log(f"ignored message on {msg.topic}:", e)
        publish_state()


def poll_loop():
    while True:
        with lock:
            was = state["healthy"]
            ok = refresh()
            if ok and not was:
                log(f"fan up: {state['model']}, clips {state['clips']}")
                state["showing"] = None
                show(force=True)
            elif was and not ok:
                log("fan went away or did not start properly")
            publish_state()
        time.sleep(POLL)


def main():
    global client
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=M.get("client_id", "holo-bridge"))
    except AttributeError:                      # paho-mqtt 1.x
        client = mqtt.Client(client_id=M.get("client_id", "holo-bridge"))
    if M.get("username", ""):
        client.username_pw_set(M["username"], M.get("password", ""))
    client.will_set(f"{BASE}/status", "offline", retain=True)
    client.on_connect, client.on_message = on_connect, on_message
    client.reconnect_delay_set(1, 60)
    client.connect_async(M["host"], M.getint("port", 1883), keepalive=60)
    client.loop_start()
    poll_loop()


if __name__ == "__main__":
    main()
