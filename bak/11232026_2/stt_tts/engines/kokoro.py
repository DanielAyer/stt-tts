"""Kokoro-82M TTS via kokoro-onnx (pip: kokoro-onnx). Supports blended voices.

Models come from the kokoro-onnx GitHub release and are checked against a
pinned SHA-256 before use, into <model_dir>/kokoro.

Options ([tts.kokoro]):
  voice   one voice, e.g. "af_heart", or a blend: "af_heart:0.6, am_adam:0.4"
          (weights are normalized to sum to 1; list voices: stt-tts engines voices)
  speed   1.0
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from .base import TTSEngine, floats_to_pcm16

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {
    "kokoro-v1.0.int8.onnx": "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
MODEL, VOICES = "kokoro-v1.0.int8.onnx", "voices-v1.0.bin"


def _root(cfg: dict) -> Path:
    return Path(cfg["paths"]["model_dir"]).expanduser() / "kokoro"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(cfg: dict, progress) -> None:
    root = _root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    for name, digest in FILES.items():
        target = root / name
        if target.exists() and _sha256(target) == digest:
            progress(f"Kokoro: {name} already present")
            continue
        progress(f"Downloading Kokoro {name}")
        part = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(RELEASE + name, timeout=60) as resp, part.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done, step = 0, 10
            while block := resp.read(1 << 20):
                out.write(block)
                done += len(block)
                if total and done * 100 // total >= step:
                    progress(f"  {done * 100 // total:3d}%  {name}")
                    step += 10
        if _sha256(part) != digest:
            part.unlink()
            raise RuntimeError(f"Kokoro {name}: checksum mismatch, download discarded")
        part.replace(target)


def parse_voice(spec: str) -> list[tuple[str, float]]:
    """'af_heart' -> [('af_heart', 1.0)]; 'af_heart:0.6, am_adam:0.4' -> weighted parts."""
    parts = []
    for item in spec.split(","):
        name, _, weight = item.strip().partition(":")
        if not name:
            continue
        parts.append((name.strip(), float(weight) if weight else 1.0))
    total = sum(w for _, w in parts)
    if not parts or total <= 0:
        raise ValueError(f"Bad Kokoro voice '{spec}'. Example: af_heart or af_heart:0.6,am_adam:0.4")
    return [(n, w / total) for n, w in parts]


class KokoroTTS(TTSEngine):
    def __init__(self, cfg: dict, opts: dict | None = None) -> None:
        super().__init__(cfg, opts)
        import logging

        from kokoro_onnx import Kokoro

        logging.getLogger("phonemizer").setLevel(logging.ERROR)  # harmless word-count warnings
        root = _root(cfg)
        missing = [n for n in FILES if not (root / n).exists()]
        if missing:
            raise RuntimeError(f"Kokoro models missing in {root}: {missing}. "
                               "Run: stt-tts engines fetch")
        self.kokoro = Kokoro(str(root / MODEL), str(root / VOICES))
        known = set(self.kokoro.get_voices())
        parts = parse_voice(self.opts["voice"])
        unknown = [n for n, _ in parts if n not in known]
        if unknown:
            raise ValueError(f"Unknown Kokoro voice(s) {unknown}. See: stt-tts engines voices")
        self.style = sum(w * self.kokoro.get_voice_style(n) for n, w in parts)

    def synthesize(self, sentence: str) -> bytes:
        samples, rate = self.kokoro.create(sentence, voice=self.style,
                                           speed=float(self.opts["speed"]), lang="en-us")
        self.sample_rate = rate
        return floats_to_pcm16(samples)


def voices(cfg: dict) -> list[str]:
    from kokoro_onnx import Kokoro
    root = _root(cfg)
    return sorted(Kokoro(str(root / MODEL), str(root / VOICES)).get_voices())
