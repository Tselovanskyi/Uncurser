import concurrent.futures
import ipaddress
import json
import re
import socket
import threading
import urllib.request

import websocket


SUPPORTED_MODEL = "F008"
SUPPORTED_MODEL_NAME = "K2 Plus"
SUPPORTED_FIRMWARE = "1.1.6.4"


def device_identity(status):
    native = status.get("native", {}) if isinstance(status, dict) else {}
    if not isinstance(native, dict):
        return "", ""
    model, version = native.get("model"), native.get("modelVersion")
    model = model.strip() if isinstance(model, str) else ""
    version = version.strip() if isinstance(version, str) else ""
    pattern = r"\d+\.\d+\.\d+\.\d+"
    if not re.fullmatch(pattern, version):
        # The native K2 response reports the release in its DWIN software field.
        fields = [part.partition(":") for part in version.split(";")]
        versions = [value.strip() for key, separator, value in fields if key.strip() == "DWIN sw ver" and separator]
        version = versions[0] if len(versions) == 1 else ""
    return model, version if re.fullmatch(pattern, version) else ""


def compatibility_error(status):
    model, firmware = device_identity(status)
    if (model, firmware) == (SUPPORTED_MODEL, SUPPORTED_FIRMWARE):
        return ""
    reason = ("Could not verify the printer model or firmware." if not model or not firmware
              else "This printer model or firmware is not supported.")
    return reason + f" Mods require {SUPPORTED_MODEL_NAME} firmware {SUPPORTED_FIRMWARE}."


def require_supported(status):
    reason = compatibility_error(status)
    if reason:
        raise ValueError(reason)


def parse_host(value):
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ValueError:
        raise ValueError("Enter an IPv4 address, for example 192.168.50.130 (without http:// or a port).") from None
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("Enter the printer's LAN address, for example 192.168.50.130.")
    return str(address)


def http(host, route, payload=None, timeout=5):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request("http://" + parse_host(host) + ":7125" + route,
                                     data=data, headers={"Content-Type": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        result = json.load(response)
    if "error" in result:
        raise RuntimeError(str(result["error"]))
    return result.get("result", result)


def native_status(host):
    connection = websocket.create_connection("ws://" + parse_host(host) + ":9999/",
                                             timeout=4, http_no_proxy=["*"])
    try:
        for _ in range(8):
            value = json.loads(connection.recv())
            params = value.get("params", value)
            if isinstance(params, dict) and "deviceState" in params:
                return params
        raise RuntimeError("Native printer status is unavailable.")
    finally:
        connection.close()


def status(host):
    info = http(host, "/printer/info")
    objects = http(host, "/printer/objects/query?print_stats&idle_timeout&toolhead")
    return {"info": info, "objects": objects["status"], "native": native_status(host)}


def require_idle(value):
    info, objects, native = value["info"], value["objects"], value["native"]
    if info.get("state") != "ready":
        raise RuntimeError("Klipper must be ready before Apply. Scanning and preview remain available.")
    if objects.get("print_stats", {}).get("state") in ("printing", "paused"):
        raise RuntimeError("Finish or cancel the print before applying changes.")
    if native.get("deviceState") != 0 or objects.get("idle_timeout", {}).get("state") == "Printing":
        raise RuntimeError("The printer is busy or preparing a print. Wait until it is idle.")
    if objects.get("toolhead", {}).get("G29_flag"):
        raise RuntimeError("Wait for calibration to finish before Apply.")


def probe(host):
    try:
        with socket.create_connection((host, 7125), timeout=0.3):
            pass
        info = http(host, "/printer/info", timeout=0.8)
        if "klipper_path" in info:
            return (host, info.get("hostname", "Klipper printer"))
    except (OSError, ValueError, KeyError, RuntimeError):
        pass
    return None


def reachable_printers(hosts):
    def reachable(host):
        try:
            with socket.create_connection((parse_host(host), 22), timeout=0.7):
                return host, True
        except (OSError, ValueError):
            return host, False
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(reachable, set(hosts)))


def discover(networks, progress=lambda message: None, on_found=lambda printer: None, stop=None):
    stop = stop if stop is not None else threading.Event()
    intervals = []
    for value in networks:
        subnet = ipaddress.IPv4Network(value, strict=False)
        trim = int(subnet.prefixlen < 31)
        intervals.append((int(subnet.network_address) + trim, int(subnet.broadcast_address) - trim))
    ranges = []
    for first, last in sorted(intervals):
        if ranges and first <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(last, ranges[-1][1]))
        else:
            ranges.append((first, last))
    total = sum(last - first + 1 for first, last in ranges)
    addresses = (str(ipaddress.IPv4Address(address)) for first, last in ranges for address in range(first, last + 1))
    found = []
    count = 0
    reported = 0
    progress(f"Searching local networks · 0/{total} addresses")
    # Bound queued work even for large subnets; closing the app cancels the rest.
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
        pending = set()
        while not stop.is_set():
            while len(pending) < 48 and not stop.is_set():
                host = next(addresses, None)
                if host is None:
                    break
                pending.add(pool.submit(probe, host))
            if not pending:
                break
            finished, pending = concurrent.futures.wait(pending, timeout=0.1,
                                                        return_when=concurrent.futures.FIRST_COMPLETED)
            for future in finished:
                count += 1
                result = future.result()
                if result and not stop.is_set():
                    found.append(result)
                    on_found(result)
            if count - reported >= 32 or count == total:
                progress(f"Searching local networks · {count}/{total} addresses")
                reported = count
        for future in pending:
            future.cancel()
    return sorted(found)
