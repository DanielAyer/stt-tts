"""Adapter registry. Add a class here to make it selectable in config."""

from . import llm_http, stub, zenoh_adapter as z

AUDIO_INPUTS = {c.name: c for c in (stub.StubMic, stub.FileInput, z.ZenohAudioInput)}
AUDIO_OUTPUTS = {c.name: c for c in (stub.StubSpeaker, stub.FileOutput, z.ZenohAudioOutput)}
# "llm" is added automatically when the LLM profile is enabled (llm.enabled = true)
TEXT_SINKS = {c.name: c for c in (stub.StdoutSink, stub.FileSink, z.ZenohTextSink)}
LLM_SINK = llm_http.LLMBridge
TEXT_SOURCES = {c.name: c for c in (stub.StdinSource, z.ZenohTextSource)}
CONTROLS = {c.name: c for c in (stub.StubButton, stub.KeyboardButton, z.ZenohButton)}


def build(registry: dict, name: str, cfg: dict):
    try:
        return registry[name](cfg)
    except KeyError:
        known = ", ".join(sorted(registry))
        raise ValueError(f"Unknown adapter '{name}'. Known: {known}") from None
