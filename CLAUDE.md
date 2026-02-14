# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered end-to-end testing framework for Android apps. An LLM with Computer Use capabilities (OpenAI or Anthropic Claude) visually navigates an Android emulator based on natural language goals defined in JSON test specs. The agent takes screenshots, decides actions (tap, swipe, type), executes them via ADB, and evaluates success criteria visually.

## Commands

```bash
# Setup
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements.txt

# Run AI agent test with OpenAI (default, requires OPENAI_API_KEY)
python -m source.agent_runner test/app_demo_test.json

# Run AI agent test with Claude (requires ANTHROPIC_API_KEY)
LLM_PROVIDER=claude python -m source.agent_runner test/app_demo_test.json

# Run deterministic (non-AI) test
python -m source.test_runner test/kokoro_demo_test.json

# Extract APK metadata
python -m source.apk_info apk/app-release.apk

# Validate Unity Android environment (Windows only)
python -m source.install_check
```

There is no linter, formatter, or test suite configured. No Makefile exists.

## Architecture

### Core Modules (`source/`)

- **`agent_runner.py`** — Main orchestrator. Loads JSON test specs, drives the LLM Computer Use API loop (OpenAI or Claude), translates AI actions to ADB commands, manages multi-step flows, records video, and generates HTML reports. This is the primary entry point.
- **`android_framework.py`** — `AndroidDevice` class wrapping ADB. Provides `tap()`, `swipe()`, `input_text()`, `keyevent()`, screenshot capture, APK install with automatic space recovery (wipes data on `INSTALL_FAILED_INSUFFICIENT_STORAGE`), and emulator lifecycle management.
- **`emulator_setup.py`** — Creates and starts Android Virtual Devices. Detects SDK/JDK on Windows (Unity-bundled) or Linux (CI env vars). Supports custom device profiles and partition sizes.
- **`test_runner.py`** — Simpler JSON-driven executor for deterministic command sequences (no AI).
- **`apk_info.py`** — APK metadata extraction using `aapt`.
- **`install_check.py`** — Unity Android environment validator (Windows).

### LLM Providers (`source/llm/`)

- **`base.py`** — `LLMProvider` ABC, shared types (`LLMTurnResult`, `LLMOutputItem`), and `create_provider()` factory.
- **`openai_provider.py`** — OpenAI Responses API with `computer_use_preview` tool. Stateless per turn.
- **`claude_provider.py`** — Anthropic Claude with `computer_20251124`/`computer_20250124` tool. Maintains conversation history internally (Claude requires `tool_result` feedback after each `tool_use`).

### Agent Loop (in `agent_runner.py`)

1. Screenshot device → send to LLM provider (OpenAI or Claude)
2. Model returns action (click, type, scroll, drag) or calls `end_test(success)`
3. Action translated to ADB command and executed on emulator
4. Repeat until success, failure, or max turns reached (default 250)

Anti-loop: same action repeated 10 times → automatic BACK key injection.

### Test Specifications (`test/`)

JSON files with: `apk`, `package`, `activity`, `goal`, `suggestions`, `negative_prompt`, `success_criteria`, and optional `steps` (multi-step flows) and `pre_steps` (manual setup before AI). Supports `{timestamp}` placeholder for unique values.

### CI/CD Pipeline

1. Unity Cloud Build produces APK → `scripts/ucb_post_build.sh` uploads to S3 and dispatches GitHub event
2. `.github/workflows/android-agent-dispatch.yml` downloads APK, provisions emulator ("AI Device" 1024x768), runs test, uploads HTML report as artifact
3. `.github/workflows/android-agent.yml` runs on push/PR with hardcoded APK

Custom AVD config lives in `ci/` (hardware-device.xml, hardware-config.ini).

### Reports

Generated in `reports/agent_<timestamp>_<package>/` with: HTML viewer, screenshots, video recording, raw API responses, and summary JSON.

## Key Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `LLM_PROVIDER` | Provider selection (`openai`, `claude`) | `openai` |
| `OPENAI_API_KEY` | Required for OpenAI provider | — |
| `OPENAI_COMPUTER_MODEL` | OpenAI model name | `computer-use-preview` |
| `ANTHROPIC_API_KEY` | Required for Claude provider | — |
| `CLAUDE_COMPUTER_MODEL` | Claude model name | `claude-opus-4-6` |
| `OPENAI_AGENT_MAX_STEPS` | Max agent turns | `250` |
| `OPENAI_AGENT_WAIT_BETWEEN_ACTIONS` | Seconds between actions | `1.5` |
| `ANDROID_SDK_ROOT` | Android SDK path (CI) | auto-detected |
| `EMULATOR_PARTITION_SIZE_MB` | AVD partition size | `2047` |

## Code Style

- English only for all code, comments, and docs
- `snake_case` for Python, `CamelCase` for classes
- Comment the "why" not the "what"; no inline comments
- Respect existing formatting; avoid unrelated style changes
