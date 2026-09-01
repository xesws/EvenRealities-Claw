# EvenRealities-Claw 🦞👓

[中文版 →](README.zh-CN.md)

Talk to a private agent through [Even Realities G2](https://www.evenrealities.com/) smart glasses.
Hold a temple, speak, and the answer comes back on the 576×288 heads-up display — laid out,
paginated, and throttled by your own server. The same display is also an **MCP surface**, so any
vendor's model (Claude Code, your IDE, anything that speaks MCP) can render to the glasses you are
wearing.

**Nothing in the demo is faked.** Real microphone, real `faster-whisper` transcription, real
DeepSeek, real tool calls against real APIs, real firmware glyph metrics. The brief allowed the
*data* to be invented; in the end even that was unnecessary. The one substituted input in the whole
repository is the demo audio, and audio is data. The section below is about how you can check that
yourself rather than take this paragraph's word for it.

---

## Architecture

![Architecture](docs/assets/diagrams/architecture.en.svg)

> Every diagram in this README is a real [draw.io](https://app.diagrams.net/) file. The `.svg` you
> see is rendered from the same XML that `.drawio` contains, so the picture and the source can
> never drift apart. Open `docs/assets/diagrams/*.drawio` to edit. See [Diagrams](#diagrams).

Four processes, three trust levels:

| Process | Port | Holds | Purpose |
|---|---|---|---|
| **Lens plugin** | — | a revocable device JWT | Runs inside the official Even App WebView. A dumb terminal with a watchdog: it opens the mic, ships PCM, and paints whatever frames arrive. |
| **Lens Gateway** | `8443` | device keys, the ASR model | WS server, pairing, streaming ASR, text layout, HUD state machine, frame lease, control plane. |
| **lens_agent** | `18790` | **the only copy of the LLM key** | The hand-written agent loop. Loopback only. |
| **lens_mcp** | `8765` | nothing | MCP surface for outside models. No mic, no ASR, no device credentials — it can only do what the control plane's nine routes allow. |

The split is the point. `lens_mcp` is the process a third-party model talks to, so it is the
process that holds nothing. It cannot read the microphone because it has no code that can.

---

## What is real, and how to check

This project's central claim is that the demo is not a mock, so here is how to falsify that.

**1. Ask the gateway who the agent is.** Provenance (`W6`) is recorded at the agent handshake, not
asserted in a doc:

```bash
curl -s http://127.0.0.1:8443/healthz | python3 -m json.tool
```

```json
"agent": { "backend": "lens", "model": "deepseek-v4-flash",
           "production": true, "endpoint": "ws://127.0.0.1:18790" }
```

When `production` is `false`, the glasses' own status bar renders a `?` next to the badge.
**The screen tells on itself** — you do not have to trust the terminal.

**2. Run the whole chain from a WAV file, with no browser involved.**
`demo/verify_audio.py` *is* a device: it pairs over WS, presses PTT, streams PCM in real time,
releases, and prints every frame the gateway sends back. The only substituted component is where
the sound comes from, and sound is data.

```bash
cd gateway && .venv/bin/python ../demo/verify_audio.py ../demo/audio/en-weather.wav
```

Real output, measured on this machine:

```
S2  Lens ● Listening 0:02   | What's the weather like today? Do I need a jet?▌
S3  Lens → Heard            | What's the weather like today? Do I need a jacket?
S4  Lens ◐ Thinking 5s      | ...
S5  Lens ◆ Weather          | ← the model actually called the weather tool
S7  Lens √ Done             | It is 16 degrees and overcast in San Francisco, with a high of 18
```

Note `jet` → `jacket` between S2 and S3. That is streaming ASR correcting itself on the final
pass; a scripted demo would not bother to be wrong first.

**3. Measured latency** (same script, real voice → real DeepSeek, one run each):

| Clip | Audio | Whole turn | Path |
|---|---|---|---|
| `en-navigation.wav` | 2.73 s | **6.1 s** | no tool |
| `en-park.wav` | 2.94 s | **6.7 s** | no tool |
| `en-weather.wav` | 2.78 s | **11.5 s** | one tool call |

A tool call roughly doubles the turn, because **budgets here are counted in model round-trips, not
in tool latency** — the tool itself returns in microseconds; it is the second trip to the model
that costs five seconds.

**4. Even the data turned out not to need faking.** The brief permitted invented data. In the end
nothing is invented: `weather` calls Open-Meteo, `currency` calls Frankfurter, `now` / `days_until`
/ `calc` compute, `list_*` and `remind_*` read and write real files, and `device` returns **`null`
when no telemetry has ever arrived** rather than a plausible battery percentage. The only
substituted input in the whole repository is the demo audio in `demo/audio/`, synthesized with
macOS `say` at 16 kHz mono — matching the format the glasses' 4-mic array actually sends.

---

## A voice turn, end to end

![Voice turn](docs/assets/diagrams/voice-turn.en.svg)

Three things in that path are less obvious than they look:

**The mic really opens first.** The plugin awaits `audioControl(true)` and only then sends
`ptt start`, so the gateway's "no audio" watchdog measures something true. The earlier design gave
the mic 1.4 s to produce its first chunk — which has to cover WS RTT, the BLE round trip, firmware
mic start, and the plugin's 200 ms buffering. On real hardware that would have false-alarmed on
almost every turn. It is now two separate timers with a 2.5 s warm-up.

**The screen is dumb on purpose.** Wrapping, pagination, and throttling all happen on the server,
which emits idempotent whole-screen frames with a monotonic `seq`. The glasses never decide
anything about layout, so a dropped or duplicated frame cannot corrupt the display.

**Layout uses the firmware's own glyph advances.** `@evenrealities/pretext` is the official metrics
library that mirrors the LVGL build on the device. The Python wrap engine reproduces its
arithmetic, and the test suite uses the JavaScript library as an **external oracle** — it asserts
that both agree on every break position for the whole corpus. The server's idea of a line is the
device's idea of a line, by construction rather than by a safety margin.

---

## The agent, and why it is small

![The four gates](docs/assets/diagrams/gates.en.svg)

`lens_agent` is a ~900-line hand-written loop over an OpenAI-compatible endpoint. It is not built
on a framework, and that is a security decision rather than an aesthetic one: at this size, the
complete set of things the agent is able to do is short enough for one person to read end to end.

It has **12 tools** (`now`, `days_until`, `device`, `weather`, `calc`, `currency`, `list_show`,
`list_add`, `list_remove`, `remind_set`, `remind_list`, `remind_cancel`) and **7 skills**
(`ask`, `daily`, `weather`, `math`, `list`, `device`, `remind`), behind four gates:

| Gate | Rule | Why it holds |
|---|---|---|
| **1** | The capability enum is `READ \| WRITE`. There is no `exec` tier. | Not "the model is told not to run commands" — there is no code path that runs one. |
| **2** | `route()` picks the skill, and its tool whitelist, with plain regex. | The model never chooses its own permissions. By the time it sees the prompt, its toolset is already fixed. |
| **3** | WRITE tools are bound to a specific file at import time. | No tool takes a path parameter, so there is no argument the model could supply to reach a different file. |
| **4** | One JSON line per call **and per denial**, appended to `~/.lens-agent/audit.jsonl`. | A refusal that leaves no trace is indistinguishable from an attack that was never attempted. |

This is what makes prompt injection structurally uninteresting here. An injected instruction
arrives inside the user turn — *after* `route()` has already run. The skill, the tool whitelist,
and the latency budget are fixed by then, so the sentence has nothing left to change. That is the
difference between a guardrail and a request not to.

One design rule earned the hard way, now written into
[docs/AGENT-LAYER.md](docs/AGENT-LAYER.md): **routing judges intent, the skill judges
feasibility.** When routing tried to judge feasibility, the agent answered "I can't set reminders
yet" to requests it could in fact serve. Claiming you cannot do something you can do is the same
class of failure as making something up.

---

## Quick start

```bash
python3 -m venv gateway/.venv && gateway/.venv/bin/pip install -r gateway/requirements.txt
export LENS_LLM_API_KEY=sk-...          # DeepSeek, or any OpenAI-compatible endpoint

./demo/start.sh --lens --en             # recommended: real agent, English HUD
./demo/start.sh --lens                  # same, Chinese HUD
./demo/start.sh --real                  # talk to a local OpenClaw gateway instead
./demo/start.sh                         # offline stand-in (the HUD badge shows "?")
```

The script prints a pairing code and a URL. Open it, allow microphone access, enter the code, and
hold the on-screen PTT button. Without glasses you get the **browser harness**, which renders the
HUD at true 576×288 using the official `pretext` glyph advances — the line breaks match the
hardware.

The key is read from the environment only. It is never written to disk and never enters the repo.

## Connect any vendor's model

```bash
cd gateway && .venv/bin/pip install -r requirements-mcp.txt
PYTHONPATH=. .venv/bin/python -m lens_mcp
claude mcp add --transport http even-glasses http://127.0.0.1:8765/mcp
```

**8 tools · 3 resources · 1 prompt.** `textkit_paginate` needs no device at all — it exposes the
layout engine as a pure function, which is the cheapest way to prove a foreign model is really
reaching your code. `hud_show` / `hud_page` / `hud_clear` / `hud_release` write to the display and
therefore need a **frame lease**.

The lease exists because one screen has one holder. Two MCP clients cannot silently overwrite each
other — the loser gets a structured `LEASE_HELD` with the current holder and a TTL, instead of
last-write-wins. And **the user speaking preempts unconditionally**: your voice always outranks a
robot's render. The preempted client finds out by polling, because the MCP specification has no
way for a server to push.

---

## Hardware constraints that shaped the design

Every number below has a source in [docs/HARDWARE-SPEC.md](docs/HARDWARE-SPEC.md), tagged by
evidence level (vendor doc / measured from SDK / measured in the official simulator / measured
against the metrics library / awaiting real hardware).

| Constraint | Consequence |
|---|---|
| 576 × 288 px per eye, 4-bit greyscale (16 levels of green) | The whole layout budget. |
| **Line height is a fixed 27 px** | Body box = 216 px = **exactly 8 lines**. An earlier version assumed 5 lines and wasted 37% of the screen on every page. |
| **No font-size, no bold, no alignment control** | Three "font size" columns in the old protocol doc described something the hardware does not have. Deleted. |
| Text brightness is `textColor` 0–4, not 16 levels | The only visual hierarchy available: status bar 4, body 3, footer 2. |
| The font is **not monospaced** | A character-budget layout model is wrong in principle. Layout is driven by real per-glyph advances. |
| **Characters outside the font are silently dropped** — no tofu box | 10 of the 13 glyphs the HUD originally used do not exist on G2. `⛓ Connection lost` would have rendered as ` Connection lost`: the one alert that most needs to be seen would have lost its anchor. See [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md). |
| `createStartUpPageContainer` may be called **once** per page lifetime | The self-built harness used to allow unlimited calls and always return success, so this class of bug was invisible until the official simulator was wired in. |

---

## Repository layout

```
plugin/     Even Hub plugin (TypeScript / Vite, runs in the official App WebView)
            harness/  browser fixture with fault injection · probe/  official-simulator probe
            tools/    metrics export, pretext oracle, simulator automation
gateway/    Lens Gateway (Python / aiohttp)
            formatting/  pretext metrics → wrap → paginate → sanitize
            device/      HUD state machine, frame lease, telemetry cache
            voice/       PTT / ASR / agent orchestration
            providers/   agent abstraction (openclaw | lens) + provenance
            lens_agent/  the hand-written agent (own process, holds the LLM key)
            lens_mcp/    MCP surface (own process, holds nothing)
protocol/   Lens protocol v1.1 + machine-readable HUD contract
demo/       one-command local chain · verify_audio.py · chat.py
tools/      diagrams.py — generates every diagram in this README
docs/       design, hardware spec, glyph table, agent layer, MCP surface
```

| Component | Lines |
|---|---|
| Gateway core | 4,403 |
| `lens_agent` | 2,299 |
| `lens_mcp` | 401 |
| Plugin + tooling (TS) | 4,510 |
| **Tests** | **6,571** |

---

## Verification

| Suite | Result | What it covers |
|---|---|---|
| `pytest` | **590 passed** | Layout invariants, HUD state machine, lease semantics, auth, telemetry, agent gates, reminders |
| `vitest` | **82 passed** | Plugin bridge, PCM payload shapes, WS protocol, harness fault injection |
| `tsc --noEmit` | clean | |
| `e2e_sim.py` | **32/32** | Full voice chain against the simulator, self-contained |
| `e2e_mcp.py` | **27/27** | Four real processes: MCP client → `lens_mcp` → control plane → gateway → device |
| `e2e_agent.py` | **23/23** | Real DeepSeek, end to end |
| `test_asr_quality.py` | CER **0.0085** (threshold 0.05) | Self-built 10-clip dataset with ground truth, three voices, run through the production ASR path |
| `test_metrics_oracle.py` | — | Python wrap engine vs. the official `pretext` library, break position by break position |

The first four suites need no service outside this repository. CI is in `.github/workflows/ci.yml`.

Two conventions the test suite follows, because they are the reason the numbers mean anything:

- **Every new test is mutation-checked.** Break the code deliberately; if the test still passes, it
  was decoration. Several tests were rewritten after failing this.
- **A test that measures the fixture instead of the code is worse than no test.** One regression
  suite here cancelled its tasks before they ever started, so the cleanup path it claimed to
  verify never ran. It passed for exactly that reason.

---

## Design principles

1. **The half-second glance.** In any state, the leftmost glyph of the status bar tells you what
   the system is doing. Every glyph in that set has been confirmed present in the G2 font, by the
   official metrics library and by simulator screenshots.
2. **The glasses are a dumb screen.** Wrapping, pagination, and throttling happen on the server;
   frames are idempotent and whole-screen.
3. **Credentials never leave the server.** The phone holds a short-lived, revocable device JWT.
   The LLM key exists in exactly one process, and that process listens on loopback only.
4. **Push to talk.** No wake word, no always-on listening, no raw audio written to disk.
5. **One screen, one holder.** Voice and MCP share the display through a lease; the user speaking
   preempts unconditionally.
6. **Permissions come from architecture, not from self-restraint.** The outward-facing MCP process
   can do exactly what nine control-plane routes allow. The agent's capability enum has no `exec`
   tier at all.
7. **The screen is not allowed to lie.** A non-production peer gets a `?` on the badge. An answer
   cut short by its budget ends in `… (cut off)` rather than a `√ Done`. Telemetry that has
   never been reported returns `null` instead of a plausible battery percentage.

---

## Diagrams

Generated by `tools/diagrams.py`, which emits both formats from one geometry:

```bash
python3 tools/diagrams.py --gen
```

| File | |
|---|---|
| `docs/assets/diagrams/*.drawio` | Real draw.io files. Open at [app.diagrams.net](https://app.diagrams.net/) or in the VS Code extension. |
| `docs/assets/diagrams/*.svg` | Rendered from the same XML, and each SVG carries the `.drawio` source in its `content` attribute — so the SVG itself is editable in draw.io. |

English and Chinese share one geometry and differ only in labels, so the two language editions of
this README show the identical system.

---

## Documentation

| Document | Contents |
|---|---|
| [REPORT.md](REPORT.md) | **Delivery report**: what was built and how, the full first-run procedure once the glasses arrive, and troubleshooting |
| [protocol/PROTOCOL.md](protocol/PROTOCOL.md) | Lens protocol v1.1 — plugin ↔ gateway WS: auth, render frames, telemetry uplink, timing |
| [docs/HARDWARE-SPEC.md](docs/HARDWARE-SPEC.md) | **G2 specification baseline** — the single source of truth for every hardware constant, each tagged with its evidence level |
| [docs/AGENT-LAYER.md](docs/AGENT-LAYER.md) | **The agent** — the loop, the four gates, and four measured facts about the DeepSeek integration (§13.1) |
| [docs/MCP-SURFACE.md](docs/MCP-SURFACE.md) | **Hardware MCP surface** — tools, lease semantics, auth design, four-process end-to-end evidence |
| [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md) | Which glyphs G2 can draw, which it cannot, with screenshot and metrics evidence |
| [docs/SIMULATOR-PARITY.md](docs/SIMULATOR-PARITY.md) | Every conclusion classified: settled by the official simulator / self-built fixture only / requires real hardware |
| [docs/DESIGN.md](docs/DESIGN.md) | System design, the glance-HUD UI spec, red-team list R1–R14 |
| [docs/DEVELOPMENT-PLAN.md](docs/DEVELOPMENT-PLAN.md) | Milestones M0–M7 with acceptance criteria |
| [protocol/hud-contract.json](protocol/hud-contract.json) | Machine-readable HUD contract — gateway and plugin read the same file |

---

## Status

**v0.7.0.** Simulator loop, real-hardware MVP, and milestones M0–M7 are complete.

Six things remain genuinely unverifiable without the physical glasses, and they are listed as such
rather than quietly assumed: BLE render timing and flicker, whether temple events actually reach
the WebView, microphone arbitration and start-up latency, the `audioControl` duration ceiling,
survival in background/lock, and the real `oversize` threshold for a full-canvas layout. Everything
else has been settled against the official simulator, the official metrics library, or real
end-to-end runs.

When the glasses arrive, follow §5 of [REPORT.md](REPORT.md).

> This repository is public. The documentation is redacted — no internal ports, paths, or
> credentials.
