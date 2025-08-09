# Local Setup Guide

Steps to run the AI agent tests locally on Windows.

## Prerequisites

- **Python 3.11+**
- **Unity Hub** with an Android module installed (provides SDK, JDK, ADB)
- **Android Studio** (only needed once to install the emulator and system images)
- **OpenAI API Key** with access to the `computer-use-preview` model, OR
- **Anthropic API Key** with access to Claude Computer Use models

## 1. Install Android Emulator via Android Studio

The Unity SDK does **not** include the Android Emulator by default. You need to install it through Android Studio's SDK Manager.

1. Open Android Studio → **Settings → Languages & Frameworks → Android SDK**
2. Set the **SDK Location** to the Unity SDK path:
   ```
   C:\Program Files\Unity\Hub\Editor\<version>\Editor\Data\PlaybackEngines\AndroidPlayer\SDK
   ```
3. In the **SDK Platforms** tab, enable **Android 14 (API 34)**
4. In the **SDK Platforms** tab → **Show Package Details**, mark:
   - `Google APIs Intel x86_64 Atom System Image`
5. In the **SDK Tools** tab, enable:
   - `Android Emulator`
   - `Android SDK Platform-Tools` (should already be installed)
6. Click **Apply** and wait for the download

## 2. Create the AVD (one time only)

The `emulator_setup.py` script can create AVDs, but the old `sdkmanager` under `tools/bin/` is incompatible with modern JDKs. Use the newer `cmdline-tools` avdmanager instead.

Open a terminal and run (adjust the Unity version number):

```powershell
$env:JAVA_HOME = "C:\Program Files\Unity\Hub\Editor\<version>\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK"

& "C:\Program Files\Unity\Hub\Editor\<version>\Editor\Data\PlaybackEngines\AndroidPlayer\SDK\cmdline-tools\<cmdline-version>\bin\avdmanager.bat" create avd -n UnityTestAVD -k "system-images;android-34;google_apis;x86_64" --force
```

To find `<cmdline-version>`, check the folder name under `SDK\cmdline-tools\` (e.g. `16.0`).

Verify the AVD was created:

```powershell
& "C:\Program Files\Unity\Hub\Editor\<version>\Editor\Data\PlaybackEngines\AndroidPlayer\SDK\emulator\emulator.exe" -list-avds
```

Should output `UnityTestAVD`.

## 3. Python environment

```powershell
cd E:\Development\nospoon-ai-e2e-agent-test
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Set environment variables

```powershell
# Android SDK (required for both providers)
$env:ANDROID_SDK_ROOT = "C:\Program Files\Unity\Hub\Editor\<version>\Editor\Data\PlaybackEngines\AndroidPlayer\SDK"
$env:ANDROID_HOME = $env:ANDROID_SDK_ROOT
$env:JAVA_HOME = "C:\Program Files\Unity\Hub\Editor\<version>\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK"

# OpenAI provider (default)
$env:OPENAI_API_KEY = "sk-your-key"

# Claude provider (alternative)
$env:ANTHROPIC_API_KEY = "sk-ant-your-key"
$env:LLM_PROVIDER = "claude"
```

> **Important:** Make sure `ANDROID_SDK_ROOT` points to the correct Unity version. An outdated value (pointing to an uninstalled version) will cause failures.
>
> **Tip:** If `LLM_PROVIDER` is not set, it defaults to `openai`. Set it to `claude` to use Anthropic Claude instead.

## 5. Start the emulator

```powershell
& "$env:ANDROID_SDK_ROOT\emulator\emulator.exe" -avd UnityTestAVD -no-snapshot -no-boot-anim -netdelay none -netspeed full
```

Wait until the emulator finishes booting (you'll see the Android home screen). Leave this terminal open.

## 6. Run the agent test

In a **new terminal** (with the same env vars and venv activated):

```powershell
.venv\Scripts\activate

# With OpenAI (default)
python -m source.agent_runner test/app_demo_test.json

# With Claude
$env:LLM_PROVIDER = "claude"
python -m source.agent_runner test/app_demo_test.json
```

Reports will be generated in `reports/agent_<timestamp>_<package>/`.

## Troubleshooting

### `ANDROID_SDK_ROOT not found`
Your env var points to an old Unity version that was uninstalled. Update it to the current version path.

### `javax/xml/bind/annotation/XmlSchema` error
The `sdkmanager.bat` or `avdmanager.bat` under `SDK\tools\bin\` is outdated and incompatible with modern JDKs. Use the one under `SDK\cmdline-tools\<version>\bin\` instead.

### `No AVDs available`
You need to create an AVD first (see step 2).

### `Required Android tools not found (adb/emulator)`
The emulator is not installed in the SDK. Install it via Android Studio SDK Manager (see step 1).

### Agent stuck in a loop
The anti-loop mechanism sends a BACK key after 10 identical actions. For multiplayer games, the agent may get stuck waiting for opponents. This is expected behavior for online-only game modes.
