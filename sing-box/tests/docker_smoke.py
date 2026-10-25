"""Exercise the built add-on with real s6 and Clash API, without a HA instance.

Run: python3 sing-box/tests/docker_smoke.py [sing-box-addon:recovery-test]
Requires Docker and Internet access for the real HTTPS outbound delay probe.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


IMAGE = sys.argv[1] if len(sys.argv) > 1 else "sing-box-addon:recovery-test"
NAME = "sing-box-recovery-smoke-" + uuid.uuid4().hex[:8]


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout.strip()


def execute(*args):
    return docker("exec", NAME, *args)


def wait_until(check):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except RuntimeError:
            pass
        time.sleep(0.25)
    raise AssertionError("Timed out waiting for service state")


def generation():
    return json.loads(execute("cat", "/run/sing-box/ready.json"))["generation"]


def health():
    return execute("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                   "http://127.0.0.1:18787/health")


with tempfile.TemporaryDirectory(prefix="sing-box-smoke-", dir="/tmp") as tmp:
    fixture = Path(tmp)
    source = fixture / "source.json"
    source.write_text('''{
      // Exercise sing-box's JSONC reader before Python sees the config.
      "outbounds": [
        {"type":"selector","tag":"Choice","outbounds":["dead","backup"],"default":"dead"},
        {"type":"socks","tag":"dead","server":"127.0.0.1","server_port":1},
        {"type":"direct","tag":"backup"}
      ]
    }''')
    (fixture / "options.json").write_text('{"config_url":"file:///fixtures/source.json"}')
    try:
        docker("run", "--detach", "--name", NAME,
               "--mount", f"type=bind,source={fixture},target=/fixtures,readonly",
               "--mount", f"type=bind,source={fixture / 'options.json'},target=/data/options.json,readonly",
               IMAGE)
        wait_until(lambda: generation() > 0)
        wait_until(lambda: health() == "200")
        assert execute("s6-svstat", "-o", "up", "/run/service/sing-box") == "true"
        execute("s6-svc", "-d", "/run/service/controller")
        wait_until(lambda: execute("s6-svstat", "-o", "up", "/run/service/controller") == "false")

        # Exercise the real delay endpoints and selector PUT. No WireGuard is
        # configured in this isolated container; recovery policy has unit tests.
        execute("python3", "-c", '''
import json, pathlib, runpy
m = runpy.run_path('/usr/local/bin/sing-box-controller.py')
config = json.loads(pathlib.Path('/run/sing-box/config.json').read_text())
client = m['Client'](config)
client.select('dead')
assert client.measure('dead') is None
assert client.measure('backup') > 0
class NoTunnel:
    def tunnel(self): return None
controller = m['Controller'](client, NoTunnel(), '/data/state/recovery.json', 'smoke', 0)
controller.step(100)
assert client.selection()[0] == 'dead'
controller.step(130)
assert client.selection()[0] == 'backup'
''')
        print("Real Clash API delay probes and two-check failover: OK", flush=True)

        execute("python3", "-c", '''
import runpy, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
m = runpy.run_path('/usr/local/bin/sing-box-controller.py')
hits = []
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        hits.append((self.path, self.headers['Host']))
        self.send_response(204)
        self.end_headers()
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    url = f'http://localhost:{server.server_port}/probe?check=1'
    settings = dict(m['DEFAULT_SETTINGS'], internet_check_urls=[url])
    network = m['Network']({}, settings=settings)
    network.interface = 'wg0'
    assert network.direct_internet()
    assert hits == [('/probe?check=1', f'localhost:{server.server_port}')], hits
finally:
    server.shutdown()
    server.server_close()
''')
        print("Direct URL hostname resolution, IP routing and curl host preservation: OK", flush=True)

        # The HTTP endpoint must be a read-only liveness adapter, including
        # concurrent requests while the network is degraded.
        execute("python3", "-c", '''
import runpy, time
m = runpy.run_path('/usr/local/bin/sing-box-controller.py')
m['atomic_json']('/run/sing-box/controller.json', {'updated': time.monotonic() - 121, 'status': 'healthy'})
''')
        assert health() == "503"
        execute("python3", "-c", '''
import runpy, time
m = runpy.run_path('/usr/local/bin/sing-box-controller.py')
m['atomic_json']('/run/sing-box/controller.json', {'updated': time.monotonic(), 'status': 'outbounds_unavailable'})
''')
        original = execute("cat", "/run/sing-box/controller.json")
        with ThreadPoolExecutor(max_workers=6) as pool:
            assert list(pool.map(lambda _: health(), range(12))) == ["200"] * 12
        assert execute("cat", "/run/sing-box/controller.json") == original
        print("Stale heartbeat / concurrent degraded HTTP checks: OK", flush=True)

        # Break the remote source: local service recovery must not download it.
        old_generation = generation()
        source.write_text("broken")
        execute("python3", "-c", '''
import runpy
m = runpy.run_path('/usr/local/bin/sing-box-controller.py')
m['Network']({}).restart()
''')
        wait_until(lambda: generation() != old_generation)
        assert execute("s6-svstat", "-o", "up", "/run/service/sing-box") == "true"
        print("Real s6 service restart reuses runtime config: OK", flush=True)

        # s6 must supervise the controller itself.
        execute("s6-svc", "-u", "/run/service/controller")
        wait_until(lambda: execute("s6-svstat", "-o", "up", "/run/service/controller") == "true")
        pid = execute("s6-svstat", "-o", "pid", "/run/service/controller")
        execute("kill", "-9", pid)
        wait_until(lambda: execute("s6-svstat", "-o", "pid", "/run/service/controller") not in (pid, "-1"))
        wait_until(lambda: health() == "200")
        execute("s6-svc", "-d", "/run/service/sing-box")
        wait_until(lambda: execute("sh", "-c", 'test ! -e /run/sing-box/ready.json && echo removed') == "removed")
        print("Controller crash recovery and sing-box readiness cleanup: OK", flush=True)
    except Exception:
        logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True, timeout=30)
        print(logs.stdout + logs.stderr, file=sys.stderr)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", NAME], capture_output=True, timeout=30)
