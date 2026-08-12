# ScreenRecon — Software Design Document

| Item | Value |
|---|---|
| Project | ScreenRecon |
| Document version | 0.1.0 |
| Date | 2026-08-13 |
| Status | Implemented |
| Audience | Developers |

## Revision history

| Version | Date | Language | Changes |
|---|---|---|---|
| v1.0 draft | 2026-08-12 | Chinese | Initial draft, written before implementation. Not part of this versioned series. |
| **0.1.0** | 2026-08-13 | English | First released design document. Translated to English and reconciled with the shipped implementation: licence changed to Apache 2.0, user-facing language changed to English, default model updated, and the design corrected wherever the draft's assumptions turned out to be wrong. Every deviation is listed in [§11](#11-changes-from-the-v10-draft). |

---

## 1. Overview

### 1.1 Background and goals

ScreenRecon is a cross-platform desktop tool. The user designates a rectangular
region of the screen; when **the mouse enters that region and dwells there for a
configured number of seconds**, the tool captures the region, sends the image to
the AI for recognition or question-answering, prints the result in the
terminal, pushes the result together with the screenshot to the user's Telegram,
and files both away in a local directory.

Positioning: an open-source, `pip install`-able command line tool. The user
brings their own API key (BYOK). The product ships no hosted backend of any
kind.

### 1.2 Target platforms

- Windows 10/11
- macOS 12+ (Intel and Apple Silicon)
- Linux X11 (best effort; not required for acceptance)

Wayland is explicitly unsupported — see [§5.2](#52-cursor-position-platformpy).

### 1.3 Deliverables

1. A PyPI package `screenrecon` providing a `screenrecon` command
2. An open-source repository (GitHub) with user documentation in the README
3. All functionality described in this document

---

## 2. Terminology

| Term | Meaning |
|---|---|
| Region | A user-defined screen rectangle: `{left, top, width, height}` |
| Dwell | The continuous time the cursor remains inside the region |
| Armed | The state in which the trigger can fire. Firing disarms it; the trigger re-arms once the cursor leaves the region |
| BYOK | Bring Your Own Key — the user configures their own Anthropic API key |

> **Coordinate space.** v1.0 specified "logical pixels". That turned out to be
> the wrong commitment — see [§5.2](#52-cursor-position-platformpy). The
> guarantee that matters is *self-consistency*: the cursor and the capture are
> read through the same coordinate space, so the numbers `--show-cursor` reports
> are exactly the numbers to put in `region`. On Windows those numbers are
> physical pixels.

---

## 3. Requirements

### 3.1 Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-1 | The user can configure the watched region (left/top/width/height) | P0 |
| FR-2 | The user can configure an Anthropic API key | P0 |
| FR-3 | The user can configure a Telegram bot token and chat ID (both mandatory) | P0 |
| FR-4 | The user can configure the local archive directory | P0 |
| FR-5 | A capture fires automatically after the cursor dwells in the region for N seconds (default 3, configurable) | P0 |
| FR-6 | The capture is sent to the AI for recognition, with a user-defined prompt | P0 |
| FR-7 | The result is printed in the terminal | P0 |
| FR-8 | The screenshot and the recognised text are both sent to Telegram | P0 |
| FR-9 | The screenshot (PNG) and the text (TXT) are archived under timestamped names | P0 |
| FR-10 | After firing, the cursor must leave the region before the trigger can fire again | P0 |
| FR-11 | An interactive setup wizard (`--configure`) verifies both sets of credentials online | P0 |
| FR-12 | A `--show-cursor` helper prints live cursor coordinates to help pick a region | P1 |
| FR-13 | Prompt presets: a built-in default plus user-defined named prompts, selected with `--mode <name>` | P1 |
| FR-14 | An `ask` subcommand captures once and answers questions about that screenshot | P2 |
| FR-15 | The `ANTHROPIC_API_KEY` environment variable overrides the key in the config file | P2 |

All fifteen are implemented.

### 3.2 Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | Cursor polling period ≤ 200 ms; idle CPU usage < 2% |
| NFR-2 | No network or API error may crash the process; error messages must be understandable, in English |
| NFR-3 | Credentials must never appear in logs, terminal output (the wizard shows the first 8 characters as a mask), or exception text |
| NFR-4 | The config file is owner-only (mode 0600) on POSIX platforms |
| NFR-5 | Local overhead of one trigger, from capture to printed result excluding API latency, is under 500 ms |
| NFR-6 | Code passes `ruff`; core logic (state machine, config loading) has unit tests |

> NFR-2 in v1.0 required Chinese messages. The product language is English;
> see [§11](#11-changes-from-v10).

### 3.3 Explicitly out of scope for v1

- A graphical interface or drag-to-select region picker (`--show-cursor` plus
  manual entry replaces it)
- Content-change triggering (a superseded design, replaced by dwell triggering)
- Watching multiple regions simultaneously
- A hosted backend or user accounts
- Standalone `.exe` / `.app` bundles (deferred to v2, subject to user feedback)

---

## 4. Architecture

### 4.1 Module layout

```
screenrecon/
├── pyproject.toml
├── README.md
├── SECURITY.md
├── LICENSE                  # Apache 2.0
├── NOTICE
├── docs/                    # this document
└── src/screenrecon/
    ├── __init__.py          # version
    ├── cli.py               # argument parsing, subcommand routing
    ├── config.py            # config load/save/validate/wizard
    ├── platform.py          # cross-platform cursor position
    ├── capture.py           # mss region capture
    ├── vision.py            # AI calls and error translation
    ├── notify.py            # Telegram delivery
    ├── storage.py           # local archive and private file writes
    ├── watcher.py           # main loop and trigger state machine
    └── ui.py                # terminal output and credential masking
```

Dependencies: `mss`, `pillow`, `anthropic`, `requests`; plus
`pyobjc-framework-Quartz` on macOS and `python-xlib` on Linux.

`ui.py` is an addition to the v1.0 layout. It is the single choke point through
which all terminal output passes, which is what makes NFR-3 auditable rather
than a property that has to be re-checked at every print site.

> `platform.py` shadows the standard library module of the same name. Installed
> code is unaffected because all imports are absolute, but running a file from
> inside the package directory can break dependencies that import stdlib
> `platform`. The name is kept for continuity with v1.0.

### 4.2 Data flow

```
cursor poll ──> trigger state machine ──> capture.grab(region) ──┬──> storage.save_png()
                                                                 └──> vision.ask(img, prompt)
                                                                           │
                                                     terminal <────────────┤
                                                     storage.save_txt() <──┤
                                                     notify.send(png, text)
```

The output steps are independent: a Telegram failure does not affect the local
archive, and a full disk does not affect the Telegram push. This property has
dedicated test coverage, because it is the reliability claim the README leads
with.

---

## 5. Detailed design

### 5.1 Trigger state machine (`watcher.py`) — core logic

State:

- `entered_at: float | None` — the monotonic time the cursor entered the region
- `armed: bool` — whether the trigger may fire

Polling period `POLL_INTERVAL = 0.1 s`. Each cycle:

```
inside = cursor is within the region (half-open: left <= x < left + width)

if not inside:
    entered_at = None; armed = True          # leaving re-arms
elif armed:
    if entered_at is None:
        entered_at = monotonic()             # record entry
    elif monotonic() - entered_at >= dwell:
        armed = False                        # disarm before handling, so
                                             # handling cannot re-trigger
        run: capture -> archive -> AI -> print -> save text -> Telegram
```

Key points:

1. **`time.monotonic()` is mandatory.** `time.time()` would fire spuriously
   whenever the system clock jumps.
2. Handling the trigger synchronously is acceptable (`armed` is False
   throughout). If it is ever made asynchronous, exactly one request must be in
   flight at a time.
3. The capture's pixel dimensions are **not** authoritative and must never be
   written back onto `region`.
4. **Dwell is continuous observed time.** When the cursor cannot be read at all
   — a locked screen, a UAC prompt — `entered_at` is cleared. Continuity across
   a period that was not observed cannot be claimed, and without this a cursor
   that happened to be inside the region when the screen locked would fire the
   instant the screen was unlocked, capturing and uploading a screen the user
   had only just revealed.
5. The measured dwell begins one poll after entry, so the effective dwell is
   `dwell_seconds + POLL_INTERVAL`. This is immaterial at the default of 3 s and
   proportionally significant below about 0.3 s.

### 5.2 Cursor position (`platform.py`)

| Platform | Implementation |
|---|---|
| Windows | `ctypes` `GetCursorPos` |
| macOS | `Quartz.CGEventGetLocation(CGEventCreate(None))` |
| Linux | `python-xlib` `query_pointer` (X11 only) |

A single `get_cursor_pos() -> tuple[int, int]` is exposed. The implementation is
bound lazily on first use, so `--help` works even when an optional platform
dependency is missing.

**Two failure kinds are distinguished**, and the distinction is load-bearing:

- `CursorError` — this machine cannot support the tool (unsupported platform,
  missing dependency, no X display). Fatal at startup.
- `CursorUnavailable` — the cursor cannot be read *right now*. On Windows this
  occurs whenever the process is not attached to the input desktop: the
  workstation is locked, a UAC prompt is showing, or an RDP session is
  disconnected. A watcher intended to run all day waits these out indefinitely
  rather than exiting.

**Coordinate space.** mss marks the process DPI-aware on Windows, after which
`GetCursorPos` reports physical pixels. `platform.py` therefore declares
per-monitor DPI awareness itself, before mss can be imported, so every entry
point agrees on what a pixel is — including `ask`, which never reads the cursor.
Consequences the user needs to know: coordinates are physical, so a region
recorded at 150% scaling points elsewhere if the scaling changes, and
coordinates read from a non-DPI-aware tool will not match.

**Wayland is refused**, including when XWayland is present. Testing for
`WAYLAND_DISPLAY and not DISPLAY` is insufficient: essentially every Wayland
desktop also runs XWayland and sets `DISPLAY`, and taking the X11 path there is
worse than failing — `XQueryPointer` returns stale coordinates whenever the
pointer is over a native Wayland window, and the capture contains only X11
clients, so the tool appears to work and never does. `SCREENRECON_FORCE_X11=1`
overrides the check for an all-X11 application stack.

### 5.3 Capture (`capture.py`)

- Captures the region with `mss` and converts to a `PIL.Image` (RGB).
- A fresh instance is created per capture inside a `with` block: mss instances
  are thread-bound on macOS. `mss.MSS` is preferred over the deprecated
  `mss.mss`, which is removed in mss 11.
- **Region validation.** A region outside the desktop is not an error for mss —
  it returns black padding. The region is therefore checked against the virtual
  desktop at startup and the user warned if it is off-screen or clipped, because
  otherwise a mistyped coordinate means every trigger silently uploads a black
  image indefinitely.
- **macOS permission.** Since macOS 10.15 an unauthorised capture does not fail
  or return black — it returns the desktop wallpaper and menu bar with the
  user's windows removed. The permission state therefore cannot be inferred from
  the pixels and is queried directly via `CGPreflightScreenCaptureAccess`. A
  flat-colour capture remains a secondary hint only.
- **Retina.** The capture backend requests nominal resolution, so on a Retina
  display the image comes back at 1× logical resolution, not 2×. Coordinates are
  unaffected. A one-shot startup warning tells the user, since the person this
  affects is the one wondering why small text is misread.
- **Oversized captures.** Anything with a long edge above 2576 px is downscaled
  with LANCZOS before upload, because the vision models resize larger images
  anyway. The local archive keeps the full-resolution image.

### 5.4 AI call (`vision.py`)

- SDK: the official `anthropic` Python SDK, floor `>=0.104` (the first version
  whose `messages.create` accepts `output_config`).
- Model: config field `model`, default `claude-opus-5`. `claude-haiku-4-5` is
  documented as the cheaper, faster alternative.
- Request: a single user message whose content is
  `[image(base64 PNG), text(prompt)]`, with `output_config={"effort": "low"}`.
  Reading a screenshot is a light task, and low effort keeps latency and cost
  down. A model that rejects the parameter is detected once and downgraded
  automatically, covering both the API's 400 and the `TypeError` an older SDK
  raises.
- `max_tokens: 4096`. This cap covers thinking *plus* the visible answer on
  current models, so a tighter budget can be spent entirely on thinking and
  return no text at all.
- Response: the concatenation of every `type == "text"` content block.

Error translation (NFR-2):

| SDK exception | Message |
|---|---|
| `AuthenticationError` | The API key is invalid or revoked. Run `screenrecon --configure` to set it again. |
| `PermissionDeniedError` | This API key is not allowed to access that resource. |
| `RateLimitError` | Too many requests. Please try again shortly. |
| `NotFoundError` | Model or endpoint not found. Check the `model` field in your config. |
| `APIStatusError` | The AI API returned `{code}`. Check your account credit or retry later. |
| `APITimeoutError` | The AI API request timed out. |
| `APIConnectionError` | Network connection failed. |

Empty responses are also classified, by `stop_reason`, into a refusal, an
output-limit truncation, or an unexplained empty answer. **Every failure is
returned as a message rather than raised**, so the main loop continues.

### 5.5 Telegram delivery (`notify.py`)

- Endpoint: Bot API `sendPhoto` (multipart PNG upload, `caption` carrying the
  text). 30 s timeout.
- **Caption limit 1024.** When the answer is longer, the caption is truncated
  and the full text follows via `sendMessage`. The truncation point is derived
  from the limit minus the marker length, so the two can never sum past the
  limit — v1.0's "first 1000 characters plus a marker" exceeds 1024.
- **Limits are counted in UTF-16 code units**, which is what Telegram counts.
  Characters outside the BMP (emoji, rarer CJK ideographs) count as two, so
  counting Python characters would let a valid-looking caption be rejected.
- Text longer than 4096 units is split across several messages.
- The photo and the text fail independently — an oversized image is rejected
  while the text is fine — so a photo failure still delivers the answer as text.
- Failures print a warning only: no retries, no interruption of the main loop.
  A retry queue is deferred to v2.
- The wizard validates the token with `getMe` and the chat ID by sending one
  test message, reporting the bot's `@username`.
- **Everything printed from a third party is sanitised first.** `requests`
  embeds the request URL in its exceptions, and that URL contains the bot token,
  including percent-encoded.

### 5.6 Local archive (`storage.py`)

- Filenames: `YYYYMMDD_HHMMSS.png` and a matching `.txt` (UTF-8). A numeric
  suffix resolves same-second collisions, which `ask` can produce.
- The directory is created if missing. Paths are normalised with
  `expandvars` → `strip` → `expanduser` → `resolve`: Windows silently drops a
  trailing space when creating a directory but keeps it in the path, and
  `%APPDATA%` is what a Windows user naturally types.
- Write failures print a warning and never interrupt the loop.
- The `.txt` is written only when the `.png` was written, so the archive never
  contains an orphan transcript.
- The archive directory and its files are owner-only on POSIX. Captures can
  contain anything that was on screen, and the transcript is a greppable
  plaintext rendering of it.

### 5.7 Configuration (`config.py`)

Path: `$XDG_CONFIG_HOME/screenrecon/config.json` when that variable is set,
otherwise `~/.config/screenrecon/config.json`.

```json
{
  "region": {"left": 100, "top": 100, "width": 600, "height": 400},
  "anthropic_api_key": "sk-ant-...",
  "telegram_bot_token": "123456:ABC-...",
  "telegram_chat_id": "123456789",
  "save_dir": "~/ScreenRecon",
  "prompt": "Describe what is in this screenshot. Be concise and lead with the key information.",
  "prompts": {
    "log": "Find the error messages in this screenshot and explain the likely cause."
  },
  "dwell_seconds": 3,
  "model": "claude-opus-5"
}
```

Rules:

- Merged with defaults on load; `region` and `prompts` merge one level deep.
- Startup is refused if any of the three credentials is empty, pointing the user
  at `--configure`.
- `ANTHROPIC_API_KEY` takes precedence over the file (FR-15).
- Validation: `dwell_seconds > 0`; `region.width`/`height` positive integers;
  errors name the specific field. **`left` and `top` may be negative** —
  monitors positioned left of or above the primary display have negative
  coordinates, which v1.0's "four positive integers" would have rejected.
- **The file is written atomically at mode 0600** via a temporary file in the
  same directory followed by a rename, so the credentials are never briefly
  readable at the process umask, a crash cannot leave a torn config, and a
  symlink planted at the destination is replaced rather than followed. The
  rename is retried briefly: on Windows a virus scanner or search indexer
  holding a read handle causes a transient failure that would otherwise discard
  a whole wizard session.
- Directories are only tightened when this tool creates them. The parent of a
  `--config` path can be `$HOME` or `/etc`, and silently tightening those would
  break everything else that reads them.

**Setup wizard (`--configure`)**: asks for each field, Enter keeps the current
value, then verifies both sets of credentials online. Verification failure still
saves, with a warning. Credentials are read with `getpass`, so the typed value
never reaches the terminal, shell history, or a screenshot ScreenRecon itself
takes. No input loop can run unbounded: closed stdin aborts cleanly, and
repeated invalid answers give up.

### 5.8 Command line interface (`cli.py`)

```
screenrecon                     read config, enter the watch loop
screenrecon --configure         interactive setup wizard
screenrecon --show-cursor       print live cursor coordinates
screenrecon --mode <name>       use prompts.<name> as this run's prompt
screenrecon ask [question]      capture once and ask about it
screenrecon --config <path>     use an alternative config file
screenrecon --version           print the version
```

Global flags precede the subcommand. `--mode` is resolved on every path that
reaches the API, so an unknown preset is always an error rather than a silently
ignored flag; with `ask` and no question, the preset *is* the question.

Exit codes: 0 success, 1 error (including a failed `ask`), 2 usage error, 130
interrupted. A final handler catches anything unexpected and prints a scrubbed
one-line message: a traceback rendered with frame locals — as `rich`, Sentry and
`pytest --showlocals` do — would otherwise expose the credentials held in the
call stack.

---

## 6. Security design

1. **BYOK.** The key exists only on the user's machine. No real credential
   appears in the code or repository; examples use placeholders.
2. The config file lives under the user's home directory, is not distributed
   with the code, and is mode 0600 on POSIX.
3. Terminal and log output mask credentials to their first 8 characters.
   Third-party text is scrubbed of the verbatim and percent-encoded token and of
   the chat ID before printing.
4. The README advises creating a dedicated Anthropic key for this tool, so it
   can be revoked and accounted for independently.
5. Screenshots may contain sensitive information. The README documents the data
   flow — the Anthropic API and the user's own Telegram, and nothing else — and
   states that the archive directory is the user's to manage.
6. `ANTHROPIC_BASE_URL` redirects both the key and the captures; it is warned
   about at startup rather than silently honoured.
7. A config file is as trusted as the code, since it decides which Telegram chat
   and which API account receive the captures. The watch banner and the `ask`
   command both print the masked chat ID and the resolved archive directory, so
   a substituted config cannot hide where the data goes.

Full detail, including the reporting process, is in `SECURITY.md`.

---

## 7. Packaging and release

- `pyproject.toml` with hatchling;
  `[project.scripts] screenrecon = "screenrecon.cli:main"`
- Conditional dependencies:
  ```
  pyobjc-framework-Quartz; sys_platform == "darwin"
  python-xlib; sys_platform == "linux"
  ```
- Python `>=3.10`
- Licence: **Apache 2.0** (v1.0 specified MIT). `LICENSE` and `NOTICE` both ship
  in the distribution, as Apache 2.0 §4(d) requires.
- Release: GitHub Release plus PyPI. The package name is subject to
  availability; if `screenrecon` is taken, fall back to `screen-recon` and
  rename the command to match.
- `[project.urls]` must point at the real repository before publishing.

---

## 8. Testing and acceptance

### 8.1 Unit tests (pytest)

Required by NFR-6, and all present:

- **State machine**: the full sequence — enter, dwell, fire, remain parked
  without re-firing, leave, re-arm — with a controlled monotonic clock and
  scripted cursor positions
- **Config**: missing fields, invalid values, environment override, masking
- **Telegram caption splitting**
- **AI error translation**, using real SDK exception instances

Added beyond the v1.0 floor, because these are where the defects actually were:
the setup wizard, CLI routing and exit codes, the archive and its permissions,
cursor backend selection and Wayland detection, capture conversion and region
checks, the watch loop's behaviour across an unreadable-cursor gap, and the
independence of the four outputs of one trigger.

No test touches the network or the screen.

### 8.2 Manual acceptance checklist

| Scenario | Expected |
|---|---|
| One full run on Windows and on macOS | All four outputs correct: capture, terminal, Telegram, archive |
| Cursor parked in the region for 60 s | Fires exactly once |
| Move out, move back in, dwell | Fires again |
| Trigger while offline | Prints a network error, process survives, recovers when the network returns |
| Trigger with an invalid API key | Prints a key-invalid message, process survives |
| Answer longer than 1024 characters | Telegram receives the photo with a truncated caption, then the full text |
| macOS without screen recording permission | A clear message directs the user to grant it |
| Retina display | Captured content matches the region (at 1× logical resolution) |
| Lock the screen, then unlock | The watcher survives and does **not** fire on unlock |
| Region configured off-screen | Warned at startup rather than silently uploading black images |

### 8.3 Reference implementation

v1.0 cited a single-file `screenrecon.py` as a starting point. It is superseded
by the module layout in [§4.1](#41-module-layout); this document is
authoritative.

---

## 9. Milestones

| Phase | Content | Estimate |
|---|---|---|
| M1 | Module split, state machine, capture, AI call; running on Windows and macOS | 2 days |
| M2 | Telegram, archive, setup wizard, error handling | 1.5 days |
| M3 | Unit tests, README, pyproject, PyPI release | 1.5 days |

---

## 10. Open questions

| # | Question | Status |
|---|---|---|
| 1 | Final PyPI and GitHub name (`screenrecon` availability) | **Open** — check before publishing |
| 2 | Does the `ask` subcommand (FR-14) make v1? | **Resolved** — yes, shipped |
| 3 | Does Telegram delivery need retries or an offline queue? | **Open** — v1 warns only; deferred to v2 |
| 4 | Should a trigger produce a sound or system notification? | **Open** |
| 5 | Is `claude-opus-5` the right default, given it costs roughly 1.7× `claude-sonnet-4-6` per call for a BYOK user? | **Open** — needs product sign-off |

---

## 11. Changes from the v1.0 draft

Every point where the implementation departs from the original Chinese draft,
and why.

### Directed by the product owner

| Area | v1.0 | 0.1.0 |
|---|---|---|
| Licence | MIT | Apache 2.0 |
| User-facing language (NFR-2) | Chinese | English |

### Corrections — v1.0 was wrong

| Area | v1.0 | 0.1.0 | Reason |
|---|---|---|---|
| Caption truncation (§5.5) | First 1000 characters plus a marker | Limit minus marker length | 1000 + marker exceeds Telegram's 1024 limit |
| Caption/message units (§5.5) | Characters | UTF-16 code units | Telegram counts UTF-16; an emoji answer would be rejected |
| `region.left` / `top` (§5.7) | Positive integers | Any integer | Monitors left of or above the primary have negative coordinates |
| `max_tokens` (§5.4) | 1024 | 4096 | The cap covers thinking plus text; 1024 can return no text at all |
| Coordinate space (§2, §5.1) | Logical pixels | Physical on Windows, self-consistent everywhere | mss makes the process DPI-aware; a logical-pixel cursor would not match the capture |
| Retina (§5.3, §8.2) | Capture is 2× the logical size | Capture is 1× | The capture backend requests nominal resolution |
| Cursor binding (§5.2) | Bound at import | Bound lazily | So `--help` works without an optional platform dependency |
| Wayland (§5.2) | Detected and reported | Refused, XWayland included | The v1.0 check passes under XWayland, where the tool silently misbehaves |

### Additions — gaps v1.0 did not anticipate

| Area | Addition |
|---|---|
| §4.1 | `ui.py`, the single choke point for output and credential masking |
| §5.1 | Dwell continuity is broken by an unobservable gap, so unlocking the screen does not fire |
| §5.2 | `CursorError` / `CursorUnavailable` split, so a locked screen does not end the session |
| §5.3 | Region validation, macOS permission preflight, oversized-capture downscaling, Retina notice |
| §5.4 | `effort` parameter with automatic downgrade; refusal and truncation classification |
| §5.5 | Message chunking above 4096 units; independent photo and text delivery; token sanitisation |
| §5.6 | Path normalisation; no orphan transcripts; owner-only archive |
| §5.7 | `XDG_CONFIG_HOME`; atomic 0600 write; `getpass`; bounded input loops |
| §5.8 | `--version`; defined exit codes; scrubbed last-resort error handler |
| §6 | `ANTHROPIC_BASE_URL` warning; destination disclosure; `SECURITY.md` |
| §8.1 | Test coverage well beyond the v1.0 floor |
