import os
import ssl
import time
import json
import hmac
import socket
import threading
import httpx
import websocket
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, Response
import random
import string
import base64
import hashlib
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

API_BASE = "https://iot-api.netprisma.us"
MQTT_URL = "wss://iot-south.netprisma.us:8443/ws/v2"

# NETPRISMA_EMAIL = os.environ["NETPRISMA_EMAIL"]
# NETPRISMA_LOGIN_PWD = os.environ["NETPRISMA_LOGIN_PWD"]
# NETPRISMA_LOGIN_RANDOM = os.environ["NETPRISMA_LOGIN_RANDOM"]
# NETPRISMA_LOGIN_SIGNATURE = os.environ["NETPRISMA_LOGIN_SIGNATURE"]
# NETPRISMA_USER_DOMAIN = os.environ["NETPRISMA_USER_DOMAIN"]
NETPRISMA_EMAIL = os.environ["NETPRISMA_EMAIL"]
NETPRISMA_PASSWORD = os.environ["NETPRISMA_PASSWORD"]

NETPRISMA_USER_DOMAIN = os.environ.get(
    "NETPRISMA_USER_DOMAIN",
    "U.SP.8589934603",
).strip()

NETPRISMA_USER_DOMAIN_SECRET = os.environ.get(
    "NETPRISMA_USER_DOMAIN_SECRET",
    "",
).strip()

if not NETPRISMA_USER_DOMAIN_SECRET:
    raise RuntimeError(
        "NETPRISMA_USER_DOMAIN_SECRET is required. "
        "Set it in .env. Do not commit your .env file."
    )
UID = os.environ.get("NETPRISMA_UID", "").strip()
FID = os.environ.get("NETPRISMA_FID", "").strip()
PRODUCT_KEY = os.environ.get("NETPRISMA_PRODUCT_KEY", "").strip()

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "admin")

# Number of fan speeds the UI exposes. OmniBreeze sells 3- and 5-speed models;
# default 3 preserves prior behavior. The device/command API accepts 1-12.
try:
    FAN_SPEED_COUNT = int(os.environ.get("FAN_SPEED_COUNT", "3"))
except ValueError:
    FAN_SPEED_COUNT = 3
FAN_SPEED_COUNT = max(1, min(12, FAN_SPEED_COUNT))

BUSINESS_REFRESH_TTL = 15
def generate_login_random() -> str:
    chars = string.digits + string.ascii_uppercase[:25] + string.ascii_lowercase[:25]
    return "".join(random.SystemRandom().choice(chars) for _ in range(16))


def md5_upper(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def sha256_lower(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encrypt_netprisma_password(password: str, random_value: str) -> str:
    # Java:
    # key_source = MD5(random).upper().substring(8, 24)
    # iv = key_source[8:16] + key_source[0:8]
    key_source = md5_upper(random_value)[8:24]
    iv = key_source[8:16] + key_source[0:8]

    cipher = AES.new(
        key_source.encode("utf-8"),
        AES.MODE_CBC,
        iv.encode("utf-8"),
    )

    encrypted = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("utf-8")


def build_login_payload(email: str, password: str) -> dict:
    random_value = generate_login_random()
    encrypted_pwd = encrypt_netprisma_password(password, random_value)

    signature = sha256_lower(
        email
        + encrypted_pwd
        + random_value
        + NETPRISMA_USER_DOMAIN_SECRET
    )

    return {
        "email": email,
        "pwd": encrypted_pwd,
        "random": random_value,
        "signature": signature,
        "userDomain": NETPRISMA_USER_DOMAIN,
    }

def jwt_payload(token: str) -> dict:
    if not token:
        return {}

    if token.startswith("Bearer "):
        token = token.split(" ", 1)[1]

    try:
        middle = token.split(".")[1]
        middle += "=" * (-len(middle) % 4)
        return json.loads(base64.urlsafe_b64decode(middle.encode()).decode())
    except Exception:
        return {}

app = Flask(__name__)

STATE_LOCK = threading.Lock()
STATES = {}

LISTENER_STATUS = {
    "running": False,
    "last_error": None,
    "last_connect": None,
}

ACTIONS = {
    "on": ("Power On", 0x0009, None),
    "off": ("Power Off", 0x0008, None),
    "sound_on": ("Sound On", 0x0069, None),
    "sound_off": ("Sound Off", 0x0068, None),
    "osc_on": ("Oscillation On", 0x0029, None),
    "osc_off": ("Oscillation Off", 0x0028, None),
}


class TokenManager:
    def __init__(self):
        self.access_token = None
        self.expiration_time = 0
        self.uid = UID
        self.lock = threading.Lock()

    def login(self):
        url = f"{API_BASE}/v2/enduser/enduserapi/emailPwdLogin"

        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "appsystemtype": "ios",
            "appid": "584",
            "accept": "application/json",
            "appversion": "3.6.0",
            "accept-language": "en-US;q=1, ar-US;q=0.9, fr-US;q=0.8",
            "user-agent": "Landbook/3.6.0 (iPhone; iOS 26.1; Scale/3.00)",
            "x-q-language": "EN",
            "app-info": "[iPhone][26.1]",
        }

        data = build_login_payload(NETPRISMA_EMAIL, NETPRISMA_PASSWORD)

        with httpx.Client(http2=True, timeout=20) as client:
            r = client.post(url, headers=headers, data=data)
            r.raise_for_status()
            parsed = r.json()

        if parsed.get("code") != 200:
            raise RuntimeError(f"Login failed: {parsed}")

        token_data = parsed["data"]["accessToken"]
        self.access_token = token_data["token"]
        self.expiration_time = int(token_data["expirationTime"])

        detected_uid = jwt_payload(self.access_token).get("uid")
        if detected_uid:
            self.uid = detected_uid

        print("NetPrisma login OK. Access token expires at:", self.expiration_time)
        if self.uid:
            print("NetPrisma UID:", self.uid)

    def get_auth_header(self):
        with self.lock:
            now = int(time.time())

            if not self.access_token or now > self.expiration_time - 300:
                self.login()

            return self.access_token


token_manager = TokenManager()


def find_first_string_key(obj, keys):
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value

        for value in obj.values():
            found = find_first_string_key(value, keys)
            if found:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = find_first_string_key(item, keys)
            if found:
                return found

    return None


def get_uid() -> str:
    token_manager.get_auth_header()

    uid = token_manager.uid or UID
    if not uid:
        raise RuntimeError("Could not autodetect UID from login token.")

    return uid


def get_fid() -> str:
    global FID

    if FID:
        return FID

    endpoints = [
        "/v2/family/enduserapi/getCurrentFamily",
        "/v2/family/enduserapi/getFamilyList",
    ]

    with httpx.Client(http2=True, timeout=15) as client:
        for endpoint in endpoints:
            r = client.get(f"{API_BASE}{endpoint}", headers=get_api_headers())
            r.raise_for_status()
            data = r.json()

            if data.get("code") != 200:
                continue

            detected_fid = find_first_string_key(data, ["fid", "familyId", "familyID"])

            if detected_fid:
                FID = detected_fid
                print("NetPrisma FID:", FID)
                return FID

    raise RuntimeError("Could not autodetect FID. Set NETPRISMA_FID in .env as a fallback.")

def check_auth(username, password):
    return (
        hmac.compare_digest(username or "", DASHBOARD_USER)
        and hmac.compare_digest(password or "", DASHBOARD_PASS)
    )


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.authorization

        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="NetPrisma Fan Dashboard"'},
            )

        return fn(*args, **kwargs)

    return wrapper


def enc_remaining_length(length: int) -> bytes:
    out = bytearray()

    while True:
        digit = length % 128
        length //= 128

        if length > 0:
            digit |= 0x80

        out.append(digit)

        if length == 0:
            break

    return bytes(out)


def utf8_field(text: str) -> bytes:
    data = text.encode("utf-8")
    return len(data).to_bytes(2, "big") + data


def mqtt_connect_packet(client_id: str, password: str) -> bytes:
    variable_header = (
        utf8_field("MQTT")
        + bytes([0x04])
        + bytes([0x42])
        + (40).to_bytes(2, "big")
    )

    payload = utf8_field(client_id) + utf8_field(password)
    body = variable_header + payload

    return bytes([0x10]) + enc_remaining_length(len(body)) + body


def mqtt_subscribe_packet(packet_id: int, topics: list[str]) -> bytes:
    body = packet_id.to_bytes(2, "big")

    for topic in topics:
        body += utf8_field(topic) + b"\x00"

    return bytes([0x82]) + enc_remaining_length(len(body)) + body


def mqtt_publish_packet(packet_id: int, topic: str, payload: bytes) -> bytes:
    body = utf8_field(topic) + packet_id.to_bytes(2, "big") + payload
    return bytes([0x33]) + enc_remaining_length(len(body)) + body


def mqtt_puback_packet(packet_id: int) -> bytes:
    return b"\x40\x02" + packet_id.to_bytes(2, "big")


def decode_remaining_length(data: bytes, pos: int = 1):
    multiplier = 1
    value = 0

    while True:
        encoded = data[pos]
        value += (encoded & 127) * multiplier
        pos += 1

        if encoded & 128 == 0:
            break

        multiplier *= 128

    return value, pos


def parse_publish(data: bytes):
    _, pos = decode_remaining_length(data)

    topic_len = int.from_bytes(data[pos:pos + 2], "big")
    pos += 2

    topic = data[pos:pos + topic_len].decode("utf-8", errors="replace")
    pos += topic_len

    qos = (data[0] >> 1) & 0x03
    packet_id = None

    if qos:
        packet_id = int.from_bytes(data[pos:pos + 2], "big")
        pos += 2

    payload = data[pos:]
    return topic, qos, packet_id, payload


def build_payload(control_id: int, value: int | None = None) -> bytes:
    seq = int(time.time() * 1000) & 0xFFFF

    body_without_checksum = (
        seq.to_bytes(2, "big")
        + b"\x00\x13"
        + control_id.to_bytes(2, "big")
    )

    if value is not None:
        body_without_checksum += value.to_bytes(2, "big")

    checksum = sum(body_without_checksum) & 0xFF
    body = bytes([checksum]) + body_without_checksum

    return b"\xaa\xaa" + len(body).to_bytes(2, "big") + body


def get_api_headers():
    return {
        "appsystemtype": "ios",
        "appid": "584",
        "accept": "application/json",
        "authorization": token_manager.get_auth_header(),
        "appversion": "3.6.0",
        "accept-language": "en-US;q=1",
        "user-agent": "Landbook/3.6.0 (iPhone; iOS 26.1; Scale/3.00)",
        "x-q-language": "EN",
        "app-info": "[iPhone][26.1]",
    }


def parse_bool(value):
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def parse_int(value, default="unknown"):
    try:
        return int(value)
    except Exception:
        return default


def get_devices():
    fid = get_fid()

    params = {
        "fid": fid,
        "isGroupDeviceShow": "1",
        "page": "1",
        "pageSize": "20",
    }

    url = f"{API_BASE}/v2/family/enduserapi/getCommonUsedDeviceList"

    with httpx.Client(http2=True, timeout=15) as client:
        r = client.get(url, headers=get_api_headers(), params=params)
        r.raise_for_status()
        data = r.json()

    if data.get("code") != 200:
        raise RuntimeError(f"Device list failed: {data}")

    payload = data.get("data") or {}
    return payload.get("list") or []


def get_product_key_for_device(device_key: str) -> str:
    with STATE_LOCK:
        state = STATES.get(device_key, {}).copy()

    product_key = state.get("productKey") or PRODUCT_KEY

    if not product_key:
        raise RuntimeError(
            f"No productKey found for device {device_key}. "
            "Device metadata sync must run first, or NETPRISMA_PRODUCT_KEY must be set."
        )

    return product_key


def get_device_business_attributes(device_key: str, product_key: str | None = None):
    url = f"{API_BASE}/v2/binding/enduserapi/getDeviceBusinessAttributes"

    if not product_key:
        product_key = get_product_key_for_device(device_key)

    params = {
        "dk": device_key,
        "pk": product_key,
    }

    with httpx.Client(http2=True, timeout=15) as client:
        r = client.get(url, headers=get_api_headers(), params=params)
        r.raise_for_status()
        data = r.json()

    if data.get("code") != 200:
        raise RuntimeError(f"Business attributes failed: {data}")

    return data.get("data", {})


def apply_business_attributes(device_key: str, data: dict):
    device_data = data.get("deviceData") or {}
    tsl_items = data.get("customizeTslInfo") or []

    attrs = {}

    for item in tsl_items:
        code = item.get("resourceCode")
        if code:
            attrs[code] = item.get("resourceValce")

    with STATE_LOCK:
        state = STATES.setdefault(device_key, {})
        state["lastBusinessRefresh"] = int(time.time())

        if "temperature" in attrs:
            state["temperature"] = parse_int(attrs["temperature"])

        if "switch" in attrs:
            state["power"] = "on" if parse_bool(attrs["switch"]) else "off"

        if "wind_speed" in attrs:
            state["speed"] = parse_int(attrs["wind_speed"])

        if "swing_wind" in attrs:
            state["oscillation"] = "on" if parse_bool(attrs["swing_wind"]) else "off"

        if "sound" in attrs:
            state["sound"] = "on" if parse_bool(attrs["sound"]) else "off"

        if "screen_display" in attrs:
            state["light"] = "on" if parse_bool(attrs["screen_display"]) else "off"

        if "working_mode" in attrs:
            state["mode"] = parse_int(attrs["working_mode"])

        if "countdown" in attrs:
            state["countdown"] = parse_int(attrs["countdown"])

        state["battery"] = device_data.get("battery", "unknown")
        state["voltage"] = device_data.get("voltage", "unknown")
        state["signalStrength"] = device_data.get("signalStrength", "unknown")
        state["firmware"] = device_data.get("version", "unknown")
        state["sdkVer"] = device_data.get("sdkVer", "unknown")
        state["deviceType"] = device_data.get("type", "unknown")
        state["dataProtocolVer"] = device_data.get("dataProtocolVer", "unknown")


def refresh_business_attributes_for_device(
    device_key: str,
    product_key: str | None = None,
    force: bool = False,
):
    now = int(time.time())

    with STATE_LOCK:
        last = STATES.get(device_key, {}).get("lastBusinessRefresh", 0)

    if not force and now - int(last or 0) < BUSINESS_REFRESH_TTL:
        return

    data = get_device_business_attributes(device_key, product_key)
    apply_business_attributes(device_key, data)


def refresh_business_attributes_for_all(devices: list, force: bool = False):
    for d in devices:
        key = d.get("deviceKey")
        pk = d.get("productKey") or PRODUCT_KEY

        if not key:
            continue

        try:
            refresh_business_attributes_for_device(key, pk, force=force)
        except Exception as e:
            with STATE_LOCK:
                STATES.setdefault(key, {})["lastBusinessError"] = str(e)

            print("Business attribute refresh failed:", key, e)


def sync_device_metadata(devices):
    with STATE_LOCK:
        for d in devices:
            key = d.get("deviceKey")

            if not key:
                continue

            current = STATES.setdefault(key, {})

            current.update({
                "deviceKey": key,
                "deviceName": d.get("deviceName"),
                "productName": d.get("productName"),
                "productKey": d.get("productKey") or PRODUCT_KEY,
                "online": bool(d.get("onlineStatus")),
                "lastSeen": int(time.time()),
            })

            current.setdefault("power", "unknown")
            current.setdefault("speed", "unknown")
            current.setdefault("oscillation", "unknown")
            current.setdefault("sound", "unknown")
            current.setdefault("temperature", "unknown")
            current.setdefault("light", "unknown")
            current.setdefault("battery", "unknown")
            current.setdefault("voltage", "unknown")
            current.setdefault("signalStrength", "unknown")
            current.setdefault("firmware", "unknown")
            current.setdefault("sdkVer", "unknown")
            current.setdefault("deviceType", "unknown")
            current.setdefault("dataProtocolVer", "unknown")
            current.setdefault("countdown", "unknown")
            current.setdefault("mode", "unknown")


def device_key_from_topic(topic: str):
    with STATE_LOCK:
        known = [
            (key, state.get("productKey"))
            for key, state in STATES.items()
            if state.get("productKey")
        ]

    for key, product_key in known:
        marker = f"qd{product_key}{key}"
        if marker in topic:
            return key

    if PRODUCT_KEY:
        marker = f"qd{PRODUCT_KEY}"

        if marker in topic:
            tail = topic.split(marker, 1)[1]
            return tail.split("/", 1)[0]

    return None


def parse_bus_payload(payload: bytes):
    if len(payload) < 11:
        return None

    if not payload.startswith(b"\xaa\xaa"):
        return None

    body_len = int.from_bytes(payload[2:4], "big")
    body = payload[4:4 + body_len]

    if len(body) < 7:
        return None

    checksum = body[0]
    seq = int.from_bytes(body[1:3], "big")
    msg_type = int.from_bytes(body[3:5], "big")
    control_id = int.from_bytes(body[5:7], "big")

    value = None
    if len(body) >= 9:
        value = int.from_bytes(body[7:9], "big")

    return {
        "checksum": checksum,
        "seq": seq,
        "msg_type": msg_type,
        "control_id": control_id,
        "value": value,
    }


def update_state_from_bus(device_key: str, payload: bytes):
    parsed = parse_bus_payload(payload)

    if not parsed:
        return

    control_id = parsed["control_id"]
    value = parsed["value"]

    with STATE_LOCK:
        state = STATES.setdefault(device_key, {})
        state["lastSeen"] = int(time.time())
        state["lastPayload"] = payload.hex()

        if control_id == 0x0008:
            state["power"] = "off"
        elif control_id == 0x0009:
            state["power"] = "on"
        elif control_id == 0x001A:
            state["speed"] = value
        elif control_id == 0x0028:
            state["oscillation"] = "off"
        elif control_id == 0x0029:
            state["oscillation"] = "on"
        elif control_id == 0x0068:
            state["sound"] = "off"
        elif control_id == 0x0069:
            state["sound"] = "on"


def update_state_from_online_payload(device_key: str, payload: bytes):
    try:
        data = json.loads(payload.decode("utf-8"))
        value = data.get("data", {}).get("value")
    except Exception:
        return

    with STATE_LOCK:
        state = STATES.setdefault(device_key, {})
        state["online"] = value == 1
        state["lastSeen"] = int(time.time())


def handle_incoming_publish(topic: str, payload: bytes):
    device_key = device_key_from_topic(topic)

    if not device_key:
        return

    if topic.endswith("/bus"):
        update_state_from_bus(device_key, payload)
    elif topic.endswith("/onl_"):
        update_state_from_online_payload(device_key, payload)


def send_fan_command(device_key: str, payload: bytes):
    product_key = get_product_key_for_device(device_key)
    device_prefix = f"qd{product_key}{device_key}"

    publish_topic = f"q/1/d/{device_prefix}/bus"
    sub_topics = [
        f"q/2/d/{device_prefix}/ack_",
        f"q/2/d/{device_prefix}/bus",
        f"q/2/d/{device_prefix}/onl_",
    ]

    client_id = f"qu_{get_uid()}_{int(time.time() * 1000)}"
    password = token_manager.get_auth_header()

    ws = websocket.create_connection(
        MQTT_URL,
        subprotocols=["mqtt"],
        origin="https://iot-south.netprisma.us:8443",
        sslopt={"cert_reqs": ssl.CERT_REQUIRED},
        timeout=10,
    )

    try:
        ws.send_binary(mqtt_connect_packet(client_id, password))

        data = ws.recv()
        if isinstance(data, str):
            data = data.encode()

        if data != b"\x20\x02\x00\x00":
            return {
                "ok": False,
                "error": f"MQTT connect failed: {data.hex()}",
            }

        ws.send_binary(mqtt_subscribe_packet(1, sub_topics))
        time.sleep(0.25)

        ws.send_binary(mqtt_publish_packet(8, publish_topic, payload))

        end = time.time() + 6
        messages = []

        while time.time() < end:
            try:
                ws.settimeout(2)
                data = ws.recv()

                if isinstance(data, str):
                    data = data.encode()

                if not data:
                    continue

                packet_type = data[0] >> 4

                if packet_type == 3:
                    topic, qos, packet_id, msg_payload = parse_publish(data)

                    if qos == 1 and packet_id is not None:
                        ws.send_binary(mqtt_puback_packet(packet_id))

                    handle_incoming_publish(topic, msg_payload)

                    item = {
                        "topic": topic,
                        "payload_hex": msg_payload.hex(),
                    }

                    try:
                        text = msg_payload.decode("utf-8")
                        item["payload_text"] = text

                        try:
                            item["payload_json"] = json.loads(text)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    messages.append(item)

                    if topic.endswith("/ack_"):
                        try:
                            ack = json.loads(msg_payload.decode("utf-8"))

                            if ack.get("status") == "succ":
                                return {
                                    "ok": True,
                                    "ack": ack,
                                    "messages": messages,
                                }
                        except Exception:
                            pass

            except (websocket.WebSocketTimeoutException, socket.timeout):
                ws.send_binary(b"\xc0\x00")

        return {
            "ok": True,
            "warning": "Command sent, but no SENDACK received before timeout.",
            "messages": messages,
        }

    finally:
        try:
            ws.send_binary(b"\xe0\x00")
        except Exception:
            pass

        ws.close()


def mqtt_state_listener():
    while True:
        try:
            devices = get_devices()
            sync_device_metadata(devices)
            refresh_business_attributes_for_all(devices, force=True)

            topics = []

            for d in devices:
                key = d.get("deviceKey")

                if not key:
                    continue

                product_key = d.get("productKey") or get_product_key_for_device(key)
                prefix = f"qd{product_key}{key}"

                topics.extend([
                    f"q/2/d/{prefix}/bus",
                    f"q/2/d/{prefix}/onl_",
                ])

            if not topics:
                time.sleep(10)
                continue

            client_id = f"qu_{get_uid()}_{int(time.time() * 1000)}"
            password = token_manager.get_auth_header()

            ws = websocket.create_connection(
                MQTT_URL,
                subprotocols=["mqtt"],
                origin="https://iot-south.netprisma.us:8443",
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},
                timeout=10,
            )

            ws.send_binary(mqtt_connect_packet(client_id, password))

            data = ws.recv()
            if isinstance(data, str):
                data = data.encode()

            if data != b"\x20\x02\x00\x00":
                raise RuntimeError(f"MQTT listener connect failed: {data.hex()}")

            ws.send_binary(mqtt_subscribe_packet(1, topics))

            with STATE_LOCK:
                LISTENER_STATUS["running"] = True
                LISTENER_STATUS["last_error"] = None
                LISTENER_STATUS["last_connect"] = int(time.time())

            print("MQTT state listener connected")

            last_refresh = time.time()

            while True:
                if time.time() - last_refresh > 600:
                    break

                try:
                    ws.settimeout(25)
                    data = ws.recv()

                    if isinstance(data, str):
                        data = data.encode()

                    if not data:
                        continue

                    packet_type = data[0] >> 4

                    if packet_type == 3:
                        topic, qos, packet_id, payload = parse_publish(data)

                        if qos == 1 and packet_id is not None:
                            ws.send_binary(mqtt_puback_packet(packet_id))

                        handle_incoming_publish(topic, payload)

                    elif packet_type == 13:
                        pass

                except (websocket.WebSocketTimeoutException, socket.timeout):
                    ws.send_binary(b"\xc0\x00")

        except Exception as e:
            with STATE_LOCK:
                LISTENER_STATUS["running"] = False
                LISTENER_STATUS["last_error"] = str(e)

            print("MQTT state listener error:", e)
            time.sleep(5)


def start_listener_once():
    t = threading.Thread(target=mqtt_state_listener, daemon=True)
    t.start()


HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Fan Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #eef3ff;
      --bg2: #f7f9ff;
      --text: #1f2937;
      --muted: #8b95a7;
      --card: #ffffff;
      --blue: #3b82f6;
      --blue2: #6ea8ff;
      --green: #22c55e;
      --red: #ef4444;
      --shadow: 0 18px 45px rgba(39, 76, 132, .14);
      --border: rgba(148, 163, 184, .18);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 20% 0%, rgba(91, 154, 255, .30), transparent 35%),
        linear-gradient(180deg, var(--bg), var(--bg2));
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .page {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      margin-bottom: 22px;
    }

    .title {
      font-size: 34px;
      line-height: 1.05;
      font-weight: 850;
      letter-spacing: -0.05em;
      color: #0f172a;
    }

    .subtitle {
      margin-top: 8px;
      color: var(--muted);
      font-size: 15px;
    }

    .listener {
      font-size: 13px;
      color: var(--muted);
      margin-top: 8px;
    }

    .all-controls {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .main-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 18px;
    }

    .fan-card {
      background: rgba(255, 255, 255, .90);
      border: 1px solid var(--border);
      border-radius: 28px;
      padding: 22px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }

    .fan-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 14px;
      margin-bottom: 18px;
    }

    .fan-name {
      font-size: 24px;
      font-weight: 850;
      letter-spacing: -0.045em;
      color: #111827;
    }

    .fan-meta {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
      word-break: break-all;
    }

    .online-pill {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 7px 10px;
      border-radius: 999px;
      background: #ecfdf5;
      color: #15803d;
      border: 1px solid #bbf7d0;
      font-size: 12px;
      font-weight: 750;
      white-space: nowrap;
    }

    .dot {
      width: 8px;
      height: 8px;
      background: #22c55e;
      border-radius: 50%;
      box-shadow: 0 0 0 4px rgba(34, 197, 94, .15);
    }

    .hero {
      display: grid;
      grid-template-columns: 1fr 150px;
      gap: 14px;
      align-items: center;
      margin: 12px 0 22px;
    }

    .temperature {
      color: var(--muted);
      font-size: 15px;
      margin-bottom: 4px;
    }

    .temp-value {
      font-size: 38px;
      font-weight: 850;
      letter-spacing: -0.04em;
      color: #111827;
    }

    .device-info {
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }

    .power-switch-wrap {
      margin-top: 18px;
    }

    .switch-label {
      margin-top: 8px;
      color: var(--muted);
      font-weight: 700;
      letter-spacing: .04em;
      font-size: 13px;
    }

    .switch {
      width: 92px;
      height: 48px;
      border: 0;
      border-radius: 999px;
      background: #cfd6e6;
      padding: 4px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: flex-start;
      transition: .18s ease;
      box-shadow: inset 0 2px 8px rgba(0,0,0,.08);
    }

    .switch.on-state {
      background: var(--blue);
      justify-content: flex-end;
    }

    .knob {
      width: 40px;
      height: 40px;
      background: #fff;
      border-radius: 50%;
      box-shadow: 0 7px 18px rgba(35, 62, 112, .25);
    }

    .fan-visual {
      height: 190px;
      position: relative;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .air-ring {
      position: absolute;
      width: 145px;
      height: 145px;
      border-radius: 50%;
      border: 5px solid rgba(59, 130, 246, .18);
    }

    .air-ring::before,
    .air-ring::after {
      content: "";
      position: absolute;
      inset: -5px;
      border-radius: 50%;
      border: 5px solid transparent;
      border-left-color: var(--blue);
      border-top-color: rgba(59, 130, 246, .55);
      transform: rotate(-30deg);
    }

    .air-ring::after {
      inset: 16px;
      border-width: 3px;
      opacity: .7;
      transform: rotate(70deg);
    }

    .tower {
      position: relative;
      z-index: 2;
      width: 44px;
      height: 150px;
      border-radius: 18px 18px 14px 14px;
      background: linear-gradient(90deg, #d6dbe7, #1f2937 22%, #111827 50%, #2f3745 78%, #e5e7eb);
      box-shadow: 0 12px 24px rgba(15,23,42,.18);
    }

    .tower::before {
      content: "";
      position: absolute;
      top: 8px;
      left: 11px;
      right: 11px;
      height: 18px;
      border-radius: 7px;
      background: #0f172a;
      box-shadow: inset 0 -2px 0 rgba(255,255,255,.2);
    }

    .tower::after {
      content: "";
      position: absolute;
      bottom: -16px;
      left: -22px;
      width: 88px;
      height: 18px;
      border-radius: 50%;
      background: #e5e7eb;
      box-shadow: 0 8px 18px rgba(15,23,42,.12);
    }

    .panel {
      background: var(--card);
      border-radius: 24px;
      box-shadow: 0 10px 28px rgba(50, 77, 126, .10);
      border: 1px solid rgba(226,232,240,.85);
      padding: 18px;
      margin-bottom: 16px;
    }

    .speed-row {
      display: grid;
      grid-template-columns: 52px 1fr 52px;
      align-items: center;
      gap: 14px;
    }

    .circle-btn {
      width: 52px;
      height: 52px;
      border: 0;
      border-radius: 50%;
      background: #eef2ff;
      color: #111827;
      font-size: 28px;
      font-weight: 650;
      cursor: pointer;
    }

    .circle-btn:disabled {
      opacity: .35;
      cursor: wait;
    }

    .speed-display {
      text-align: center;
    }

    .speed-number {
      font-size: 38px;
      line-height: 1;
      font-weight: 850;
      color: #111827;
    }

    .speed-label {
      margin-top: 4px;
      color: var(--muted);
      font-size: 14px;
    }

    .speed-bar {
      margin-top: 18px;
      height: 18px;
      border-radius: 999px;
      background: #e5e7f3;
      overflow: hidden;
    }

    .speed-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #c9dcff, var(--blue));
      border-radius: 999px;
      transition: width .2s ease;
    }

    .toggle-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 14px;
    }

    .mini-card {
      background: var(--card);
      border-radius: 22px;
      box-shadow: 0 10px 28px rgba(50, 77, 126, .10);
      border: 1px solid rgba(226,232,240,.85);
      padding: 18px;
      min-height: 116px;
    }

    .mini-title {
      font-size: 18px;
      font-weight: 800;
      letter-spacing: -0.03em;
      color: #1f2937;
      margin-bottom: 18px;
    }

    .mini-switch {
      width: 72px;
      height: 38px;
      border: 0;
      border-radius: 999px;
      background: #d6dbe7;
      padding: 4px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: flex-start;
      transition: .18s ease;
    }

    .mini-switch.on-state {
      background: var(--blue);
      justify-content: flex-end;
    }

    .mini-knob {
      width: 30px;
      height: 30px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 5px 14px rgba(35, 62, 112, .2);
    }

    .status {
      margin-top: 14px;
      padding: 12px 14px;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 16px;
      color: #64748b;
      min-height: 44px;
      font-size: 13px;
      white-space: pre-wrap;
    }

    .all-btn {
      min-width: 170px;
      border: 0;
      border-radius: 18px;
      padding: 17px 22px;
      color: #fff;
      font-size: 16px;
      font-weight: 850;
      cursor: pointer;
      box-shadow: 0 12px 30px rgba(39, 76, 132, .16);
    }

    .all-on {
      background: #5aa463;
    }

    .all-off {
      background: #d65333;
    }

    @media (max-width: 720px) {
      .page {
        padding: 18px;
      }

      .topbar {
        display: block;
      }

      .title {
        font-size: 32px;
      }

      .all-controls {
        margin-top: 18px;
        justify-content: stretch;
      }

      .all-btn {
        flex: 1;
        min-width: 0;
      }

      .main-grid {
        grid-template-columns: 1fr;
      }

      .fan-card {
        border-radius: 26px;
        padding: 18px;
      }

      .hero {
        grid-template-columns: 1fr 130px;
      }

      .fan-visual {
        height: 170px;
      }

      .air-ring {
        width: 128px;
        height: 128px;
      }

      .toggle-grid {
        grid-template-columns: repeat(2, 1fr);
      }
    }

    @media (max-width: 420px) {
      .hero {
        grid-template-columns: 1fr;
      }

      .fan-visual {
        order: -1;
      }
    }
  </style>
</head>
<body>
<div class="page">
  <header class="topbar">
    <div>
      <div class="title">My Family</div>
      <div class="subtitle">Common fans dashboard</div>
      <div class="listener" id="listener">Listener: checking...</div>
    </div>

    <div class="all-controls">
      <button class="all-btn all-on" onclick="allCmd('on', this)">All Fans On</button>
      <button class="all-btn all-off" onclick="allCmd('off', this)">All Fans Off</button>
    </div>
  </header>

  <main class="main-grid">
  {% for d in devices %}
    <section class="fan-card">
      <div class="fan-head">
        <div>
          <div class="fan-name">{{ d.deviceName }}</div>
          <div class="fan-meta">{{ d.productName }} · {{ d.deviceKey }}</div>
        </div>

        <div class="online-pill" id="online-{{ d.deviceKey }}">
          <span class="dot"></span>
          <span id="online-text-{{ d.deviceKey }}">Online</span>
        </div>
      </div>

      <div class="hero">
        <div>
          <div class="temperature">Current temperature</div>
          <div class="temp-value" id="temp-{{ d.deviceKey }}">—°F</div>

          <div class="device-info">
            <div>Signal: <span id="signal-{{ d.deviceKey }}">—</span></div>
            <div>Battery: <span id="battery-{{ d.deviceKey }}">—</span></div>
            <div>Voltage: <span id="voltage-{{ d.deviceKey }}">—</span></div>
            <div>Light: <span id="light-{{ d.deviceKey }}">—</span></div>
          </div>

          <div class="power-switch-wrap">
            <button class="switch" id="power-switch-{{ d.deviceKey }}" onclick="togglePower('{{ d.deviceKey }}', this)">
              <span class="knob"></span>
            </button>
            <div class="switch-label" id="power-label-{{ d.deviceKey }}">UNKNOWN</div>
          </div>
        </div>

        <div class="fan-visual">
          <div class="air-ring"></div>
          <div class="tower"></div>
        </div>
      </div>

      <div class="panel">
        <div class="speed-row">
          <button class="circle-btn" onclick="speedStep('{{ d.deviceKey }}', -1, this)">−</button>

          <div class="speed-display">
            <div class="speed-number" id="speed-{{ d.deviceKey }}">—</div>
            <div class="speed-label">Fan Speed</div>
          </div>

          <button class="circle-btn" onclick="speedStep('{{ d.deviceKey }}', 1, this)">+</button>
        </div>

        <div class="speed-bar">
          <div class="speed-fill" id="speed-fill-{{ d.deviceKey }}"></div>
        </div>
      </div>

      <div class="toggle-grid">
        <div class="mini-card">
          <div class="mini-title">Oscillation</div>
          <button class="mini-switch" id="osc-switch-{{ d.deviceKey }}" onclick="toggleOsc('{{ d.deviceKey }}', this)">
            <span class="mini-knob"></span>
          </button>
        </div>

        <div class="mini-card">
          <div class="mini-title">Sound</div>
          <button class="mini-switch" id="sound-switch-{{ d.deviceKey }}" onclick="toggleSound('{{ d.deviceKey }}', this)">
            <span class="mini-knob"></span>
          </button>
        </div>
      </div>

      <div class="status" id="status-{{ d.deviceKey }}">Ready</div>
    </section>
  {% endfor %}
  </main>
</div>

<script>
const FAN_SPEED_COUNT = {{ fan_speed_count }};
let latestStates = {};

function getState(deviceKey) {
  return latestStates[deviceKey] || {};
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function valOrDash(v, suffix = "") {
  if (v === undefined || v === null || v === "unknown" || v === "") return "—";
  return String(v) + suffix;
}

function setSwitch(id, isOn) {
  const el = document.getElementById(id);
  if (!el) return;

  if (isOn) {
    el.classList.add("on-state");
  } else {
    el.classList.remove("on-state");
  }
}

function speedPercent(speed) {
  const n = parseInt(speed || 0);

  if (!n || n < 1) return 0;

  return Math.min(100, Math.max(0, (n / FAN_SPEED_COUNT) * 100));
}

async function refreshState() {
  try {
    const res = await fetch("/api/state");
    const data = await res.json();

    if (!data.ok) return;

    latestStates = data.states || {};

    const listener = data.listener || {};
    setText(
      "listener",
      "Listener: " + (listener.running ? "connected" : "disconnected") +
      (listener.last_error ? " · " + listener.last_error : "")
    );

    for (const [key, s] of Object.entries(latestStates)) {
      const powerOn = s.power === "on";
      const oscOn = s.oscillation === "on";
      const soundOn = s.sound === "on";
      const speed = s.speed && s.speed !== "unknown" ? s.speed : "—";

      setText("temp-" + key, valOrDash(s.temperature, "°F"));
      setText("signal-" + key, valOrDash(s.signalStrength, " dBm"));
      setText("battery-" + key, valOrDash(s.battery, "%"));
      setText("voltage-" + key, valOrDash(s.voltage, "V"));
      setText("light-" + key, valOrDash(s.light));

      setSwitch("power-switch-" + key, powerOn);
      setText(
        "power-label-" + key,
        s.power && s.power !== "unknown" ? s.power.toUpperCase() : "UNKNOWN"
      );

      setText("speed-" + key, speed);

      const fill = document.getElementById("speed-fill-" + key);
      if (fill) fill.style.width = speedPercent(speed) + "%";

      setSwitch("osc-switch-" + key, oscOn);
      setSwitch("sound-switch-" + key, soundOn);

      const online = document.getElementById("online-" + key);
      const onlineText = document.getElementById("online-text-" + key);

      if (online && onlineText) {
        onlineText.textContent = s.online ? "Online" : "Offline";
        online.style.background = s.online ? "#ecfdf5" : "#fef2f2";
        online.style.color = s.online ? "#15803d" : "#b91c1c";
        online.style.borderColor = s.online ? "#bbf7d0" : "#fecaca";
      }
    }
  } catch (e) {
    setText("listener", "Listener: error");
  }
}

async function cmd(deviceKey, action, btn) {
  const box = document.getElementById("status-" + deviceKey);
  box.textContent = "Sending " + action + "...";
  btn.disabled = true;

  try {
    const res = await fetch("/api/command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({device_key: deviceKey, action})
    });

    const data = await res.json();

    if (data.ok) {
      box.textContent = "Success: " + action;
      await refreshState();
    } else {
      box.textContent = "Failed: " + JSON.stringify(data, null, 2);
    }
  } catch (e) {
    box.textContent = "Error: " + e;
  } finally {
    btn.disabled = false;
  }
}

function togglePower(deviceKey, btn) {
  const s = getState(deviceKey);
  const next = s.power === "on" ? "off" : "on";

  cmd(deviceKey, next, btn);
}

function toggleOsc(deviceKey, btn) {
  const s = getState(deviceKey);
  const next = s.oscillation === "on" ? "osc_off" : "osc_on";

  cmd(deviceKey, next, btn);
}

function toggleSound(deviceKey, btn) {
  const s = getState(deviceKey);
  const next = s.sound === "on" ? "sound_off" : "sound_on";

  cmd(deviceKey, next, btn);
}

function speedStep(deviceKey, delta, btn) {
  const s = getState(deviceKey);
  let current = parseInt(s.speed || 1);

  if (!current || current < 1) current = 1;

  let next = current + delta;

  if (next < 1) next = 1;
  if (next > FAN_SPEED_COUNT) next = FAN_SPEED_COUNT;

  cmd(deviceKey, "speed:" + next, btn);
}

async function allCmd(action, btn) {
  btn.disabled = true;

  try {
    const res = await fetch("/api/all", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action})
    });

    const data = await res.json();

    await refreshState();

    if (!data.ok) {
      alert("Some commands failed. Check dashboard logs.");
    }
  } catch (e) {
    alert("Error: " + e);
  } finally {
    btn.disabled = false;
  }
}

refreshState();
setInterval(refreshState, 2000);
</script>
</body>
</html>
"""


@app.route("/")
@require_auth
def index():
    devices = get_devices()
    sync_device_metadata(devices)

    # Force refresh on page load so the dashboard opens with real current state.
    refresh_business_attributes_for_all(devices, force=True)

    return render_template_string(
        HTML, devices=devices, fan_speed_count=FAN_SPEED_COUNT
    )


@app.route("/api/state")
@require_auth
def api_state():
    try:
        devices = get_devices()
        sync_device_metadata(devices)

        # Frontend calls this every 2 seconds, but business attributes
        # refresh only every BUSINESS_REFRESH_TTL seconds unless forced.
        refresh_business_attributes_for_all(devices, force=False)

    except Exception as e:
        print("State refresh error:", e)

    with STATE_LOCK:
        return jsonify({
            "ok": True,
            "states": STATES,
            "listener": LISTENER_STATUS,
        })


@app.route("/api/command", methods=["POST"])
@require_auth
def api_command():
    body = request.get_json(force=True)
    device_key = body["device_key"]
    action = body["action"]

    if action.startswith("speed:"):
        speed = int(action.split(":", 1)[1])

        if speed < 1 or speed > 12:
            return jsonify({"ok": False, "error": "Invalid speed"})

        payload = build_payload(0x001A, speed)

    else:
        if action not in ACTIONS:
            return jsonify({"ok": False, "error": f"Unknown action: {action}"})

        _, control_id, value = ACTIONS[action]
        payload = build_payload(control_id, value)

    try:
        refresh_business_attributes_for_device(device_key, force=True)
    except Exception as e:
        print("Pre-command attribute refresh failed:", e)

    result = send_fan_command(device_key, payload)

    time.sleep(0.7)

    try:
        refresh_business_attributes_for_device(device_key, force=True)
    except Exception as e:
        print("Post-command attribute refresh failed:", e)

    result["payload_hex"] = payload.hex()

    return jsonify(result)


@app.route("/api/all", methods=["POST"])
@require_auth
def api_all():
    body = request.get_json(force=True)
    action = body.get("action")

    if action not in ("on", "off"):
        return jsonify({"ok": False, "error": "Only on/off supported for all fans"})

    _, control_id, value = ACTIONS[action]
    devices = get_devices()
    results = []

    sync_device_metadata(devices)
    refresh_business_attributes_for_all(devices, force=True)

    for d in devices:
        key = d.get("deviceKey")
        pk = d.get("productKey") or PRODUCT_KEY

        if not key:
            continue

        payload = build_payload(control_id, value)
        result = send_fan_command(key, payload)

        time.sleep(0.5)

        try:
            refresh_business_attributes_for_device(key, pk, force=True)
        except Exception as e:
            print("Post-all-command attribute refresh failed:", key, e)

        results.append({
            "deviceKey": key,
            "deviceName": d.get("deviceName"),
            "payload_hex": payload.hex(),
            "result": result,
        })

    ok = all(item["result"].get("ok") for item in results)

    return jsonify({
        "ok": ok,
        "action": action,
        "results": results,
    })


@app.route("/health")
def health():
    return jsonify({"ok": True})


start_listener_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)