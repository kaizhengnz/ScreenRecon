# Security Policy

## Reporting a vulnerability

Please report security issues privately rather than in a public issue. Open a
[GitHub security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, or email the maintainers. We aim to acknowledge within a
week.

Please include what you did, what happened, and what you expected. If a
credential was exposed, say which one — the API key, the Telegram bot token, or
the chat ID — so we can advise on rotation.

## What ScreenRecon handles

- **An AI provider API key** (Anthropic, OpenAI, Google Gemini, or an
  OpenAI-compatible endpoint like DeepSeek / Moonshot / Doubao) and a
  **Telegram bot token and chat ID**, stored in your config file.
- **Screenshots of a region of your screen**, which may contain anything that
  was on it.

## Design commitments

These are the properties we consider security bugs if broken:

1. **No credential in output.** Credentials never appear in terminal output,
   warnings, or error messages — only the first 8 characters, as a mask. Text
   originating from a third party (HTTP libraries embed the request URL, and the
   Telegram URL contains the bot token) is scrubbed before printing, including
   percent-encoded forms. The setup wizard reads credentials without echo.
2. **No credential in a crash.** Unexpected exceptions are caught and reported
   as a scrubbed one-line message, because a traceback rendered with frame
   locals — as `rich`, Sentry and `pytest --showlocals` do — would otherwise
   expose the key from the call stack.
3. **Owner-only files.** On macOS and Linux the config file, the archive
   directory and every capture are created with owner-only permissions
   (`0600` / `0700`). The config file is written atomically via a temporary
   file, so it is never briefly readable at the process umask and a symlink
   planted at its path is replaced rather than followed. Windows has no POSIX
   mode bits; there, files are protected only by the profile's ACLs.
4. **Bounded egress.** The only network destinations are the AI provider you
   configured (Anthropic, OpenAI, Google Gemini, or the `base_url` you set for
   an OpenAI-compatible endpoint) and `api.telegram.org`. There is no telemetry
   and no update check.

## Things to know

- **Endpoint overrides redirect your captures.** For the Anthropic provider,
  the SDK honours `ANTHROPIC_BASE_URL` — anything able to set an environment
  variable in your shell can point the API (and therefore your screenshots and
  key) at another host, and ScreenRecon prints a warning when it is set. The
  OpenAI-compatible provider takes its endpoint from the `base_url` config
  field, which is visible in `screenrecon --show`. `HTTPS_PROXY` and
  `REQUESTS_CA_BUNDLE` affect every provider.
- **A config file is as trusted as the code.** `screenrecon --config PATH` will
  use whatever credentials that file contains, so a config you did not write can
  route your captures to someone else's Telegram chat and someone else's API
  account. Only use config files you wrote. The watch banner prints the masked
  chat ID and the resolved archive directory so a substitution is visible.
- **The archive is not encrypted.** `save_dir` accumulates every capture; manage
  and prune it yourself.
- **Use a dedicated API key per provider.** Create a separate key for this tool
  so you can revoke it and account for its usage independently of the rest of
  your workload.

## Supported versions

Fixes are applied to the latest release.
