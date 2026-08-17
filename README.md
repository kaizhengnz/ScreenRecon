# ScreenRecon

English | [简体中文](README.zh-CN.md)

Watch a rectangle on your screen. When the mouse **dwells inside it** for a few seconds,
ScreenRecon captures that region, sends it to the AI, prints the answer in your
terminal, pushes the screenshot plus the answer to your Telegram, and files both away
locally.

Open source, `pip install`-able, bring-your-own-key. There is no hosted backend: your API
key and your screenshots go to the AI provider you configured and to your own Telegram
chat, and nowhere else.

```
mouse dwells in region ──> capture ──┬──> save JPEG
                                     └──> AI     ──┬──> terminal
                                                   ├──> save TXT
                                                   └──> Telegram
```

## Requirements

- Python 3.10+
- Windows 10/11, macOS 12+ (Intel or Apple Silicon), or Linux with X11
- An API key for one of: Anthropic, OpenAI, Google Gemini, or an OpenAI-compatible endpoint (DeepSeek / Moonshot / Doubao). See [Providers and required extras](#providers-and-required-extras) below.
- A Telegram bot token and chat ID

Linux note: Wayland is not supported — the cursor position cannot be read there, and
XWayland is not a workaround (it reports stale coordinates over native Wayland windows
and captures them as black), so ScreenRecon refuses to start in a Wayland session. Log in
with an X11/Xorg session. If your applications really are all X11, `SCREENRECON_FORCE_X11=1`
overrides the check.

## Install

```bash
pip install screenrecon
```

From a checkout:

```bash
pip install -e ".[dev]"
```

## Quick start

```bash
# 1. Set up: pick the region interactively (drag a rectangle across the screen),
#    then fill in credentials and save directory.
screenrecon --configure

# 2. Start watching.
screenrecon
```

Hover inside the region for the configured dwell time (3 seconds by default) and the
capture fires. To fire again, move the mouse out of the region and back in — parking the
cursor there will never re-trigger.

## Commands

| Command | What it does |
| --- | --- |
| `screenrecon` | Watch the configured region |
| `screenrecon --configure` | Interactive setup: drag-to-select region picker, then credentials (verified online) |
| `screenrecon --screen` | Re-pick just the watched region; every other config field is left alone |
| `screenrecon --key` | Prompt for a new API key for the current provider |
| `screenrecon --model` | Pick a new AI model only |
| `screenrecon --prompt` | Pick a new default prompt only (mini-wizard) |
| `screenrecon --prompt "..."` | Watch with `"..."` as the system prompt for this run only; config is untouched |
| `screenrecon --dwell` | Set dwell seconds only |
| `screenrecon --save-dir` | Set the archive directory only |
| `screenrecon --telegram` | Prompt for Telegram bot token + chat ID (as a pair) |
| `screenrecon --show` | Print the current config (credentials masked) and exit |
| `screenrecon ask "question"` | Capture once, answer one question, exit |
| `screenrecon ask` | Capture once, then keep asking questions about that screenshot |
| `screenrecon --prompt "..." ask` | Capture once and use `"..."` as the question |
| `screenrecon --config PATH` | Use an alternative config file |
| `screenrecon --debug` | Watch as usual and show a persistent red outline around the region (visual sanity check) |
| `screenrecon --version` | Print the version |

Global flags come before the subcommand: `screenrecon --config PATH ask "..."`,
not `screenrecon ask "..." --config PATH`.

`ask` exits non-zero if the API call failed, so `screenrecon ask "..." && ...`
does not run on an error.

## Configuration

Default location: `$XDG_CONFIG_HOME/screenrecon/config.json` if that variable is
set, otherwise `~/.config/screenrecon/config.json`. On macOS and Linux the file
and its directory are created owner-only (`0600` / `0700`).

The system prompt is a single string — `prompt` — set once via `--configure` or `--prompt` (a numbered picker with a shipped shortlist plus type-any-text), or supplied ad-hoc for one run via `--prompt "..."`.

```json
{
  "region": { "left": 100, "top": 100, "width": 600, "height": 400 },
  "provider": "anthropic",
  "model": "claude-haiku-4-5",
  "api_key": "sk-ant-...",
  "base_url": "",
  "telegram_bot_token": "123456:ABC-...",
  "telegram_chat_id": "123456789",
  "save_dir": "~/ScreenRecon",
  "prompt": "Describe what is in this screenshot. Be concise and lead with the key information.",
  "dwell_seconds": 3
}
```

| Field | Notes |
| --- | --- |
| `region` | Screen rectangle. `width`/`height` must be positive; `left`/`top` may be negative for monitors positioned left of or above the primary display. |
| `monitor` | Optional, written by `--configure` / `--screen`: `{"index": N, "of": M}` records which monitor the region was picked on so the watch banner can show it verbatim. Regenerated whenever the region is re-picked; delete it to force a live recompute. |
| `dwell_seconds` | How long the mouse must stay inside before firing. Fractional values are allowed. |
| `provider` | `anthropic` / `openai` / `google` / `openai_compatible`. Empty means "infer from the model name prefix" (`claude-*` → Anthropic, `gpt-*` / `o*` → OpenAI, `gemini-*` → Google). The compat path requires the field to be set explicitly, together with `base_url`. |
| `model` | Any vision-capable model ID for the chosen provider. Defaults to `claude-haiku-4-5`; the wizard offers curated shortlists per provider (`claude-opus-5`, `gpt-5`, `gemini-2.5-pro`, `deepseek-vl2`, …) and accepts any typed custom ID. |
| `api_key` | The key for the chosen provider. Rewritten by `--key`; the wizard prompts for it as part of setup. Legacy 0.1.5 configs may still carry `anthropic_api_key` — it is read as a fallback and migrated on the next save. |
| `base_url` | Only used when `provider` is `openai_compatible`. Set to the Chat-Completions-compatible endpoint (DeepSeek / Moonshot / Doubao presets are pre-filled by the wizard). |
| `prompt` | The system prompt sent with every capture. Any string; use `--prompt "..."` for a one-off override without changing this. Pre-SR-36 configs may still carry a `prompts` dict — it is silently dropped on the next save. |
| `save_dir` | `~` is expanded and the directory is created if missing. |

No environment-variable overrides in 0.1.6+: the config file is the single source of truth for credentials. Users upgrading from 0.1.5 who relied on `ANTHROPIC_API_KEY` should run `screenrecon --key` once to move the value into the file.

### Providers and required extras

Optional dependencies keep the install lean — you only pull in the SDKs you actually use.

| Provider | Install | Example models |
| --- | --- | --- |
| Anthropic (default) | `pip install screenrecon` | `claude-haiku-4-5`, `claude-opus-5` |
| OpenAI | `pip install 'screenrecon[openai]'` | `gpt-5`, `gpt-5-mini` |
| Google Gemini | `pip install 'screenrecon[google]'` | `gemini-2.5-pro`, `gemini-2.5-flash` |
| OpenAI-compatible (DeepSeek / Kimi / Doubao / custom) | `pip install 'screenrecon[openai]'` | `deepseek-vl2`, `moonshot-v1-8k-vision-preview`, `doubao-1-5-vision-pro-32k-...` |
| Everything | `pip install 'screenrecon[all]'` | any of the above |

If you pick a provider whose SDK is not installed, the tool prints a `pip install 'screenrecon[...]'` command and exits without touching the network.

### Getting an API key

**Anthropic (Claude).**
1. Sign up or log in at [console.anthropic.com](https://console.anthropic.com/).
2. Go to **Settings → API Keys → Create Key**, give it a name like `screenrecon`, and copy the key that appears (it starts with `sk-ant-...`). Anthropic will only show the full key once.
3. Add billing credit under **Settings → Billing** — vision calls need a paid balance, the free tier is not enough for continuous use.

**OpenAI (GPT).** [platform.openai.com](https://platform.openai.com/) → **API keys → Create new secret key** (starts with `sk-...`). Add billing credit under **Settings → Billing**.

**Google (Gemini).** [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) → **Create API key** (starts with `AIza...`). Free-tier quotas cover moderate desktop use; check the current limits before scripting.

**DeepSeek / Moonshot (Kimi) / Doubao.** Each provider's own console (`platform.deepseek.com`, `platform.moonshot.cn`, `console.volcengine.com/ark`). Prepay or bind billing per that provider's flow. Copy the OpenAI-compatible key; `screenrecon --configure` fills in the matching `base_url` when you pick the preset.

Create a dedicated key per tool rather than reusing an existing one: you can then revoke or replace it independently, and account-level usage reports make it easy to see what ScreenRecon cost you.

### Getting a Telegram bot token and chat ID

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token it gives you.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id`.

`screenrecon --configure` sends a test message at the end, so you will know immediately
whether both values are right.

## Output

Each trigger writes two files into `save_dir`:

```
20260812_143052.jpg    the captured region (JPEG, quality 90)
20260812_143052.txt    the recognised text (UTF-8)
```

The same text is printed to the terminal and sent to Telegram. If the answer is longer
than Telegram's 1024-character caption limit, the photo carries a truncated caption and
the full text follows as a separate message.

The four outputs are independent: a Telegram outage does not stop the local archive, and
a full disk does not stop the Telegram push.

## How coordinates work

The setup picker and the watch loop read the screen through the same API as the
capture, so the coordinates the picker returns are exactly what the watcher acts on.

On Windows, the process declares per-monitor DPI awareness before the first
capture, so the picker reports **physical** pixels on a scaled display. One
consequence worth knowing: coordinates are physical, so a region recorded at
150% scaling will point somewhere else if you later change the display scaling.
Re-run `screenrecon --configure` after changing scaling.

A region that falls outside the screen is not an error for the capture backend — it
returns black. ScreenRecon checks the region against the desktop at startup and warns if
it is off-screen or clipped.

Captures with a long edge over 2576 px are downscaled before being sent, because the
vision models resize anything larger anyway. The local archive keeps the full-resolution
image; only the uploaded copy is downscaled.

On a Retina Mac the capture comes back at logical (1×) resolution rather than the native
2×, because the capture backend requests nominal resolution. Coordinates are unaffected,
but very small text is captured at half the detail an equivalent non-Retina display would
give. Enlarging the region or the source text is the practical workaround.

## macOS screen recording permission

The first capture on macOS requires permission:

**System Settings → Privacy & Security → Screen Recording →** enable your terminal
(Terminal, iTerm, VS Code, …), then **quit the terminal completely and reopen it**.

Without it macOS does not fail the capture — it silently returns the desktop wallpaper
and menu bar with your windows removed, so the AI would confidently describe your
wallpaper. ScreenRecon asks the system for the permission state at startup and prints
this reminder rather than trying to guess from the pixels.

## Security and privacy

- **Bring your own key.** Credentials live only in your config file, and no credential
  ever appears in terminal output, logs or tracebacks — only the first 8 characters are
  shown. Third-party error text is scrubbed before printing, because HTTP libraries embed
  the request URL (which contains the Telegram bot token) in their exceptions. The setup
  wizard reads credentials without echoing them.
- **Create a dedicated API key per provider** for this tool so you can revoke it and
  account for its usage independently of the rest of your workload.
- **Screenshots may contain sensitive information.** They are sent to whichever AI
  provider you configured and to your own Telegram chat, and stored in `save_dir` —
  unless `ANTHROPIC_BASE_URL` (Anthropic), `HTTPS_PROXY`, or an explicit
  `base_url` for the OpenAI-compatible provider redirects them elsewhere. Managing
  the archive directory is up to you.
- **Only use config files you wrote.** `--config` accepts any path, and a config file
  carries the credentials that decide which Telegram chat and which API account your
  captures go to. The watch banner prints the masked chat ID and the resolved archive
  directory so a substituted config is visible.
- On macOS and Linux the config file, the archive directory and every capture are created
  owner-only. See [SECURITY.md](SECURITY.md).

## Cost and latency

The default model is `claude-haiku-4-5` — fast and cheap, plenty for OCR and
short descriptions. For higher accuracy on complex scenes, set
`"model": "claude-opus-5"` (with effort `low` under the hood, so a lookup is
still fast). Models that do not accept the effort parameter are detected
automatically and the parameter is dropped.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

The test suite covers the trigger state machine (enter → dwell → fire → no repeat →
leave → re-arm), config loading and validation, the setup wizard, CLI routing and exit
codes, the local archive and its permissions, cursor-backend selection and Wayland
detection, capture conversion and region checks, the Telegram caption split, the AI
error translation table, and the independence of the four outputs of one trigger. None of
it touches the network or the screen.

Before publishing, add a `[project.urls]` section to `pyproject.toml` pointing at the
real repository.

## Not in v1

Watching several regions at once, content-change triggers, a hosted backend, and
standalone `.exe`/`.app` bundles.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
