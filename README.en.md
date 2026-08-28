# AI Video Studio

[简体中文](README.md) · **English**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A desktop-first workbench for making AI-native long-form video.

> **AI produces footage · The system engineers and orchestrates it · The human directs**

AI can produce a good-looking few seconds, but it cannot assemble a film: faces drift between
shots, cuts don't connect, and changing one thing means re-rendering a whole batch. AIVS is not a
model — it is the engineering system that turns "a few seconds" into "a film": character
appearances stay locked, what gets fed to the model is explicit down to the frame, every
generation is kept as an immutable version, and the timeline and export never depend on AI.

## What it is · what it isn't

| It is | It isn't |
|---|---|
| A video project and orchestration layer: characters, scenes, shots, context, versions, timeline | Not a model, and it does not train models |
| A client of ComfyUI / any REST service | It does not maintain, parse, or rewrite your ComfyUI graph |
| Local-first: one project = one self-contained folder you can copy to another machine | Not a cloud service; your footage is never uploaded |
| LLM-optional: the manual path covers the entire workflow | Not a tool that requires wiring up an LLM first |

## Four hard constraints

1. **No specific video model is baked into the business layer** — there is no `if model == "wan"`.
   All differences live in the provider adapter layer. A shot only records a capability
   (`text2image` / `image2video` / `first_last_frame` / `upscale`) and a provider name.
2. **The LLM is optional** — the default is `llm_provider="none"`; AI entry points return
   `LLM_UNAVAILABLE` and always spell out the manual alternative. The source of truth is always
   the `project.db` inside the project folder.
3. **Generated versions are never overwritten** — `GenerationVersion` is append-only and freezes
   that run's prompt, context, parameters and output. No endpoint can rewrite an existing version;
   you can only change which version is currently adopted.
4. **Never fail silently** — every error carries four parts: `{code, title, detail, suggestions}`,
   and the UI must surface the suggestions. Even a failed startup shows a reason, never a blank window.

## The core chain

```text
Character → Appearance → Scene → Shot → Context → Generation
         → GenerationVersion → Clip → Timeline → Final Video
```

No step can be skipped: before a shot can be generated, its context must be complete — who is in
it, where it happens, which frame it starts from.

## Walkthrough (first run)

1. **Start the dev environment**: `python scripts/dev.py` (see Quick start below).
2. **Create a project**: pick an empty folder; AIVS writes `project.aivs.json` + `project.db`
   into it. The whole folder is portable at any time.
3. **Prepare a generation preset**: in ComfyUI, rename the **titles** of your entry nodes to
   `AIVS_PROMPT` / `AIVS_FIRST_FRAME` / `AIVS_REF_1`… → export via "Save (API Format)" → upload it
   on the settings page and set it as the default. AIVS only fills values into nodes matched by
   title; the loras, speed-up nodes and samplers in your graph are never inspected or modified
   (see [docs/05](docs/05-生成方式与节点要求.md)).
4. **Lock appearances**: character → appearance → character sheet; location → day/night/rain
   variants → reference images. This step decides whether the next few dozen shots stay consistent.
5. **Break down the script**: paste text and let the AI split it into scenes and shots, or build
   them by hand — both paths are equivalent.
6. **Set up each shot**: assign first/last frames on the storyboard, draw transition links between
   cards, and open the context inspector to confirm exactly what will be fed to the model (every
   entry carries `included` and a reason).
7. **Generate**: after queueing, watch the queue and logs in the bottom console
   (`Ctrl` + `` ` ``). Every run is kept as a version; you adopt one, and nothing is ever deleted.
8. **Finish**: auto-assemble the timeline, adjust it by hand, then export. This step only uses
   FFmpeg — neither ComfyUI nor an LLM needs to be online.
9. **Move to another machine**: "Export current project" on the start page writes the project into a
   single `.aivspkg` (assets included by default, rendered output not), and "Import project package"
   restores it elsewhere. To move just one scene's setup (characters, locations, props, shot
   structure), use "Export this scene" / "Import a scene" on the scene flow graph — a scene package
   imports into any project you have open. **Packages carry neither the preset graph nor any key or
   service address** — only a checklist of what the project requires, compared against your machine
   before you import.

Step-by-step details are in [docs/04](docs/04-功能开发步骤与体验.md).

## Requirements

| | Required? | Notes |
|---|---|---|
| Python | 3.11+ | Backend (FastAPI + SQLite) |
| Node.js | 20+ | Frontend (Vue 3 + Vite + TS) |
| FFmpeg | Shipped with the app | Frame extraction / transcoding / export. `python scripts/fetch_ffmpeg.py` downloads it into `bin/` — you don't need to install it yourself |
| ComfyUI | Optional | While offline, asset work, storyboarding, timeline and export all keep working; generation buttons stay disabled with the reason shown |
| LLM | Optional | Disabled by default; script breakdown and the AI director panel both have manual equivalents |
| Rust | Only to build the desktop shell | `cargo install tauri-cli --version "^2"` |

## Quick start

```bash
python scripts/dev.py
```

One command starts the backend (`127.0.0.1:8765`) and the frontend
(<http://localhost:5173>), runs a dependency check, opens your browser, and shuts both down
together on `Ctrl+C`. On Windows you can also double-click `start.cmd`; on macOS / Linux use
`./start.sh`. Useful flags: `--backend-only` / `--frontend-only` / `--port 8899` / `--no-open`.

Install dependencies once after cloning:

```bash
cd backend && python -m pip install -e ".[dev]"
```

```bash
cd frontend && npm install
```

```bash
python scripts/fetch_ffmpeg.py
```

If a step is missing, the launcher tells you exactly what to run.

### Docker (run the orchestration side on a Linux server)

ComfyUI usually lives on a separate GPU machine, so the containers only run the frontend, the
backend and FFmpeg:

```bash
cp .env.docker.example .env && ./docker-start.sh
```

Point `AIVS_COMFY_BASE_URL` in `.env` at your GPU machine. Full instructions:
[docs/docker-deployment.md](docs/docker-deployment.md).

## How generation works: node titles only

AIVS **does not maintain the graph on the model side**. The ComfyUI adapter injects values into
nodes matched by title — nothing is parsed, validated or rewritten:

| Title | Purpose |
|---|---|
| `AIVS_PROMPT` / `AIVS_NEGATIVE` | Prompt / negative prompt (`AIVS_PROMPT` is the only required entry for image or video output) |
| `AIVS_FIRST_FRAME` / `AIVS_LAST_FRAME` | First / last frame. Strict first-last-frame work (transitions) needs both |
| `AIVS_REF_1` … `AIVS_REF_9` | Reference image slots — character sheets and location references go here |
| `AIVS_REF_VIDEO_1..4` / `AIVS_REF_AUDIO_1..4` | Reference video / audio, counted separately from images |
| `AIVS_DURATION` / `AIVS_SEED` | Duration (frame count) / random seed |
| `AIVS_SOURCE_VIDEO` | Input for second-pass work (upscale / interpolation): the clip you already rendered |
| `AIVS_AUDIO_TEXT` / `AIVS_AUDIO_PROMPT` / `AIVS_VOICE_REF` | Entries for the audio graph (sound is a separate chain) |

**First/last frames are not reference material**: frames decide where the picture starts and ends;
references decide who appears, where, and doing what. Too few slots degrades rather than fails —
whatever could not be fed in is recorded in the frozen version parameters and shown in the UI.

Besides ComfyUI there is `http_api`, a generic REST contract. The contract and a minimal
acceptance checklist are in [docs/05](docs/05-生成方式与节点要求.md).

## Configuration

Three layers, highest priority first: **`settings.json` written by the settings page → `AIVS_`
environment variables (including `backend/.env`) → code defaults**. The settings page shows which
layer each effective value came from.

Common variables: `AIVS_PORT`, `AIVS_COMFY_BASE_URL`, `AIVS_FFMPEG_PATH`, `AIVS_WORKER_LIMIT`,
`AIVS_LLM_PROVIDER`, `AIVS_RUNTIME_DIR`. Override the frontend proxy target with `AIVS_BACKEND`.
API keys are never returned in clear text — only a mask and whether a value exists.

## Layout

```text
xunjie_video_ide/
├── backend/          Python + FastAPI + SQLite (api → services → persistence)
│   ├── app/api/          Deliberately thin routers: Pydantic body + delegation
│   ├── app/services/     Business layer; each module exports a single instance
│   ├── app/generation/   Provider adapters (ComfyUI presets / generic REST) + preset parsing
│   ├── app/ai/           LLM protocol adapters + AI director (write tools only propose, never commit)
│   └── alembic/          Migrations, run per project database
├── frontend/         Vue 3 + Vite + TS (features/ per feature, shared/ for common pieces)
├── tauri/            Tauri 2 desktop shell, hosts the backend as a sidecar
├── scripts/          dev.py (one-command dev env) · fetch_ffmpeg.py (bundled FFmpeg)
├── docker/           Containerised deployment (orchestration on a server, ComfyUI stays on the GPU box)
├── docs/             Design documents (Chinese)
└── bin/              Bundled FFmpeg / FFprobe (not in git)
```

Project data does **not** live in this repository: each project is a folder you choose, containing
`project.aivs.json` + `project.db` + `assets/` + `generations/` + `cache/`. There is no global
database.

## Documentation

| Document | Contents |
|---|---|
| [01 Stack & architecture](docs/01-技术栈与架构.md) | Technology choices and trade-offs, process architecture, generation chain, module boundaries, disk layout, risks |
| [02 Feature specification](docs/02-功能开发文档.md) | Information architecture, page inventory, per-feature specs and acceptance, state machines, M0–M6 milestones, design language |
| [03 Data model & contracts](docs/03-数据模型与接口契约.md) | Full schema, error contract, REST / WS endpoints, scheduler spec, on-disk conventions, test inventory |
| [04 Build steps & experience](docs/04-功能开发步骤与体验.md) | Steps 1–9 and the completion criteria for each |
| [05 Generation & node requirements](docs/05-生成方式与节点要求.md) | The three integration modes, `AIVS_*` title convention, REST contract, minimal acceptance checklist |
| [Docker deployment](docs/docker-deployment.md) | Compose and single-container modes, wiring up an external ComfyUI |

The documents are currently Chinese-only.

## Development

```bash
cd backend && python -m pytest -q
```

```bash
cd backend && python -m ruff check . && python -m ruff format .
```

```bash
cd frontend && npm run typecheck && npm run lint && npm run build
```

Migrations run **per project database** — there is no global one:

```bash
cd backend && python -m alembic -x db=<project-dir>/project.db upgrade head
```

Before changing anything, read [CLAUDE.md](CLAUDE.md): it records where each of the four hard
constraints is enforced, the responsibility boundary of every layer, and a long list of reasons why
certain things are deliberately *not* written the obvious way.

## Status

- **Backend and frontend are fully wired together**: 23 services + 21 routers (including `/ws`),
  with 352 tests passing in the backend suite. Every registered feature really talks to
  the backend; when a single capability is missing (ComfyUI offline, no LLM configured) the button
  stays disabled with the reason in its tooltip — no fake UI, no fabricated data.
- **Desktop installers are not built yet**: `tauri.conf.json` already targets Windows, macOS and
  Linux, but the build script for the Python sidecar does not exist yet, so today only the
  development setup runs.
- **Linux**: the backend and Docker paths are in use; the desktop shell's AppImage target is
  unverified.

## Licence

Released under the **MIT** licence — see [LICENSE](LICENSE). Use it, change it, ship it in a closed
source product; the only obligation is to keep the copyright and licence notice.

**The FFmpeg / FFprobe binaries shipped with the application are not covered by that licence.** They
are third-party static builds compiled with `--enable-gpl`, so the binaries themselves are GPL. This
application only ever invokes FFmpeg as an external process (`app/core/ffmpeg.py` is the single entry
point) and never links its libraries, so the two are merely aggregated and the MIT licence of this
project's own code is unaffected — but **whoever distributes an installer must satisfy the GPL**
(ship the licence text and a way to get the corresponding source). Details and sources are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). To avoid shipping GPL binaries altogether, skip
`scripts/fetch_ffmpeg.py`, let users install FFmpeg themselves, and point `AIVS_FFMPEG_PATH` at it.

ComfyUI, model weights and workflow graphs are **not distributed by this project**; their licences
belong to their respective publishers.




