"""Engine plugins. Run with: python3 -m unittest discover -s tests

Tests that need an engine's packages or models skip themselves when those
aren't present. To run the real Kokoro test, point STT_TTS_TEST_KOKORO_DIR at
a folder holding kokoro-v1.0.int8.onnx and voices-v1.0.bin.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt_tts import config, engines  # noqa: E402
from stt_tts.engines import base  # noqa: E402

HAS_MOONSHINE = importlib.util.find_spec("moonshine_voice") is not None
HAS_KOKORO = importlib.util.find_spec("kokoro_onnx") is not None
KOKORO_DIR = os.environ.get("STT_TTS_TEST_KOKORO_DIR", "")


class RegistryTest(unittest.TestCase):
    def test_defaults_are_moonshine(self):
        cfg = config.defaults()
        chosen = engines.selected(cfg)
        self.assertEqual((chosen["stt"].name, chosen["tts"].name), ("moonshine", "moonshine"))

    def test_packages_follow_the_choice(self):
        cfg = config.defaults()
        self.assertEqual(engines.packages_for(engines.selected(cfg).values()),
                         ["moonshine-voice>=0.1.6"])  # one package for both directions
        cfg["tts"]["engine"] = "kokoro"
        self.assertEqual(engines.packages_for(engines.selected(cfg).values()),
                         ["moonshine-voice>=0.1.6", "kokoro-onnx>=0.4"])
        cfg["stt"]["engine"] = cfg["tts"]["engine"] = "none"
        self.assertEqual(engines.packages_for(engines.selected(cfg).values()), [])

    def test_old_stub_name_still_works(self):
        cfg = config.defaults()
        cfg["stt"]["engine"] = "stub"
        self.assertEqual(engines.spec("stt", "stub").name, "none")
        self.assertIsInstance(engines.build_stt(cfg), base.STTEngine)

    def test_unknown_engine(self):
        with self.assertRaises(ValueError):
            engines.spec("tts", "festival")

    def test_options_overlay_and_ensure(self):
        cfg = config.defaults()
        cfg["tts"]["engine"] = "kokoro"
        cfg["tts"]["kokoro"] = {"voice": "af_heart:0.6,am_adam:0.4"}
        opts = engines.options(cfg, engines.spec("tts", "kokoro"))
        self.assertEqual(opts, {"voice": "af_heart:0.6,am_adam:0.4", "speed": 1.0})
        engines.ensure_options(cfg)
        self.assertEqual(cfg["tts"]["kokoro"]["speed"], 1.0)
        self.assertEqual(cfg["stt"]["moonshine"]["model"], "small-streaming")

    @unittest.skipIf(HAS_MOONSHINE, "moonshine-voice is installed here")
    def test_missing_packages_give_a_clear_error(self):
        with self.assertRaises(engines.EngineUnavailable) as caught:
            engines.build_stt(config.defaults())
        self.assertIn("stt-tts setup engines", str(caught.exception))


class PCMTest(unittest.TestCase):
    def test_roundtrip(self):
        pcm = base.floats_to_pcm16([0.0, 0.5, -0.5, 1.5, -1.5])
        back = base.pcm16_to_floats(pcm)
        self.assertAlmostEqual(back[1], 0.5, places=3)
        self.assertAlmostEqual(back[3], 1.0, places=3)   # clipped
        self.assertAlmostEqual(back[4], -1.0, places=3)


class KokoroBlendTest(unittest.TestCase):
    def test_parse_voice(self):
        from stt_tts.engines.kokoro import parse_voice
        self.assertEqual(parse_voice("af_heart"), [("af_heart", 1.0)])
        self.assertEqual(parse_voice("af_heart:3, am_adam:1"), [("af_heart", 0.75), ("am_adam", 0.25)])
        with self.assertRaises(ValueError):
            parse_voice(":")

    @unittest.skipUnless(HAS_KOKORO and KOKORO_DIR, "kokoro-onnx or its models not available")
    def test_real_blended_synthesis(self):
        cfg = config.defaults()
        cfg["paths"]["model_dir"] = str(Path(KOKORO_DIR).parent)
        cfg["tts"]["engine"] = "kokoro"
        cfg["tts"]["kokoro"] = {"voice": "af_heart:0.6, am_adam:0.4"}
        with mock.patch("stt_tts.engines.kokoro._root", return_value=Path(KOKORO_DIR)):
            tts = engines.build_tts(cfg)
            pcm = tts.synthesize("Testing one two three.")
        self.assertEqual(tts.sample_rate, 24000)
        self.assertGreater(len(pcm) / 2 / tts.sample_rate, 0.5)
        self.assertGreater(max(abs(x) for x in base.pcm16_to_floats(pcm[:48000])), 0.01)


class MoonshineAdapterTest(unittest.TestCase):
    """Moonshine's models can't be downloaded in every environment, so the adapter is
    checked against a stand-in that records the calls it receives."""

    def setUp(self):
        self.fake = mock.MagicMock()
        self.fake.ModelArch.SMALL_STREAMING = "SMALL"
        self.fake.get_model_for_language.return_value = ("/models/small", "SMALL")
        line = mock.Mock(text=" hello world ")
        stream = self.fake.Transcriber.return_value.create_stream.return_value
        stream.stop.return_value = mock.Mock(lines=[line])
        tts = self.fake.TextToSpeech.return_value
        for setter in ("language", "voice", "models_from", "on_progress"):
            getattr(tts, setter).return_value = tts
        tts.synthesize.return_value = ([0.0, 0.25, -0.25], 24000)
        self.patch = mock.patch.dict(sys.modules, {"moonshine_voice": self.fake})
        self.patch.start()
        sys.modules.pop("stt_tts.engines.moonshine", None)

    def tearDown(self):
        self.patch.stop()
        sys.modules.pop("stt_tts.engines.moonshine", None)

    def test_stt_streams_frames_and_finalizes(self):
        cfg = config.defaults()
        stt = engines.build_stt(cfg)
        self.fake.get_model_for_language.assert_called_once()
        args, kwargs = self.fake.get_model_for_language.call_args
        self.assertEqual(args, ("en", "SMALL"))
        self.assertTrue(str(kwargs["cache_root"]).endswith("models/moonshine"))
        for _ in range(3):
            stt.feed(b"\x00\x40" * 320)
        stream = self.fake.Transcriber.return_value.create_stream.return_value
        self.assertEqual(stream.add_audio.call_count, 3)
        self.assertEqual(stream.add_audio.call_args[0][1], 16000)
        self.assertEqual(stt.finalize(), "hello world")
        stream.close.assert_called_once()

    def test_tts_uses_voice_and_local_models(self):
        cfg = config.defaults()
        tts = engines.build_tts(cfg)
        fake_tts = self.fake.TextToSpeech.return_value
        fake_tts.voice.assert_called_with("kokoro_af_heart")
        self.assertEqual(fake_tts.models_from.call_args.kwargs, {"download": False})
        self.assertEqual(tts.sample_rate, 24000)
        self.assertEqual(len(tts.synthesize("Hi.")), 6)


if __name__ == "__main__":
    unittest.main()
