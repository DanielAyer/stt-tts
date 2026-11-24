"""The call/response core (stt_tts.core). Run with: python3 -m unittest discover -s tests

Uses the "none" engines, so no models are needed.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt_tts import config  # noqa: E402
from stt_tts.core import Core  # noqa: E402


class CoreTest(unittest.TestCase):
    def setUp(self):
        cfg = config.defaults()
        cfg["stt"]["engine"] = "none"
        cfg["tts"]["engine"] = "none"
        self.core = Core(cfg)

    def tearDown(self):
        self.core.close()

    def test_stream_round_trip_and_reuse(self):
        s = self.core.open_stream()
        s.feed(b"\x00\x00" * 16000)
        self.assertIn("1.0 s", s.finalize())
        s.feed(b"\x00\x00" * 8000)
        self.assertIn("0.5 s", s.finalize())
        self.assertEqual(s.partial(), "")
        s.close()

    def test_streams_are_independent(self):
        a, b = self.core.open_stream(), self.core.open_stream()
        a.feed(b"\x00\x00" * 16000)
        b.feed(b"\x00\x00" * 4000)
        self.assertIn("1.0 s", a.finalize())
        self.assertIn("0.2 s", b.finalize())

    def test_transcribe_clip(self):
        self.assertIn("stub transcript", self.core.transcribe(b"\x00\x00" * 1600))

    def test_synthesize_per_sentence(self):
        chunks = list(self.core.synthesize_stream("One. Two! Three?"))
        self.assertEqual(len(chunks), 3)
        self.assertEqual(self.core.synthesize("One. Two! Three?"), b"".join(chunks))
        self.assertEqual(self.core.input_rate, 16000)
        self.assertGreater(self.core.output_rate, 0)

    def test_unknown_voice_is_refused(self):
        with self.assertRaises(ValueError):
            list(self.core.synthesize_stream("Hi.", voice="someone-else"))


if __name__ == "__main__":
    unittest.main()
