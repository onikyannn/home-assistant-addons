import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit, parse_qs

from test_controller import load_controller


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.module = load_controller()
        self.current = "Naïve / west"
        self.status = 200
        self.delay = 75
        self.expected_query = {"url": ["https://www.gstatic.com/generate_204"], "timeout": ["5000"]}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def authorized(self):
                if self.headers.get("Authorization") != "Bearer test-secret":
                    self.reply(401, {"message": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                path = urlsplit(self.path)
                if unquote(path.path) == "/proxies/Choice / 東京":
                    self.reply(200, {"type": "Selector", "name": "Choice / 東京",
                                     "now": test.current, "all": ["Naïve / west", "primary"],
                                     "history": [], "udp": True})
                elif unquote(path.path) == "/proxies/Naïve / west/delay":
                    query = parse_qs(path.query)
                    if query == test.expected_query:
                        self.reply(test.status, {"delay": test.delay})
                    else:
                        self.reply(400, {})
                else:
                    self.reply(404, {})

            def do_PUT(self):
                if not self.authorized():
                    return
                if unquote(self.path) != "/proxies/Choice / 東京":
                    self.reply(404, {})
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.current = body["name"]
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = {
            "outbounds": [{"type": "selector", "tag": "Choice / 東京"}],
            "experimental": {"clash_api": {
                "external_controller": f"127.0.0.1:{self.server.server_port}", "secret": "test-secret"}}}
        self.client = self.module.Client(self.config)

    def test_real_http_reads_unicode_tags_and_updates_selector(self):
        self.assertEqual(self.client.selection(), ("Naïve / west", ["Naïve / west", "primary"]))
        self.assertEqual(self.client.measure("Naïve / west"), 75)
        self.client.select("primary")
        self.assertEqual(self.client.selection()[0], "primary")

    def test_configured_probe_url_reaches_the_api(self):
        self.expected_query = {"url": ["https://probe.example/custom?hello=world"], "timeout": ["5000"]}
        settings = {**self.module.DEFAULT_SETTINGS, "healthcheck_url": self.expected_query["url"][0]}
        client = self.module.Client(self.config, settings=settings)
        self.assertEqual(client.measure("Naïve / west"), 75)

    def test_only_outbound_failures_are_treated_as_unavailable(self):
        for status in (503, 504):
            self.status = status
            self.assertIsNone(self.client.measure("Naïve / west"))
        for status in (400, 401, 500):
            self.status = status
            with self.assertRaises(HTTPError) as caught:
                self.client.measure("Naïve / west")
            caught.exception.close()

    def test_api_authentication_error_is_not_a_failed_outbound(self):
        self.client.secret = "wrong"
        with self.assertRaises(HTTPError) as caught:
            self.client.measure("Naïve / west")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def test_malformed_delay_is_an_api_error(self):
        for delay in (0, -1, True, "75", None):
            self.delay = delay
            with self.assertRaises(ValueError):
                self.client.measure("Naïve / west")


if __name__ == "__main__":
    unittest.main()
