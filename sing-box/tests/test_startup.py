import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "rootfs/etc/services.d/sing-box/run"
CONTROLLER = ROOT / "rootfs/usr/local/bin/sing-box-controller.py"


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.config = self.root / "config"
        self.config.mkdir()
        self.runtime = self.root / "runtime"
        self.log = self.root / "commands.jsonl"
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps({"outbounds": [
            {"type": "selector", "tag": "custom", "outbounds": ["direct"]},
            {"type": "direct", "tag": "direct"}]}))
        options = self.root / "options.json"
        options.write_text('{"config_url":"https://test.invalid/config"}')
        self.env = dict(os.environ, CONFIG_DIR=str(self.config), STATE_DIR=str(self.root / "state"),
                        RUNTIME_DIR=str(self.runtime), OPTIONS_FILE=str(options),
                        CONTROLLER_SCRIPT=str(CONTROLLER), TEST_LOG=str(self.log),
                        TEST_SOURCE=str(self.source), PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        self.executable("curl", '''
import os, pathlib, sys
if os.environ.get("FAIL_DOWNLOAD"):
    sys.exit(1)
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_bytes(pathlib.Path(os.environ["TEST_SOURCE"]).read_bytes())
''')
        self.executable("sing-box", '''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["TEST_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[0] == "check":
    try:
        json.loads(pathlib.Path(args[args.index("-c") + 1]).read_text())
    except ValueError:
        sys.exit(1)
elif args[0] == "merge":
    source = pathlib.Path(args[args.index("-C") + 1]) / "config.json"
    pathlib.Path(args[1]).write_bytes(source.read_bytes())
''')

    def executable(self, name, body):
        import sys
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)

    def start(self):
        result = subprocess.run(["sh", str(RUN)], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_download_is_cached_unchanged_and_runtime_is_prepared(self):
        self.start()
        self.assertEqual((self.config / "config.json").read_bytes(), self.source.read_bytes())
        prepared = json.loads((self.runtime / "config.json").read_text())
        self.assertEqual(prepared["experimental"]["clash_api"]["external_controller"], "127.0.0.1:9090")
        self.assertTrue(prepared["outbounds"][0]["interrupt_exist_connections"])
        self.assertTrue((self.runtime / "ready.json").exists())

    def test_cached_source_is_prepared_when_download_fails(self):
        (self.config / "config.json").write_bytes(self.source.read_bytes())
        self.env["FAIL_DOWNLOAD"] = "1"
        self.start()
        self.assertTrue((self.runtime / "config.json").exists())
        self.assertEqual((self.config / "config.json").read_bytes(), self.source.read_bytes())

    def test_invalid_download_uses_cached_source(self):
        (self.config / "config.json").write_bytes(self.source.read_bytes())
        self.source.write_text("broken")
        self.start()
        self.assertTrue((self.runtime / "ready.json").exists())

    def test_service_restart_reuses_runtime_without_download(self):
        self.start()
        self.env["FAIL_DOWNLOAD"] = "1"
        Path(self.env["OPTIONS_FILE"]).unlink()
        previous = json.loads((self.runtime / "ready.json").read_text())["generation"]
        self.log.unlink()
        commands = self.start()
        self.assertFalse(any(command[0] == "merge" for command in commands))
        self.assertNotEqual(json.loads((self.runtime / "ready.json").read_text())["generation"], previous)

    def test_finish_clears_readiness(self):
        self.runtime.mkdir()
        ready = self.runtime / "ready.json"
        ready.write_text("{}")
        subprocess.run(["sh", str(ROOT / "rootfs/etc/services.d/sing-box/finish")],
                       env=self.env, capture_output=True, check=True)
        self.assertFalse(ready.exists())


if __name__ == "__main__":
    unittest.main()
