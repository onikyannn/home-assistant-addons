#!/usr/bin/env python3
"""One owner for outbound selection and bounded WireGuard recovery."""

import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_SETTINGS = {
    "healthcheck_url": "https://www.gstatic.com/generate_204",
    "internet_check_urls": ["https://1.1.1.1/", "https://8.8.8.8/"],
    "healthcheck_tolerance": 50,
    "healthcheck_interval": 30,
    "recovery_grace": 90,
}


def load_settings(path):
    try:
        options = json.loads(Path(path).read_text())
    except FileNotFoundError:
        options = {}
    settings = {key: options.get(key, value) for key, value in DEFAULT_SETTINGS.items()}
    for key, (low, high) in {
        "healthcheck_tolerance": (0, 10000), "healthcheck_interval": (5, 3600),
        "recovery_grace": (0, 3600),
    }.items():
        value = settings[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    for key in ("healthcheck_url", "internet_check_urls"):
        urls = [settings[key]] if key == "healthcheck_url" else settings[key]
        if not isinstance(urls, list) or not urls:
            raise ValueError(f"{key} must contain at least one URL")
        for url in urls:
            schemes = ("https",) if key == "healthcheck_url" else ("http", "https")
            if not isinstance(url, str) or any(c.isspace() for c in url):
                raise ValueError(f"{key} contains an invalid URL")
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in schemes and bool(parsed.hostname)
                parsed.port  # Validate a supplied port without logging the URL.
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(f"{key} requires a valid {'/'.join(schemes)} URL")
    return settings


def read_config(path):
    # sing-box's merger retains JSONC comments. Match strings first so URLs,
    # escaped quotes and comment delimiters in credentials remain untouched.
    string = r'"(?:\\.|[^"\\])*"'
    source = re.sub(string + r'|//[^\r\n]*|/\*[\s\S]*?\*/',
                    lambda m: m[0] if m[0].startswith('"') else " ", Path(path).read_text())
    source = re.sub(string + r'|,\s*(?=[}\]])',
                    lambda m: m[0] if m[0].startswith('"') else "", source)
    return json.loads(source)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def selector_tag(config):
    selectors = [o for o in config.get("outbounds", []) if o.get("type") == "selector"]
    if len(selectors) == 1:
        return selectors[0].get("tag") or None
    return None


def prepare(config):
    tag = selector_tag(config)
    if tag is not None:
        api = config.setdefault("experimental", {}).setdefault("clash_api", {})
        if not api.get("external_controller"):
            api["external_controller"] = "127.0.0.1:9090"
        for outbound in config["outbounds"]:
            if outbound.get("tag") == tag:
                outbound["interrupt_exist_connections"] = True


class Client:
    def __init__(self, config, settings=None):
        settings = settings or DEFAULT_SETTINGS
        self.query = urlencode({"url": settings["healthcheck_url"], "timeout": 5000})
        api = config["experimental"]["clash_api"]
        address = urlsplit("//" + api["external_controller"])
        host = address.hostname
        if not host or address.port is None or address.path or address.username:
            raise ValueError("unsupported Clash API address")
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in host:
            host = "[" + host + "]"
        self.base_url = f"http://{host}:{address.port}"
        self.secret = api.get("secret", "")
        self.path = "/proxies/" + quote(selector_tag(config), safe="")
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, data=None):
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        body = None if data is None else json.dumps(data).encode()
        req = Request(self.base_url + path, data=body, headers=headers,
                      method="GET" if data is None else "PUT")
        with self.opener.open(req, timeout=7) as response:
            body = response.read()
            return json.loads(body) if body else None

    def selection(self):
        result = self.request(self.path)
        current, tags = result["now"], result["all"]
        if (not isinstance(tags, list) or not tags or
                any(not isinstance(tag, str) or not tag for tag in tags) or
                current not in tags):
            raise ValueError("invalid selector response")
        return current, tags

    def measure(self, tag):
        try:
            result = self.request("/proxies/" + quote(tag, safe="") + "/delay?" + self.query)
            delay = result["delay"]
            if type(delay) is not int or delay <= 0:
                raise ValueError("invalid delay response")
            return delay
        except HTTPError as error:
            if error.code not in (503, 504):
                raise
            error.close()
            return None

    def select(self, tag):
        self.request(self.path, {"name": tag})


def tunnel_addresses(config):
    def single(value):
        if isinstance(value, list):
            return value[0] if len(value) == 1 else None
        return value

    endpoints = [e for e in config.get("endpoints", [])
                 if e.get("type") == "wireguard" and e.get("system") is True]
    if len(endpoints) != 1 or len(endpoints[0].get("peers", [])) != 1:
        return None
    endpoint = endpoints[0]
    if endpoint.get("detour") != selector_tag(config) or not endpoint.get("detour"):
        return None
    try:
        local = ipaddress.ip_interface(single(endpoint.get("address"))).ip
        network = ipaddress.ip_network(single(endpoint["peers"][0].get("allowed_ips")), strict=False)
        if network.version != 4 or not 1 <= network.prefixlen <= 30 or local not in network:
            return None
        peer = network.network_address + 1
        if peer != local:
            return str(local), str(peer)
    except (ValueError, TypeError):
        pass
    return None


class Network:
    def __init__(self, config, settings=None, progress=None):
        self.settings = settings or DEFAULT_SETTINGS
        self.progress = progress or (lambda: None)
        self.addresses = tunnel_addresses(config)
        self.interface = None

    @staticmethod
    def command(args, timeout=5):
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=timeout)

    def tunnel(self):
        if self.addresses is None:
            return None
        local, peer = self.addresses
        result = self.command(["ip", "-j", "-4", "addr", "show"])
        interfaces = json.loads(result.stdout) if result.returncode == 0 else []
        self.interface = next((i["ifname"] for i in interfaces
                               if any(a.get("local") == local for a in i.get("addr_info", []))), None)
        return bool(self.interface and self.command(
            ["ping", "-I", self.interface, "-c", "1", "-W", "2", peer]).returncode == 0)

    def resolve(self, host):
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        # Isolate getaddrinfo: its system DNS timeout is not bounded by a
        # Python socket timeout, and must not stall the recovery controller.
        try:
            result = self.command([
                sys.executable, "-c",
                "import json,socket,sys; print(json.dumps(list(dict.fromkeys("
                "r[4][0] for r in socket.getaddrinfo(sys.argv[1],None,type=socket.SOCK_STREAM)))))",
                host], timeout=3)
        except subprocess.TimeoutExpired:
            return []
        return json.loads(result.stdout) if result.returncode == 0 else []

    def direct_internet(self):
        for url in self.settings["internet_check_urls"]:
            self.progress()
            parsed = urlsplit(url)
            host = parsed.hostname
            for address in self.resolve(host):
                self.progress()
                route = self.command(["ip", "-j", "route", "get", address])
                if route.returncode:
                    continue
                routes = json.loads(route.stdout)
                interface = routes[0].get("dev") if routes else None
                if not interface or interface == self.interface:
                    continue
                args = ["curl", "--silent", "--insecure", "--output", "/dev/null",
                        "--noproxy", "*", "--interface", interface,
                        "--connect-timeout", "1", "--max-time", "2"]
                if address != host:
                    port = parsed.port or (443 if parsed.scheme == "https" else 80)
                    destination = f"[{address}]" if ":" in address else address
                    args += ["--resolve", f"{host}:{port}:{destination}"]
                result = self.command(args + [url])
                self.progress()
                if result.returncode == 0:
                    return True
        return False

    def restart(self):
        subprocess.run(["s6-svc", "-r", "/run/service/sing-box"], check=True, timeout=10)


class Controller:
    def __init__(self, api, network, marker, identity, now, progress=None, generation_valid=None, clock=None,
                 settings=None):
        settings = settings or DEFAULT_SETTINGS
        self.grace = settings["recovery_grace"]
        self.tolerance = settings["healthcheck_tolerance"]
        self.api = api
        self.network = network
        self.marker = Path(marker)
        self.identity = identity
        self.progress = progress or (lambda: None)
        self.generation_valid = generation_valid or (lambda: True)
        self.clock = clock
        self.pending = None
        self.current = None
        self.failures = 0
        self.grace_until = now + self.grace
        try:
            self.restarted = json.loads(self.marker.read_text()).get("identity") == identity
        except FileNotFoundError:
            self.restarted = False
        except (ValueError, AttributeError):
            # A damaged recovery budget must not enable a restart loop.
            self.restarted = True

    def timestamp(self, fallback):
        return self.clock() if self.clock is not None else fallback

    def selection_unchanged(self, current, tags, now):
        selected, members = self.api.selection()
        if (selected, members) == (current, tags):
            return True
        self.current = selected
        self.grace_until = self.timestamp(now) + self.grace
        self.pending = None
        self.failures = 0
        return False

    def api_error(self, error):
        if isinstance(error, HTTPError):
            error.close()
        self.pending = None
        self.failures = 0
        print(f"[WARN] Outbound API error: {type(error).__name__}: {error}", flush=True)
        return "api_error"

    def step(self, now):
        tunnel_up = self.network.tunnel()
        if tunnel_up:
            self.marker.unlink(missing_ok=True)
            self.restarted = False
        try:
            current, tags = self.api.selection()
            if self.current is not None and current != self.current:
                self.grace_until = self.timestamp(now) + self.grace
                self.failures = 0
                self.pending = None
            self.current = current
            with ThreadPoolExecutor(max_workers=min(8, len(tags))) as pool:
                futures = {pool.submit(self.api.measure, tag): tag for tag in tags}
                delays = {}
                try:
                    for future in as_completed(futures):
                        self.progress()
                        delays[futures[future]] = future.result()
                finally:
                    # An API error must not wait for all queued timeout batches.
                    for future in futures:
                        future.cancel()
            now = self.timestamp(now)
            if not self.generation_valid():
                self.pending = None
                self.failures = 0
                return "service_changed"
            if not self.selection_unchanged(current, tags, now):
                return "selection_changed"
            print(f"[INFO] Outbound healthcheck: current={current} delays_ms={delays}", flush=True)
            available = [tag for tag in tags if delays[tag] is not None]
            best = min(available, key=delays.get) if available else None
            should_switch = best is not None and best != current and (
                delays[current] is None or (
                    tunnel_up is not False and now >= self.grace_until and
                    delays[current] - delays[best] >= self.tolerance))
            if should_switch:
                candidate = (current, best, tuple(tags))
                if self.pending == candidate:
                    self.api.select(best)
                    self.current = best
                    self.grace_until = self.timestamp(now) + self.grace
                    self.failures = 0
                    self.pending = None
                    print(f"[INFO] Outbound switched: {current} -> {best}", flush=True)
                    return "recovering"
                self.pending = candidate
            else:
                self.pending = None
        except (OSError, ValueError, KeyError, TypeError) as error:
            return self.api_error(error)

        if tunnel_up or tunnel_up is None or delays[current] is None or now < self.grace_until:
            self.failures = 0
            if not available:
                return "outbounds_unavailable"
            if tunnel_up is None:
                return "outbound_only"
            return "healthy" if tunnel_up else "recovering"
        if not self.network.direct_internet():
            self.failures = 0
            return "internet_unavailable"
        self.failures += 1
        if self.failures >= 3 and not self.restarted:
            # Direct Internet probes take time too. Revalidate both boundaries
            # immediately before spending the persistent recovery attempt.
            try:
                if not self.selection_unchanged(current, tags, now):
                    return "selection_changed"
            except (OSError, ValueError, KeyError, TypeError) as error:
                return self.api_error(error)
            if not self.generation_valid():
                self.pending = None
                self.failures = 0
                return "service_changed"
            atomic_json(self.marker, {"identity": self.identity})
            self.restarted = True
            self.pending = None
            self.failures = 0
            self.grace_until = self.timestamp(now) + self.grace
            print("[WARN] Tunnel still down with a working outbound and Internet; restarting sing-box once", flush=True)
            self.network.restart()
            return "restarting"
        return "tunnel_down"


def heartbeat_fresh(path, now):
    try:
        updated = json.loads(Path(path).read_text())["updated"]
        return (type(updated) in (float, int) and math.isfinite(updated) and
                0 <= now - updated <= 120)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def generation_matches(path, expected):
    try:
        return json.loads(Path(path).read_text())["generation"] == expected
    except (OSError, ValueError, KeyError, TypeError):
        return False


def wait_interval(deadline, progress, clock=time.monotonic, sleep=time.sleep):
    while (remaining := deadline - clock()) > 0:
        sleep(min(30, remaining))
        progress()


def run(runtime, state):
    settings = load_settings(os.environ.get("OPTIONS_FILE", "/data/options.json"))
    controller = None
    generation = None

    def progress():
        atomic_json(runtime / "controller.json", {"updated": time.monotonic(), "status": "checking"})

    while True:
        started = time.monotonic()
        status = "waiting_for_sing_box"
        progress()
        try:
            ready_path = runtime / "ready.json"
            if ready_path.exists():
                ready = json.loads(ready_path.read_text())
                if ready["generation"] != generation:
                    config = json.loads(Path(ready["config"]).read_text())
                    network = Network(config, settings=settings, progress=progress)
                    tag = selector_tag(config)
                    identity = json.dumps([tag, network.addresses])
                    controller = (Controller(
                        Client(config, settings=settings), network, state / "recovery.json", identity, started,
                        progress=progress, clock=time.monotonic, settings=settings,
                        generation_valid=lambda expected=ready["generation"]: generation_matches(ready_path, expected))
                                  if tag else None)
                    generation = ready["generation"]
                    if not tag:
                        print("[WARN] Controller needs exactly one tagged selector; automatic recovery disabled", flush=True)
                    elif network.addresses is None:
                        print("[WARN] Unsupported WireGuard topology; managing outbounds only", flush=True)
                status = controller.step(started) if controller else "unmanaged"
            else:
                controller = None
                generation = None
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            if controller:
                controller.pending = None
                controller.failures = 0
            status = "controller_error"
            print(f"[WARN] Controller error: {type(error).__name__}: {error}", flush=True)
        def idle_heartbeat():
            atomic_json(runtime / "controller.json", {"updated": time.monotonic(), "status": status})

        idle_heartbeat()
        wait_interval(started + settings["healthcheck_interval"], idle_heartbeat)


if __name__ == "__main__":
    runtime = Path(os.environ.get("RUNTIME_DIR", "/run/sing-box"))
    state = Path(os.environ.get("STATE_DIR", "/data/state"))
    command = sys.argv[1]
    if command == "prepare":
        path = Path(sys.argv[2])
        config = read_config(path)
        prepare(config)
        atomic_json(path, config)
    elif command == "ready":
        atomic_json(runtime / "ready.json", {"generation": time.monotonic_ns(), "config": sys.argv[2]})
    elif command == "watchdog":
        sys.exit(0 if heartbeat_fresh(runtime / "controller.json", time.monotonic()) else 1)
    elif command == "run":
        run(runtime, state)
    else:
        raise SystemExit("unknown controller command")
