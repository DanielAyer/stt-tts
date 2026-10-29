# stt-tts

A local, bidirectional speech module for language models. Speech from any audio device becomes plain text for an LLM, and the LLM's text reply becomes speech again. It runs on the CPU, so the GPU stays free for the model.

![Speech to text on top, text to speech beneath it, mirrored](docs/flow.svg)

> **Status: scaffold.** The workflow, state machine, installer, config and Zenoh wiring are real. The speech engines and audio devices are **stubs**. Search the code for `STUB:` to find every endpoint to replace. See [Stubbed endpoints](#stubbed-endpoints) and [What our rig uses](#what-our-rig-uses).

## How it works

- **User side is audio, in any modality.** A headset, a room speaker, a Bluetooth device, a network stream or a file each plug in as an adapter. Input is normalized to 16 kHz mono PCM.
- **Model side is text.** Transcripts go to any sink (an LLM adapter, stdout, a file, Zenoh). Text to speak comes from any source.
- **One button, no wake word.** A single context-sensitive control drives the module:

| State | A press does |
| --- | --- |
| Idle | Start listening |
| Listening | Stop and send what you said |
| Thinking | Cancel the reply and listen again |
| Speaking | Interrupt playback and listen again |

- **Half-duplex.** The mic is closed while the module speaks, so it never hears itself.
- **Interrupts are honest.** An interrupt emits the text actually spoken, so the LLM's context matches what you heard.
- **Two input modes, one result.** Burst (record a clip, send it whole) and stream (live transcription) both end as an utterance in, text out.

## Requirements

| | Minimum |
| --- | --- |
| OS | Linux, x86-64 |
| CPU | 4 cores with AVX2 (most Intel Core 4th gen and newer, AMD Zen and newer) |
| RAM | 8 GB |
| Disk | 2 GB free at the install location |
| Python | 3.11 or newer (standard library only for the installer) |
| Optional | `python3-venv` (recommended), `python3-tk` (GUI installer), `eclipse-zenoh` (Zenoh) |

These minimums are estimates from published benchmarks of the planned engines, not measurements of this stack. The installer checks them and lets you continue below them.

## Install

```bash
git clone https://github.com/DanielAyer/stt-tts.git
cd stt-tts
./install.sh
```

`install.sh` finds a Python 3.11+, then opens the **GUI installer** if it can show a window, or the **command-line installer** if it can't.

### Installing over SSH

To get the GUI on a remote server, forward X11 and make sure Tk is on the server:

```bash
ssh -X user@server          # -Y if your X server rejects untrusted clients
sudo apt install python3-tk # on the server, once
./install.sh
```

The installer decides like this:

1. No `DISPLAY` set (plain `ssh`) → command line.
2. `DISPLAY` set but Tk not installed → command line.
3. Tk installed but a test window fails to open (broken forwarding) → command line.
4. Otherwise → GUI.

The reason is printed at the top, e.g. `Using the command-line installer (no display found (connect with ssh -X to forward X11)).`

### Running the installer again

If stt-tts is already installed, the installer starts by showing the current install and asking:

- **Modify**: walk through the options again, starting from your current settings. Data, transcripts and stored keys are kept; program files are refreshed.
- **Replace** ("scorched earth"): delete the program, data store, transcripts, config, stored keys and launcher, then install fresh. The command line asks you to type `replace` to confirm. Deletion only touches paths containing `stt-tts`, and it can't be undone.
- **Cancel**.

Over SSH without a terminal prompt available, pass `--modify` or `--replace`; otherwise the installer stops without changing anything.

### If the machine is below the minimum

The installer shows a table of every check. If any required check fails, it says which and asks:

- **Continue anyway**: install, knowing it may run slowly or not at all.
- **Stop and clean up**: exit, removing anything the installer created.

`--yes` answers "continue" without asking (useful for scripts). Without a terminal to ask and without `--yes`, the command-line installer stops.

Cleanup is exact: the installer records every file and directory it creates and removes only those, restoring any launcher or config it replaced. The same rollback runs if the install fails partway or you press Ctrl-C.

### Options

| Flag | Effect |
| --- | --- |
| `--cli` | Force the command-line installer |
| `--gui` | Require the GUI (exits if no window can open) |
| `--yes` | Continue past failed requirement checks |
| `--modify` | If already installed: keep data and settings, refresh program files |
| `--replace` | If already installed: delete everything and install fresh (no undo) |
| `--llm` | Command line: also ask the LLM integration questions |
| `--zenoh` | Also `pip install eclipse-zenoh` into the module's venv (needs network) |
| `--app-dir PATH` | Install location (command line) |
| `--data-dir PATH` | Data store: models, transcripts, logs (command line) |
| `--bin-dir PATH` | Where the `stt-tts` launcher goes (default `~/.local/bin`) |
| `--config-file PATH` | Config file location |
| `--uninstall` | Remove what the installer created (add `--app-dir` if you moved it) |

### What gets created

| What | Default |
| --- | --- |
| Module and its venv | `~/.local/opt/stt-tts` |
| Data store (models, transcripts, logs) | `~/.local/share/stt-tts` |
| Audio spool | `/mnt/voice-spool` if it exists, else `$XDG_RUNTIME_DIR/stt-tts` |
| Config | `~/.config/stt-tts/config.toml` |
| Launcher | `~/.local/bin/stt-tts` |

API keys are stored separately: in the system keyring if one is available, otherwise in `~/.config/stt-tts/secrets.json` with owner-only permissions. They never go in `config.toml`.

Uninstalling removes everything the installer created, **including the data store and stored keys**. Copy out transcripts you want to keep first.

### RAM spool (recommended)

Burst clips only need to exist until they're transcribed, so keep them in RAM. The installer prints these steps if your spool isn't on tmpfs:

```bash
sudo mkdir -p /mnt/voice-spool
echo 'tmpfs /mnt/voice-spool tmpfs size=256M,mode=1777,noatime,nodev,nosuid,noexec 0 0' | sudo tee -a /etc/fstab
sudo mount /mnt/voice-spool
stt-tts config set paths.spool_dir /mnt/voice-spool
```

256 MB holds over two hours of 16 kHz audio. Each clip is deleted once transcribed, and the spool empties on reboot.

## GUI vs command line

Both installers run the same checks and the same install steps. They differ in how much you choose up front:

| Capability | GUI | Command line |
| --- | --- | --- |
| Existing install: modify, replace or cancel | Yes | Yes (`--modify` / `--replace` without a terminal) |
| Requirement table, continue or stop and clean up | Yes | Yes |
| Choose install location, data store, spool, config file | Browse dialogs | Flags, else defaults |
| Choose audio inputs/outputs, text sinks/sources, button source | Checkboxes | Defaults; change later |
| Behavior after a reply (go idle or reopen the mic) | Radio buttons | Default (idle); change later |
| Zenoh router and key names | Form fields | Defaults; change later |
| Discover live Zenoh channels during install | Only if the installer's Python has `eclipse-zenoh` | No |
| Zenoh mutual TLS certificates | Form fields | Change later |
| LLM integration | Its own page | With `--llm`, or later with `stt-tts setup llm` |
| Input allow-list and output sealing | Security page | Later with `stt-tts security` |
| Progress and rollback on failure | Log window | Printed |

The command line deliberately takes every default, so an SSH install never stalls on questions. Change anything afterwards:

```bash
stt-tts config show                              # every setting
stt-tts config set input.adapters stub_mic,zenoh # lists are comma-separated
stt-tts config set control.after_reply listening
stt-tts channels --assign                        # pick Zenoh keys from a live router
stt-tts setup llm                                # add or change LLM integration
stt-tts security status                          # check the security overlays
```

`config set` refuses any change that would leave an LLM or stored credential reachable by unauthenticated input (see [Security](#security)).

## Configuration

Everything selectable lives in `config.toml`; any key missing from the file falls back to its default. `stt-tts config path` prints where the file is.

| Key | Default | Options now |
| --- | --- | --- |
| `input.adapters` | `["stub_mic"]` | `stub_mic`, `file`, `zenoh` |
| `output.adapters` | `["stub_speaker"]` | `stub_speaker`, `file`, `zenoh` |
| `text.sinks` | `["stdout"]` | `stdout`, `file`, `zenoh` |
| `text.sources` | `["stdin"]` | `stdin`, `zenoh` |
| `control.adapter` | `stub_button` | `stub_button`, `keyboard` (Enter key), `zenoh` |
| `control.after_reply` | `idle` | `idle`, `listening` |
| `stt.engine` | `stub` | `stub` (Moonshine planned) |
| `tts.engine` | `stub` | `stub` (Kokoro planned) |
| `tts.voice` | `af_heart` | any Kokoro voice or blend, once implemented |
| `output.buffer_ms` | `300` | playback buffer; smaller makes interrupts snappier |
| `zenoh.enabled` | `false` | `true`, `false` |
| `zenoh.connect` | `["tcp/localhost:7447"]` | router endpoints |
| `zenoh.tls.enabled` | `false` | mutual TLS; also set `root_ca`, `cert`, `key` |
| `llm.enabled` | `false` | set by `stt-tts setup llm` |
| `security.input.enabled` | `false` | input allow-list; forced on with an LLM or stored credential |
| `security.input.allowed` | `[]` | `cert:<name>`, `local:<adapter>` |
| `security.output.seal` | `false` | sign and encrypt Zenoh/file outputs (stub) |

## LLM integration

Optional. The core stays agnostic at both ends; this profile adds a direct link to a language model:

- **One adapter, many backends.** It speaks the OpenAI-compatible chat API, which llama.cpp's `llama-server`, Ollama, vLLM and most cloud APIs accept. Switching models is a config change.
- **Replies are spoken as they stream.** TTS starts on the first finished sentence (once the network call is implemented).
- **Interrupts are recorded.** The conversation history keeps only the part of a reply you actually heard, marked as interrupted.
- **HTTPS is configurable, not assumed by the core.** Within this profile it's required for any host other than the machine itself, with certificate checking, an optional custom CA for your own server, and an optional client certificate for mutual TLS.
- **It turns on input security.** Any LLM connection makes the input allow-list mandatory, with or without an API key (next section).

Set it up during install (GUI page, or `--llm` on the command line), or any time later:

```bash
stt-tts setup llm            # walk through backend, URL, model, API key, TLS, allowed devices
stt-tts setup llm --remove   # turn it off and delete the stored key
```

## Security

Open by default at both ends: any device may send audio, and outputs go out as-is. That keeps the module a plain clip-in between any two systems. Security comes as opt-in overlays, with one rule that isn't optional.

**Input allow-list.** When on, only listed identities may send audio or press the button:

- `cert:<name>`: a Zenoh device presenting a certificate with that common name (needs mutual TLS)
- `local:<adapter>`: a device attached to the module's machine; covered automatically if "trust local devices" is on

Device names, MAC addresses and IDs carried in the data are deliberately not accepted, because they're easy to fake.

**The rule.** If an LLM is connected, or any output uses a stored credential, the input allow-list must be on, and Zenoh inputs must use mutual TLS. Otherwise anyone who can reach the module could spend your key through it. The module refuses to start, `config set` refuses to save, and the installer refuses to finish in that state, each with a message naming the fix.

**Zenoh enforcement.** Per-device checks happen at the router. Generate a default-deny access-control block from your allow-list and add it to your `zenohd` config:

```bash
stt-tts security allow cert:kitchen-mic
stt-tts security acl          # prints the access_control block
```

Allowed device certificates may publish audio and button presses; the module's own certificate (common name `stt-tts`) may read them and publish outputs. Check the field names against your Zenoh version's documentation.

**Output sealing (stub).** For outputs that pass through or rest somewhere you don't fully trust (Zenoh routers, files), the module signs each message with its own private key, then encrypts it to each recipient's public key. Only recipients can read it, and they can verify it came from the module. Settings are saved now; the cryptography is not implemented yet.

**Stored keys.** Kept in the system keyring if available, otherwise in an owner-only file next to the config (`stt-tts` refuses to read it if others can). Never in `config.toml`, never logged.

**Certificates.** `stt-tts certs` runs a small private certificate authority for all of the above; see the next section.

**Not covered.** If the LLM is given tools (running commands, sending messages), anyone speaking near an allowed mic is effectively instructing it. Keep tools off, or require confirmation for each action.

## Zenoh

With `zenoh.enabled = true` and `zenoh` listed in the adapters you want, the module subscribes and publishes on these key expressions. Every name is configurable.

| Config key | Default | Direction | Payload |
| --- | --- | --- | --- |
| `zenoh.keys.in_audio` | `voice/in/audio` | subscribe | 16 kHz mono PCM frames |
| `zenoh.keys.in_control` | `voice/in/control` | subscribe | any message = one button press |
| `zenoh.keys.out_text` | `voice/out/text` | publish | JSON `{"text": ..., "mode": "burst" or "stream"}` |
| `zenoh.keys.tts_text` | `voice/tts/text` | subscribe | UTF-8 text to speak |
| `zenoh.keys.tts_audio` | `voice/tts/audio` | publish | PCM at `tts.sample_rate` |
| `zenoh.keys.events` | `voice/events` | publish | JSON state events, e.g. `interrupted` |

Install support with `./install.sh --zenoh`, or later with `~/.local/opt/stt-tts/venv/bin/pip install eclipse-zenoh`.

### Discovering channels

Zenoh has no central channel list, but a `zenohd` router answers queries on its admin space. `stt-tts channels` asks for:

```
@/<router-id>/router/subscriber/**
@/<router-id>/router/publisher/**
@/<router-id>/router/queryable/**
```

and lists the key expressions found. `stt-tts channels --assign` then walks through each `zenoh.keys.*` setting and lets you pick a number from the list, type a new key, or keep the current one.

Limits to know:

- **A router is required.** A pure peer-to-peer mesh has no admin space to ask.
- **Only declared entities appear.** A topic someone publishes with a plain `put()`, with no declared publisher and no live subscriber, won't be listed.
- **The admin space must be readable.** It is by default.
- **Layout varies by Zenoh version.** The parser expects the 1.x layout.

## Certificates

`stt-tts certs` is a small private certificate authority (CA), built on the `openssl` command. One CA signs every certificate in your setup, so each part trusts the others by checking against that one CA.

| Command | What it does |
| --- | --- |
| `stt-tts certs init` | Create the CA (once). Its key is owner-only. |
| `stt-tts certs issue NAME --server --host H [--host H2]` | Certificate for a server (LLM host, Zenoh router). List every hostname and IP clients will use. |
| `stt-tts certs issue NAME` | Client certificate for a device. `NAME` becomes the identity you allow with `stt-tts security allow cert:NAME`. |
| `stt-tts certs issue stt-tts --use-for zenoh` | The module's own client certificate, wired into its Zenoh mutual TLS settings (`--use-for llm` for the LLM link). |
| `stt-tts certs trust llm` | Make the module trust the CA for the LLM link (`trust zenoh` for the router). |
| `stt-tts certs list` | Issued certificates, their type, hosts and expiry. |

Files live in `<data store>/pki`: `ca.crt` and `ca.key`, plus `issued/<name>/` holding `key.pem`, `cert.pem` and a copy of `ca.crt`. Keys are ECDSA P-256. Issued certificates last 825 days (`--days` to change); the CA lasts 10 years.

- **Protect `ca.key`.** Anyone with it can mint certificates your whole setup trusts. Back it up somewhere safe: a replace (scorched earth) reinstall deletes the data store, and the CA with it.
- **Copy each `key.pem` only to the machine it belongs to.**
- **Hostnames must match.** The module connects only if the URL's host is one of the certificate's `--host` entries.

### Example: TLS for llama.cpp on another machine

On the stt-tts machine:

```bash
stt-tts certs init
stt-tts certs issue ravensnest --server --host ravensnest --host ravensnest.local --host 192.168.1.50
scp -r ~/.local/share/stt-tts/pki/issued/ravensnest you@ravensnest:~/llama-tls
```

On the LLM machine, build llama.cpp with `libssl-dev` installed (check with `llama-server --help | grep ssl`), then:

```bash
chmod 600 ~/llama-tls/key.pem
llama-server -m model.gguf --host 0.0.0.0 --port 8080 \
  --ssl-key-file ~/llama-tls/key.pem --ssl-cert-file ~/llama-tls/cert.pem
```

Back on the stt-tts machine:

```bash
curl --cacert ~/.local/share/stt-tts/pki/ca.crt https://ravensnest:8080/v1/models   # should answer
stt-tts certs trust llm
stt-tts setup llm        # base URL: https://ravensnest:8080/v1
```

llama-server's documented TLS options (`--ssl-key-file`, `--ssl-cert-file`) set the server's certificate only; they don't make it require one from clients. To authenticate the module to the server, also start llama-server with `--api-key` and enter that key in `stt-tts setup llm`. Requiring client certificates is a job for the planned reverse proxy.

## Stubbed endpoints

The main workflow (`stt_tts/pipeline.py`) is complete; every endpoint it calls is replaceable through config. These are stubs today:

| Endpoint | Interface | Stub now | File |
| --- | --- | --- | --- |
| Audio capture | `AudioInput` | `stub_mic` emits silence | `stt_tts/adapters/stub.py` |
| Audio playback | `AudioOutput` | `stub_speaker` counts bytes | `stt_tts/adapters/stub.py` |
| Button | `ControlInput` | `stub_button` never presses | `stt_tts/adapters/stub.py` |
| Speech to text | `STTEngine` | returns a placeholder transcript | `stt_tts/engines.py` |
| Text to speech | `TTSEngine` | returns silence sized to the text | `stt_tts/engines.py` |
| Zenoh adapters | all five | written for zenoh 1.x, untested against a router | `stt_tts/adapters/zenoh_adapter.py` |
| Model download | installer step | skipped | `installer/core.py` |
| LLM network call | `LLMBridge.send` | request built and validated; reply is a placeholder | `stt_tts/adapters/llm_http.py` |
| Output sealing | `Sealer.seal` | settings saved; not implemented | `stt_tts/security.py` |
| Live capture and playback threads | pipeline | synchronous | `stt_tts/pipeline.py` |

To add a device or engine, subclass the interface, give it a `name`, and register it in `stt_tts/adapters/__init__.py` or `stt_tts/engines.py`. It then appears in the config options and the GUI installer.

Try the workflow with stubs:

```bash
stt-tts demo
```

## What our rig uses

The reference setup this module is being built for. Engine choices are planned, not yet implemented.

| Part | Choice |
| --- | --- |
| Voice server | Intel Core i7-6700T (4 cores / 8 threads, AVX2), 8 GB DDR4-2133 |
| LLM host | Separate machine with an NVIDIA RTX 3080 Ti Laptop GPU (16 GB), reached over the network |
| Speech to text | [Moonshine Small Streaming](https://huggingface.co/moonshine-ai/moonshine-streaming-tiny) (123M parameters, MIT), on CPU |
| Text to speech | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) int8 ONNX (Apache-2.0), on CPU, with a blended voice |
| Batch transcription (optional) | Parakeet TDT 0.6B v2 on the LLM host's GPU |
| Spool | tmpfs at `/mnt/voice-spool`, 256 MB |
| Transport | Zenoh, default key names |
| LLM link | llama.cpp's `llama-server` on the LLM host with its own TLS (`--ssl-key-file`, `--ssl-cert-file`), certificate from our own CA |
| Control | One physical button (hardware to be chosen) |

Notes from sizing the voice server: its RAM runs single-channel (one stick in two slots), and a matching second stick would double memory bandwidth. It's a 35 W "T" part, so sustained speech synthesis may run nearer the 2.8 GHz base clock than the 3.6 GHz turbo.

## Development

```bash
python3 -m unittest discover -s tests   # state machine, pipeline, config, security, rollback,
                                        # and real TLS handshakes with stt-tts certs
python3 -m stt_tts demo                 # run from the repo without installing
```

```
install.sh              entry point: finds Python, starts the installer
installer/              requirement checks, GUI and CLI front ends, install/modify/replace core
stt_tts/                the module: config, state machine, pipeline, adapters, engines,
                        security overlays, credential store, LLM profile
docs/flow.svg           the diagram above
tests/                  unit tests (standard library only)
```

## Roadmap

- Implement the Moonshine and Kokoro engines and model download
- Streaming LLM call (sentence-by-sentence TTS)
- Output sealing with PyNaCl (sign, then encrypt to recipients)
- Reverse proxy support (e.g. Caddy) for a shared inference server: one address and one set of certificates for several services, certificate renewal without reloading the model
- Real capture and playback adapters (PipeWire/ALSA, Bluetooth)
- Threaded live capture so partial transcripts stream while you speak
- Physical button adapter
- Verbal observer: say "Obs, ..." to edit or read back pending input before it's sent
- Optional echo cancellation, for opening the mic during playback
- Choose a license
