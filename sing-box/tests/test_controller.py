import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.error import HTTPError


MODULE = Path(__file__).resolve().parents[1] / "rootfs/usr/local/bin/sing-box-controller.py"


def load_controller():
    spec = importlib.util.spec_from_file_location("controller", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class API:
    def __init__(self):
        self.current = "primary"
        self.delays = {"primary": 80, "backup west": 180}
        self.switches = []
        self.error = None
        self.on_measure = None

    def selection(self):
        if self.error:
            raise self.error
        return self.current, list(self.delays)

    def measure(self, tag):
        if self.on_measure:
            self.on_measure()
        return self.delays[tag]

    def select(self, tag):
        self.current = tag
        self.switches.append(tag)


class Network:
    def __init__(self):
        self.up = True
        self.internet = True
        self.restarts = 0

    def tunnel(self):
        return self.up

    def direct_internet(self):
        return self.internet

    def restart(self):
        self.restarts += 1


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_controller()

    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / "recovery.json"
        self.api = API()
        self.net = Network()
        self.controller = self.make_controller()

    def make_controller(self, now=0, **kwargs):
        return self.module.Controller(self.api, self.net, self.marker, "wg-test", now, **kwargs)

    def test_failover_needs_two_confirmations_and_reads_members_each_cycle(self):
        self.api.delays["primary"] = None
        self.controller.step(100)
        self.assertEqual(self.api.switches, [])
        self.api.delays = {"primary": None, "Naïve / west": 90}
        self.controller.step(130)
        self.assertEqual(self.api.switches, [])
        self.controller.step(160)
        self.assertEqual(self.api.switches, ["Naïve / west"])

    def test_latency_advantage_of_50_ms_requires_two_checks(self):
        self.api.current = "backup west"
        self.api.delays = {"primary": 130, "backup west": 180}
        self.controller.step(100)
        self.assertEqual(self.api.switches, [])
        self.controller.step(130)
        self.assertEqual(self.api.switches, ["primary"])

    def test_small_advantage_does_not_switch(self):
        self.api.current = "backup west"
        self.api.delays["primary"] = 131
        self.controller.step(100)
        self.controller.step(130)
        self.assertEqual(self.api.switches, [])

    def test_no_latency_switch_while_tunnel_down_or_in_grace(self):
        self.api.current = "backup west"
        self.controller.step(0)
        self.controller.step(30)
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)
        self.assertEqual(self.api.switches, [])

    def test_outage_or_recovery_resets_pending_switch(self):
        self.api.delays["primary"] = None
        self.controller.step(100)
        self.api.delays["backup west"] = None
        self.controller.step(130)
        self.api.delays["backup west"] = 100
        self.controller.step(160)
        self.assertEqual(self.api.switches, [])
        self.api.delays["primary"] = 80
        self.controller.step(190)
        self.api.delays["primary"] = None
        self.controller.step(220)
        self.assertEqual(self.api.switches, [])

    def test_manual_change_during_measurement_cancels_switch(self):
        self.api.delays["primary"] = None
        self.controller.step(100)
        self.api.on_measure = lambda: setattr(self.api, "current", "backup west")
        self.controller.step(130)
        self.assertEqual(self.api.switches, [])

    def test_switch_grace_prevents_premature_tunnel_restart(self):
        self.net.up = False
        self.api.delays["primary"] = None
        self.controller.step(100)
        self.controller.step(130)
        self.controller.step(160)
        self.controller.step(190)
        self.assertEqual(self.net.restarts, 0)
        self.controller.step(220)
        self.controller.step(250)
        self.assertEqual(self.net.restarts, 0)
        self.controller.step(280)
        self.assertEqual(self.net.restarts, 1)

    def test_initial_grace_then_three_tunnel_failures(self):
        self.net.up = False
        for now in (0, 30, 60, 90, 120):
            self.controller.step(now)
        self.assertEqual(self.net.restarts, 0)
        self.controller.step(150)
        self.assertEqual(self.net.restarts, 1)

    def test_all_outbounds_down_never_restart(self):
        self.net.up = False
        self.api.delays = {"primary": None, "backup west": None}
        for now in range(100, 1000, 30):
            self.controller.step(now)
        self.assertEqual(self.net.restarts, 0)
        self.assertEqual(self.api.switches, [])

    def test_no_internet_resets_tunnel_failure_count(self):
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)
        self.net.internet = False
        self.controller.step(160)
        self.net.internet = True
        self.controller.step(190)
        self.controller.step(220)
        self.assertEqual(self.net.restarts, 0)
        self.controller.step(250)
        self.assertEqual(self.net.restarts, 1)

    def test_api_failure_cancels_pending_recovery(self):
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)
        self.api.error = HTTPError("/proxies", 401, "denied", {}, None)
        self.addCleanup(self.api.error.close)
        self.assertEqual(self.controller.step(160), "api_error")
        self.api.error = None
        self.controller.step(190)
        self.controller.step(220)
        self.assertEqual(self.net.restarts, 0)

    def test_restart_budget_survives_controller_and_service_restart(self):
        self.net.up = False
        for now in (100, 130, 160):
            self.controller.step(now)
        self.assertEqual(self.net.restarts, 1)
        self.controller = self.make_controller(170)
        for now in range(300, 1000, 30):
            self.controller.step(now)
        self.assertEqual(self.net.restarts, 1)
        self.net.up = True
        self.controller.step(1000)
        self.assertFalse(self.marker.exists())
        self.net.up = False
        for now in (1030, 1060, 1090):
            self.controller.step(now)
        self.assertEqual(self.net.restarts, 2)

    def test_no_tunnel_topology_still_allows_outbound_switching(self):
        self.net.up = None
        self.api.delays["primary"] = None
        self.controller.step(100)
        self.controller.step(130)
        for now in range(160, 500, 30):
            self.controller.step(now)
        self.assertEqual(self.api.switches, ["backup west"])
        self.assertEqual(self.net.restarts, 0)

    def test_manual_selection_gets_its_own_tunnel_grace(self):
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)
        self.api.current = "backup west"
        self.controller.step(160)
        self.controller.step(190)
        self.controller.step(220)
        self.assertEqual(self.net.restarts, 0)
        self.controller.step(250)
        self.controller.step(280)
        self.controller.step(310)
        self.assertEqual(self.net.restarts, 1)

    def test_failed_restart_command_still_consumes_budget(self):
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)
        with patch.object(self.net, "restart", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.controller.step(160)
        self.controller = self.make_controller(170)
        for now in (300, 330, 360, 390):
            self.controller.step(now)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.net.restarts, 0)

    def test_manual_change_during_probes_cancels_imminent_restart(self):
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)
        self.api.on_measure = lambda: setattr(self.api, "current", "backup west")
        self.assertEqual(self.controller.step(160), "selection_changed")
        self.assertEqual(self.net.restarts, 0)
        self.assertFalse(self.marker.exists())
        self.api.on_measure = None
        self.controller.step(190)
        self.controller.step(220)
        self.assertEqual(self.net.restarts, 0)

    def test_manual_change_during_internet_probe_cancels_imminent_restart(self):
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)

        def internet():
            self.api.current = "backup west"
            return True

        with patch.object(self.net, "direct_internet", internet):
            self.assertEqual(self.controller.step(160), "selection_changed")
        self.assertEqual(self.net.restarts, 0)
        self.assertFalse(self.marker.exists())

    def test_probe_progress_refreshes_real_watchdog_state(self):
        snapshot = Path(self.tmp.name) / "controller.json"
        self.module.atomic_json(snapshot, {"updated": 0})
        release = threading.Event()
        progressed = threading.Event()
        self.api.delays = {"primary": None, **{f"backup-{i}": None for i in range(199)}}

        def progress():
            self.module.atomic_json(snapshot, {"updated": 200, "status": "checking"})
            progressed.set()

        def measure(tag):
            if tag != "primary":
                release.wait(5)
            return None

        self.controller = self.module.Controller(self.api, self.net, self.marker, "wg-test", 0,
                                                 progress=progress)
        with patch.object(self.api, "measure", measure), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.controller.step, 100)
            try:
                self.assertTrue(progressed.wait(2), "heartbeat must update before the group finishes")
                self.assertFalse(pending.done())
                self.assertTrue(self.module.heartbeat_fresh(snapshot, 200))
            finally:
                release.set()
            pending.result(timeout=5)

    def test_service_restart_during_probes_discards_old_tunnel_evidence(self):
        ready = Path(self.tmp.name) / "ready.json"
        self.module.atomic_json(ready, {"generation": 1})
        self.controller = self.make_controller(
            generation_valid=lambda: self.module.generation_matches(ready, 1))
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)
        self.api.on_measure = lambda: self.module.atomic_json(ready, {"generation": 2})
        self.assertEqual(self.controller.step(160), "service_changed")
        self.assertEqual(self.net.restarts, 0)
        self.assertFalse(self.marker.exists())

    def test_service_restart_during_internet_probe_cancels_recovery(self):
        ready = Path(self.tmp.name) / "ready.json"
        self.module.atomic_json(ready, {"generation": 1})
        self.controller = self.make_controller(
            generation_valid=lambda: self.module.generation_matches(ready, 1))
        self.net.up = False
        self.controller.step(100)
        self.controller.step(130)

        def internet():
            ready.unlink()
            return True

        with patch.object(self.net, "direct_internet", internet):
            self.assertEqual(self.controller.step(160), "service_changed")
        self.assertEqual(self.net.restarts, 0)
        self.assertFalse(self.marker.exists())

    def test_grace_starts_after_slow_measurements_finish(self):
        clock = [100]
        self.controller = self.make_controller(clock=lambda: clock[0])
        self.net.up = False
        self.api.delays["primary"] = None
        self.controller.step(100)
        self.api.on_measure = lambda: clock.__setitem__(0, 300)
        self.controller.step(130)
        self.assertEqual(self.api.switches, ["backup west"])
        self.assertEqual(self.controller.grace_until, 390)


class ConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_controller()

    def test_prepare_enables_only_loopback_api_and_interrupts_connections(self):
        config = {"outbounds": [{"type": "selector", "tag": "custom"}]}
        self.module.prepare(config)
        self.assertEqual(config["experimental"]["clash_api"]["external_controller"], "127.0.0.1:9090")
        self.assertIs(config["outbounds"][0]["interrupt_exist_connections"], True)

    def test_config_reader_preserves_strings_while_removing_jsonc_syntax(self):
        source = r'''{
            // A URL and comment delimiters inside strings must survive.
            "url": "https://example.test/a/*b*/?q=//,}",
            "escaped": "a\"//b",
            "array": [1, /* middle */ 2, // trailing
            ],
        }'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.json"
            path.write_text(source)
            self.assertEqual(self.module.read_config(path), {
                "url": "https://example.test/a/*b*/?q=//,}", "escaped": 'a"//b', "array": [1, 2]})

    def test_existing_api_and_secret_are_preserved(self):
        api = {"external_controller": "0.0.0.0:19090", "secret": "test-secret"}
        config = {"experimental": {"clash_api": dict(api)},
                  "outbounds": [{"type": "selector", "tag": "custom"}]}
        self.module.prepare(config)
        self.assertEqual(config["experimental"]["clash_api"], api)
        client = self.module.Client(config)
        self.assertEqual(client.base_url, "http://127.0.0.1:19090")
        self.assertEqual(client.secret, "test-secret")

    def test_ambiguous_selectors_are_not_automatically_managed(self):
        config = {"outbounds": [{"type": "selector", "tag": "one"},
                                {"type": "selector", "tag": "two"}]}
        self.module.prepare(config)
        self.assertNotIn("experimental", config)
        self.assertIsNone(self.module.selector_tag(config))

    def test_topology_uses_existing_first_usable_peer_convention(self):
        config = {"outbounds": [{"type": "selector", "tag": "select"}],
                  "endpoints": [{"type": "wireguard", "system": True, "detour": "select",
                  "address": ["10.4.5.2/24"], "peers": [{"allowed_ips": ["10.4.5.0/24"]}]}]}
        self.assertEqual(self.module.tunnel_addresses(config), ("10.4.5.2", "10.4.5.1"))
        config["endpoints"][0]["peers"][0]["allowed_ips"] = ["0.0.0.0/0"]
        self.assertIsNone(self.module.tunnel_addresses(config))

    def test_unrelated_selector_cannot_justify_a_tunnel_restart(self):
        config = {"outbounds": [{"type": "selector", "tag": "unrelated"}],
                  "endpoints": [{"type": "wireguard", "system": True, "detour": "other",
                  "address": ["10.4.5.2/24"], "peers": [{"allowed_ips": ["10.4.5.0/24"]}]}]}
        self.assertIsNone(self.module.tunnel_addresses(config))

    def test_network_internet_probe_excludes_wireguard_and_binds_to_direct_interface(self):
        network = self.module.Network({})
        network.interface = "wg0"
        from subprocess import CompletedProcess

        commands = []

        def command(args, timeout=5):
            commands.append(args)
            if args[0] == "ip":
                interface = "wg0" if args[-1] == "1.1.1.1" else "eth0"
                return CompletedProcess(args, 0, json.dumps([{"dev": interface}]))
            return CompletedProcess(args, 0, "")

        with patch.object(network, "command", command):
            self.assertTrue(network.direct_internet())
        curls = [c for c in commands if c[0] == "curl"]
        self.assertEqual(len(curls), 1)
        self.assertEqual(curls[0][-1], "https://8.8.8.8/")
        self.assertEqual(curls[0][curls[0].index("--interface") + 1], "eth0")

    def test_watchdog_is_read_only_and_ignores_network_degradation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            self.assertFalse(self.module.heartbeat_fresh(path, 100))
            self.module.atomic_json(path, {"updated": 100, "status": "outbounds_unavailable"})
            original = path.read_bytes()
            for _ in range(10):
                self.assertTrue(self.module.heartbeat_fresh(path, 220))
            self.assertFalse(self.module.heartbeat_fresh(path, 221))
            self.assertFalse(self.module.heartbeat_fresh(path, 99))
            self.assertEqual(path.read_bytes(), original)
            path.write_text("{broken")
            self.assertFalse(self.module.heartbeat_fresh(path, 100))


if __name__ == "__main__":
    unittest.main()
