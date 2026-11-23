"""Moonshine Voice engines (pip: moonshine-voice). STT and TTS from one package.

Models are downloaded into <model_dir>/moonshine at install time (fetch_*),
so the module runs offline afterwards.

STT options ([stt.moonshine]):
  model     tiny-streaming | base-streaming | small-streaming | medium-streaming
  language  en
TTS options ([tts.moonshine]):
  voice     e.g. kokoro_af_heart, piper_en_US-lessac-medium, zipvoice_american_female
            (list them: stt-tts engines voices)
  language  en-us
  speed     1.0
"""

from __future__ import annotations

from pathlib import Path

import moonshine_voice as mv

from .base import STTEngine, TTSEngine, floats_to_pcm16, pcm16_to_floats

from .moonshine_meta import ARCH_NAMES

ARCHES = {name: name.upper().replace("-", "_") for name in ARCH_NAMES}


def _root(cfg: dict) -> Path:
    return Path(cfg["paths"]["model_dir"]).expanduser() / "moonshine"


def _arch(name: str):
    try:
        return getattr(mv.ModelArch, ARCHES[name])
    except KeyError:
        raise ValueError(f"Unknown Moonshine model '{name}'. Choose: {', '.join(ARCHES)}") from None


def _stt_opts(cfg: dict) -> dict:
    from . import options, spec
    return options(cfg, spec("stt", "moonshine"))


def _tts_opts(cfg: dict) -> dict:
    from . import options, spec
    return options(cfg, spec("tts", "moonshine"))


def _progress_printer(progress):
    last = {"pct": -10}

    def report(fraction: float, name: str) -> None:
        pct = int(fraction * 100)
        if pct >= last["pct"] + 10 or pct == 100:
            last["pct"] = pct
            progress(f"  {pct:3d}%  {name}")
    return report


def fetch_stt(cfg: dict, progress) -> None:
    o = _stt_opts(cfg)
    progress(f"Downloading Moonshine STT model ({o['model']})")
    mv.get_model_for_language(o["language"], _arch(o["model"]), cache_root=_root(cfg),
                              on_progress=_progress_printer(progress))


def fetch_tts(cfg: dict, progress) -> None:
    o = _tts_opts(cfg)
    progress(f"Downloading Moonshine TTS voice ({o['voice']})")
    tts = (mv.TextToSpeech().language(o["language"]).voice(o["voice"])
           .models_from(_root(cfg) / "tts", download=True)
           .on_progress(_progress_printer(progress)))
    tts.load()
    tts.close()


class MoonshineSTT(STTEngine):
    def __init__(self, cfg: dict, opts: dict | None = None) -> None:
        super().__init__(cfg, opts)
        path, arch = mv.get_model_for_language(self.opts["language"], _arch(self.opts["model"]),
                                               cache_root=_root(cfg),
                                               on_progress=lambda *_: None)
        self.transcriber = mv.Transcriber(path, arch)
        self.rate = cfg["input"]["sample_rate"]
        self._stream = None
        self._partial = ""

    def _start(self) -> None:
        self._stream = self.transcriber.create_stream()
        self._stream.start()
        self._partial = ""

    def feed(self, pcm: bytes) -> str | None:
        if self._stream is None:
            self._start()
        self._stream.add_audio(pcm16_to_floats(pcm), self.rate)  # updates on its own cadence
        return None

    def update(self) -> str:
        """Current partial transcript (in-progress; it will change)."""
        if self._stream is None:
            return ""
        text = _text(self._stream.update_transcription())
        self._partial = text
        return text

    def finalize(self) -> str:
        if self._stream is None:
            return ""
        transcript = self._stream.stop()  # transcribes the audio still buffered
        text = _text(transcript) if transcript is not None else self._partial
        self._stream.close()
        self._stream = None
        return text

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
        self.transcriber.close()


def _text(transcript) -> str:
    return " ".join(line.text.strip() for line in transcript.lines if line.text.strip())


class MoonshineTTS(TTSEngine):
    def __init__(self, cfg: dict, opts: dict | None = None) -> None:
        super().__init__(cfg, opts)
        self.tts = (mv.TextToSpeech().language(self.opts["language"]).voice(self.opts["voice"])
                    .models_from(_root(cfg) / "tts", download=False))
        self.tts.load()
        _, self.sample_rate = self.tts.synthesize("Ready.", speed=self.opts["speed"])  # warm up, learn rate

    def synthesize(self, sentence: str) -> bytes:
        samples, rate = self.tts.synthesize(sentence, speed=self.opts["speed"])
        self.sample_rate = rate
        return floats_to_pcm16(samples)

    def close(self) -> None:
        self.tts.close()


def voices(language: str = "en-us") -> list[str]:
    found = mv.list_tts_voices(language)
    return sorted(set(found.get("present", [])) | set(found.get("downloadable", [])))
