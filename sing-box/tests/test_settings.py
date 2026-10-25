import json
import contextlib
import io
import os
import ipaddress
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess
import subprocess

from test_controller import API, Network, load_controller


class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_controller()

    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_old_options_keep_the_existing_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "options.json"
            path.write_text('{"config_url":"https://config.example/config.json"}')
            settings = self.module.load_settings(path)
        self.assertEqual(settings, {
            "healthcheck_url": "https://www.gstatic.com/generate_204",
            "internet_check_urls": ["https://1.1.1.1/", "https://8.8.8.8/"],
            "healthcheck_tolerance": 50, "healthcheck_interval": 30, "recovery_grace": 90})

    def test_invalid_values_fail_with_the_option_name(self):
        for key, value in (("healthcheck_interval", 0), ("recovery_grace", -1),
                           ("healthcheck_interval", 3601), ("healthcheck_tolerance", True),
                           ("healthcheck_tolerance", -1), ("healthcheck_url", "http://probe.example/"),
                           ("internet_check_urls", []), ("internet_check_urls", ["file:///tmp/test"])):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "options.json"
                path.write_text(json.dumps({key: value}))
                with self.assertRaisesRegex(ValueError, key):
                    self.module.load_settings(path)

    def test_custom_grace_and_latency_tolerance_drive_switching(self):
        settings = {**self.module.DEFAULT_SETTINGS, "recovery_grace": 20, "healthcheck_tolerance": 100}
        api, network = API(), Network()
        api.current = "backup west"
        api.delays["primary"] = 81
        with tempfile.TemporaryDirectory() as tmp:
            controller = self.module.Controller(api, network, Path(tmp) / "marker", "test", 0,
                                                settings=settings)
            self.assertEqual(controller.grace_until, 20)
            controller.step(20)
            controller.step(25)
            self.assertEqual(api.switches, [])
            api.delays["primary"] = 80
            controller.step(30)
            controller.step(35)
            self.assertEqual(api.switches, ["primary"])
            self.assertEqual(controller.grace_until, 55)

    def test_direct_internet_uses_custom_urls_on_physical_interface(self):
        settings = {**self.module.DEFAULT_SETTINGS, "internet_check_urls": ["https://probe.example/status"]}
        network = self.module.Network({}, settings=settings)
        network.interface = "wg0"
        commands = []

        def command(args, timeout=5):
            commands.append((args, timeout))
            if args[0] == "ip":
                ipaddress.ip_address(args[-1])  # iproute2 accepts numeric destinations only.
            return CompletedProcess(args, 0, '[{"dev":"eth0"}]' if args[0] == "ip" else "")

        with patch.object(network, "command", command), \
                patch.object(network, "resolve", return_value=["203.0.113.7"]):
            self.assertTrue(network.direct_internet())
        self.assertEqual(commands[0][0][-1], "203.0.113.7")
        curl, timeout = commands[1]
        self.assertEqual(curl[-1], "https://probe.example/status")
        self.assertEqual(curl[curl.index("--max-time") + 1], "2")
        self.assertEqual(curl[curl.index("--interface") + 1], "eth0")
        self.assertEqual(curl[curl.index("--resolve") + 1], "probe.example:443:203.0.113.7")
        self.assertGreater(timeout, 2)

    def test_real_dns_resolver_handles_localhost_and_numeric_urls(self):
        network = self.module.Network({})
        self.assertEqual(network.resolve("1.1.1.1"), ["1.1.1.1"])
        resolved = network.resolve("localhost")
        self.assertTrue(resolved)
        self.assertTrue(all(ipaddress.ip_address(address).is_loopback for address in resolved))

    def test_dns_failure_is_bounded_and_skips_to_the_next_internet_url(self):
        settings = {**self.module.DEFAULT_SETTINGS,
                    "internet_check_urls": ["https://unresolved.example/", "https://1.1.1.1/"]}
        network = self.module.Network({}, settings=settings)
        commands = []

        def command(args, timeout=5):
            commands.append(args)
            if args[0] != "ip" and args[0] != "curl":
                self.assertLessEqual(timeout, 5)
                raise subprocess.TimeoutExpired(args, timeout)
            return CompletedProcess(args, 0, '[{"dev":"eth0"}]' if args[0] == "ip" else "")

        with patch.object(network, "command", command):
            self.assertTrue(network.direct_internet())
        curls = [command for command in commands if command[0] == "curl"]
        self.assertEqual(len(curls), 1)
        self.assertEqual(curls[0][-1], "https://1.1.1.1/")

    def test_long_interval_keeps_watchdog_fresh_without_shortening_the_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controller.json"
            clock = [0]

            def heartbeat():
                self.module.atomic_json(path, {"updated": clock[0], "status": "healthy"})

            def sleep(seconds):
                clock[0] += seconds
                self.assertTrue(self.module.heartbeat_fresh(path, clock[0]))

            heartbeat()
            self.module.wait_interval(600, heartbeat, clock=lambda: clock[0], sleep=sleep)
            self.assertEqual(clock[0], 600)
            self.assertTrue(self.module.heartbeat_fresh(path, 600))

    def test_run_reads_gui_interval_and_tolerance_from_options(self):
        api, network = API(), Network()
        api.current = "backup west"
        api.delays["primary"] = 81  # 99 ms advantage is below the configured 100 ms.
        network.addresses = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            config = root / "config.json"
            config.write_text('{"outbounds":[{"type":"selector","tag":"select"}]}')
            options = root / "options.json"
            options.write_text('{"healthcheck_interval":7,"recovery_grace":0,"healthcheck_tolerance":100}')
            self.module.atomic_json(runtime / "ready.json", {"generation": 1, "config": str(config)})
            clock, deadlines = [0], []

            def wait(deadline, progress):
                deadlines.append(deadline)
                clock[0] = deadline
                if len(deadlines) == 20:
                    raise SystemExit

            with patch.dict(os.environ, {"OPTIONS_FILE": str(options)}), \
                    patch.object(self.module, "Client", lambda config, **kwargs: api), \
                    patch.object(self.module, "Network", lambda config, **kwargs: network), \
                    patch.object(self.module.time, "monotonic", lambda: clock[0]), \
                    patch.object(self.module, "wait_interval", wait), self.assertRaises(SystemExit):
                self.module.run(runtime, root / "state")
            self.assertEqual(deadlines[:3], [7, 14, 21])
            self.assertEqual(api.switches, [])


if __name__ == "__main__":
    unittest.main()
