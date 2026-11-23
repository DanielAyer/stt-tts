"""Doublet service over Zenoh. Run with: python3 -m unittest discover -s tests

The protocol tests need nothing. The service tests start a private Zenoh
router on a loopback port inside the test process and use the "none" engines,
so they need eclipse-zenoh but no models. They skip when zenoh is missing.
"""

import importlib.util
import json
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt_tts import config, protocol  # noqa: E402

HAS_ZENOH = importlib.util.find_spec("zenoh") is not None


class ProtocolTest(unittest.TestCase):
    def test_keys_layout(self):
        k = protocol.Keys("stt-tts", "echo/1")
        self.assertEqual(k.audio_in, "stt-tts/echo/1/audio/in")
        self.assertEqual(k.audio_out, "stt-tts/echo/1/audio/out")
        self.assertEqual(k.text_in, "stt-tts/echo/1/text/in")
        self.assertEqual(k.text_out, "stt-tts/echo/1/text/out")
        self.assertEqual(k.attach, "stt-tts/echo/1/attach")
        self.assertEqual(k.live_app, "stt-tts/live/echo/1/app")
        self.assertEqual(protocol.parse_live_key("stt-tts", k.live_term), ("echo/1", "term"))
        self.assertIn("attach", k.as_dict())

    def test_parse_live_key_opaque_and_junk(self):
        k = protocol.Keys("stt-tts", "0123abcd")
        self.assertEqual(protocol.parse_live_key("stt-tts", k.live_app), ("0123abcd", "app"))
        self.assertIsNone(protocol.parse_live_key("stt-tts", "stt-tts/live/app"))
        self.assertIsNone(protocol.parse_live_key("stt-tts", "stt-tts/live/echo/1/other"))
        self.assertIsNone(protocol.parse_live_key("stt-tts", "elsewhere/live/echo/1/app"))

    def test_app_slug_is_key_safe(self):
        self.assertEqual(protocol.app_slug("My App!"), "my-app")
        self.assertEqual(protocol.app_slug("a/b*c$d?e#f"), "a-b-c-d-e-f")
        self.assertEqual(protocol.app_slug("**"), "app")
        self.assertEqual(protocol.app_slug(""), "app")
        self.assertEqual(protocol.app_slug("x" * 100), "x" * 32)
        for reserved in protocol.RESERVED:
            self.assertEqual(protocol.app_slug(reserved.upper()), f"{reserved}-app")

    def test_readable_names_count_per_app_and_never_reuse(self):
        namer = protocol.Namer("readable")
        self.assertEqual(namer.allocate("echo"), "echo/1")
        self.assertEqual(namer.allocate("Echo"), "echo/2")
        self.assertEqual(namer.allocate("llm"), "llm/1")
        self.assertEqual(namer.allocate("echo"), "echo/3")

    def test_opaque_names_are_unique_single_segments(self):
        namer = protocol.Namer("opaque")
        ids = {namer.allocate("echo") for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all("/" not in i and len(i) == 32 for i in ids))

    def test_unknown_naming_mode_is_refused(self):
        with self.assertRaises(ValueError):
            protocol.Namer("uuid")

    def test_tokens_are_unique(self):
        self.assertEqual(len({protocol.new_token() for _ in range(200)}), 200)
        self.assertGreaterEqual(len(protocol.new_token()), 30)

    def test_encoding_rate_round_trip(self):
        self.assertEqual(protocol.rate_from_encoding(protocol.audio_out_encoding(24000)), 24000)
        self.assertEqual(protocol.rate_from_encoding(protocol.AUDIO_IN_ENCODING), 16000)

    def test_loads_tolerates_junk(self):
        self.assertEqual(protocol.loads(b"not json"), {})
        self.assertEqual(protocol.loads(b'{"a": 1}'), {"a": 1})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAS_ZENOH, "eclipse-zenoh not installed")
class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import zenoh

        from stt_tts.adapters.zenoh_adapter import open_session
        from stt_tts.service import VoiceService

        endpoint = f"tcp/127.0.0.1:{_free_port()}"
        rc = zenoh.Config()
        rc.insert_json5("mode", '"router"')
        rc.insert_json5("scouting/multicast/enabled", "false")
        rc.insert_json5("listen/endpoints", json.dumps([endpoint]))
        cls.router = zenoh.open(rc)

        cfg = config.defaults()
        cfg["stt"]["engine"] = "none"
        cfg["tts"]["engine"] = "none"
        cfg["zenoh"]["connect"] = [endpoint]
        cfg["service"]["prefix"] = "stt-tts-test"
        cls.cfg = cfg
        cls.prefix = cfg["service"]["prefix"]
        cls.service = VoiceService(cfg)
        cls.session = open_session(cfg)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.service.close()
        cls.router.close()

    def _app(self, name):
        from stt_tts import client
        app = client.App(self.session, self.prefix, name)
        heard = []
        app.on_transcript(lambda msg: heard.append(msg["text"]))
        time.sleep(0.3)
        return app, heard

    def _doublet(self, did):
        from stt_tts import client
        for d in client.list_doublets(self.session, self.prefix):
            if d["id"] == did:
                return d
        return None

    def _talk(self, term, seconds=0.5):
        term.press()
        term.wait_for("listening", timeout=5)
        for _ in range(int(seconds / 0.02)):
            term.send_audio(b"\x00\x00" * 320)
        time.sleep(0.2)
        term.press()

    def test_register_assigns_readable_keys(self):
        first, _ = self._app("Named App")
        second, _ = self._app("named app")
        self.assertEqual(first.info["id"], "named-app/1")
        self.assertEqual(second.info["id"], "named-app/2")
        self.assertEqual(first.info["keys"]["audio_in"], f"{self.prefix}/named-app/1/audio/in")
        self.assertEqual(second.info["keys"]["text_out"], f"{self.prefix}/named-app/2/text/out")
        first.close()
        second.close()

    def test_register_lists_and_closes_when_app_leaves(self):
        app, _ = self._app("leaver")
        did = app.info["id"]
        self.assertIsNotNone(self._doublet(did))
        app.close()
        for _ in range(40):
            if self._doublet(did) is None:
                break
            time.sleep(0.1)
        self.assertIsNone(self._doublet(did))

    def test_lock_and_round_trip(self):
        from stt_tts import client
        app, heard = self._app("echo")
        d = self._doublet(app.info["id"])
        term = client.Terminal(self.session, d, "t1")
        with self.assertRaises(RuntimeError):
            client.Terminal(self.session, d, "t2")

        self._talk(term)
        term.wait_for("thinking", timeout=5)
        for _ in range(20):
            if heard:
                break
            time.sleep(0.1)
        self.assertTrue(heard and heard[0].startswith("(stub transcript"))

        app.say("One. Two.")
        term.wait_for("speaking", timeout=5)
        term.wait_for("playback_done", timeout=10)
        self.assertTrue(term.audio)
        term.detach()
        app.close()

    def test_untokened_publishes_are_ignored(self):
        from stt_tts import client
        app, heard = self._app("guarded")
        d = self._doublet(app.info["id"])
        k = d["keys"]
        self.session.put(k["control"], json.dumps({"op": "press"}))
        self.session.put(k["text_in"], "spoofed")
        time.sleep(0.5)
        self.assertEqual(self._doublet(app.info["id"])["state"], "idle")
        term = client.Terminal(self.session, d, "t1")
        self.session.put(k["control"], json.dumps({"op": "press"}), attachment="wrong")
        time.sleep(0.3)
        self.assertEqual(self._doublet(app.info["id"])["state"], "idle")
        term.detach()
        app.close()

    def test_press_while_speaking_interrupts(self):
        from stt_tts import client
        app, _ = self._app("long")
        d = self._doublet(app.info["id"])
        term = client.Terminal(self.session, d, "t1")
        self._talk(term)
        term.wait_for("thinking", timeout=5)
        # stub TTS: 2.5 words/s, so this is several seconds of playback
        app.say("First sentence here. " + "Then many more words follow on and on. " * 6)
        term.wait_for("speaking", timeout=5)
        time.sleep(1.0)
        term.press()
        ev = term.wait_for("interrupted", timeout=5)
        self.assertIn("First sentence", ev.get("spoken_text", ""))
        term.wait_for("listening", timeout=5)
        term.detach()
        app.close()


if __name__ == "__main__":
    unittest.main()
