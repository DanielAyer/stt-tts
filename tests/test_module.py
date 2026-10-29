"""Run with: python3 -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from installer import core  # noqa: E402
from stt_tts import config, security  # noqa: E402
from stt_tts.adapters.llm_http import check_url  # noqa: E402
from stt_tts.discovery import parse_admin_key  # noqa: E402
from stt_tts.pipeline import VoiceModule  # noqa: E402
from stt_tts.state import Action, Controller, State  # noqa: E402


class StateMachineTest(unittest.TestCase):
    def test_full_cycle(self):
        c = Controller()
        self.assertEqual(c.press(), [Action.OPEN_MIC])
        self.assertTrue(c.mic_open)
        self.assertEqual(c.press(), [Action.CLOSE_MIC_AND_SEND])
        self.assertEqual(c.audio_ready(), [Action.START_PLAYBACK])
        self.assertFalse(c.mic_open)  # half-duplex
        self.assertEqual(c.playback_done(), [])
        self.assertIs(c.state, State.IDLE)

    def test_cancel_and_interrupt_reopen_mic(self):
        c = Controller()
        c.press(); c.press()
        self.assertEqual(c.press(), [Action.CANCEL_REPLY, Action.OPEN_MIC])
        c.press(); c.audio_ready()
        self.assertEqual(c.press(), [Action.INTERRUPT_PLAYBACK, Action.OPEN_MIC])

    def test_late_reply_after_cancel_is_dropped(self):
        c = Controller()
        c.press(); c.press(); c.press()  # cancelled, now listening
        self.assertEqual(c.audio_ready(), [])
        self.assertIs(c.state, State.LISTENING)

    def test_after_reply_listening(self):
        c = Controller(after_reply="listening")
        c.press(); c.press(); c.audio_ready()
        self.assertEqual(c.playback_done(), [Action.OPEN_MIC])


class PipelineTest(unittest.TestCase):
    def test_interrupt_reports_spoken_text(self):
        m = VoiceModule(config.defaults())
        m.sinks = []
        m.press(); m.press()
        m.speak("First sentence. Second sentence.", autoplay=False)
        m.play_next()
        m.press()
        interrupted = [e for e in m.events if e["event"] == "interrupted"][0]
        self.assertEqual(interrupted["spoken_text"], "First sentence.")


class ConfigTest(unittest.TestCase):
    def test_roundtrip_and_set(self):
        cfg = config.defaults()
        config.set_value(cfg, "zenoh.keys.out_text", "lab/robot/text")
        config.set_value(cfg, "zenoh.enabled", "true")
        config.set_value(cfg, "input.adapters", "stub_mic, zenoh")
        with tempfile.TemporaryDirectory() as d:
            path = config.save(cfg, Path(d) / "c.toml")
            loaded = config.load(path)
        self.assertEqual(loaded["zenoh"]["keys"]["out_text"], "lab/robot/text")
        self.assertIs(loaded["zenoh"]["enabled"], True)
        self.assertEqual(loaded["input"]["adapters"], ["stub_mic", "zenoh"])


class DiscoveryTest(unittest.TestCase):
    def test_parse_admin_key(self):
        ch = parse_admin_key("@/abc123/router/subscriber/voice/in/audio")
        self.assertEqual((ch.key, ch.kind), ("voice/in/audio", "subscriber"))
        self.assertIsNone(parse_admin_key("@/abc123/router/linkstate/routers"))


class SecurityTest(unittest.TestCase):
    def llm_cfg(self):
        cfg = config.defaults()
        cfg["llm"].update(enabled=True, api_key_secret="llm-api-key")
        return cfg

    def test_open_by_default(self):
        cfg = config.defaults()
        security.enforce(cfg)
        self.assertTrue(security.InputGate(cfg).admit(None))

    def test_credential_requires_input_security(self):
        cfg = self.llm_cfg()
        with self.assertRaises(security.SecurityError):
            security.enforce(cfg)
        cfg["security"]["input"]["enabled"] = True
        security.enforce(cfg)

    def test_keyless_llm_still_requires_input_security(self):
        cfg = config.defaults()
        cfg["llm"]["enabled"] = True
        with self.assertRaises(security.SecurityError):
            security.enforce(cfg)

    def test_zenoh_input_needs_tls_when_credentialed(self):
        cfg = self.llm_cfg()
        cfg["security"]["input"]["enabled"] = True
        cfg["input"]["adapters"] = ["zenoh"]
        with self.assertRaises(security.SecurityError):
            security.enforce(cfg)
        cfg["zenoh"]["tls"]["enabled"] = True
        security.enforce(cfg)

    def test_gate_refuses_unlisted(self):
        cfg = config.defaults()
        cfg["security"]["input"].update(enabled=True, local_trusted=False, allowed=["cert:mic1"])
        gate = security.InputGate(cfg)
        self.assertTrue(gate.admit(security.Identity("cert", "mic1")))
        self.assertFalse(gate.admit(security.Identity("cert", "mic2")))
        self.assertFalse(gate.admit(None))
        self.assertFalse(gate.admit_adapter("stub_mic", zenoh_tls=False))

    def test_identity_must_be_cryptographic(self):
        with self.assertRaises(security.SecurityError):
            security.Identity.parse("mac:AA:BB:CC")

    def test_plain_http_only_to_localhost(self):
        self.assertTrue(check_url("http://192.168.1.20:8080/v1"))
        self.assertEqual(check_url("http://127.0.0.1:8080/v1"), "")
        self.assertEqual(check_url("https://api.example.com/v1"), "")


class ManifestTest(unittest.TestCase):
    def test_rollback_keeps_preexisting_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            existing = Path(d) / "existing"
            existing.mkdir()
            m = core.Manifest()
            m.mkdir(existing / "new" / "deeper")
            (existing / "new" / "f.txt").write_text("x")
            m.file(existing / "new" / "f.txt")
            m.rollback(lambda _m: None)
            self.assertTrue(existing.exists())
            self.assertFalse((existing / "new").exists())


if __name__ == "__main__":
    unittest.main()
