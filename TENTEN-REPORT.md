# MoneyPrinterTurbo — App Tenten report

Date: 2026-08-17. Hardware: Fedora laptop, loopback-only FastAPI + Streamlit, chromeless Chrome `--app` desktop window. Evidence harness: Streamlit at `127.0.0.1:8501` (browser-qa / Playwright) plus the live FastAPI binary at `127.0.0.1:8080`. This is a Python desktop app, not Tauri — `cargo tauri build` / clippy / cargo-audit are N/A.

## Screen set

- Advanced WebUI (live config: `simple_mode = false`): 4-column Video Script / Video Settings / Audio Settings / Subtitle Settings, plus Generate, Task Manager, Settings, Best Bet.
- Simple mode (code path in `webui/Main.py`, not the live process): topic + Generate only.
- Loading: Streamlit spinner on Best Bet / Generate.
- Error: Streamlit `st.error` / log panel; backend-unreachable is Streamlit's own disconnect banner.
- Empty: Task Manager "No tasks yet".
- Unreached this run: simple-mode layout (config drift — live process was started with simple mode off).

## Thresholds (this machine)

- Cold start of `uv run python main.py` to `/docs` 200: ~8s (measured 2026-08-17).
- One 1080p 8s Imagine clip: ~70s.
- Full 65s 9:16 render of 7 local 1080p clips: several minutes of MoviePy preprocess (progress stays at 50 while `temp-clip-N.mp4` writes).

## Gates

| # | Gate | Score | Notes |
|---|------|------:|-------|
| 0 | Baseline | 8 | No Tauri release binary. FastAPI + Streamlit start clean. Before-screenshots at 1000×700 / 1280×800 / 1920×1080 in `storage/tenten-evidence/`. |
| 1 | Truth | 8 | Best-bet copy stays "best bet with receipts". Moonshot "Recommended / Exclusive offer" affiliate triad rewritten as an explicit affiliate note. |
| 2 | Build health | 9 | `611 passed, 11 skipped` before later auth/copy edits; targeted suites after those edits green. `ruff check app/ webui/ test/` clean. Unused `json` import in `test_imagine_source.py` removed. |
| 3 | Data integrity | 8 | `script.json` atomic write + `0600`. In-memory state (`enable_redis = false`). `config.toml` now chmod 0600 on save. No SQLite. |
| 4 | Design | 6 | 1280 / 1920 layouts clean. At 1000×700 the 2×2 wrap hides Audio / Subtitle / Generate below the fold with no scroll cue. Tour popover desyncs on `stMain` scroll. Dark-mode tour card themed in `styles.css`. |
| 5 | A11y | 7 | axe `--strict`: 0 violations / 23 passes. Native Streamlit focus ring is fine. Tour buttons had no `:focus-visible` — added. Tour still does not trap Tab (P2, `streamlit_tour` limit). Screen-reader not claimable. |
| 6 | Performance | 7 | Cold start ~8s. Combine of native 1080p clips is the bottleneck; expected, not a dep leak. |
| 7 | Copy | 8 | Simple Mode + Video Source help added. Speech Region full-width comma fixed. Affiliate honesty pass. Best-bet framing left intact. |
| 8 | Security | 8 | CORS wildcard removed. `verify_token` on when `app.api_key` is set (empty still allows local WebUI). `/tasks` `follow_symlink=False`. Upload 512MB cap. `save_video` streamed + 500MB cap. ffmpeg timeouts on concat and silent audio. Example `listen_host` is `127.0.0.1`. Live check: unauth POST → 401, hostile Origin → no ACAO. |
| 9 | Ship-shape | 7 | Desktop launcher `~/.local/bin/moneyprinter-app` + `.desktop` still present. User systemd manager still wedged (`Rs`, `systemctl --user` times out) — launcher uses the direct-launch fallback. Public MIT fork is intentional; `config.toml` gitignored. Guard fingerprint/source-map N/A. |
| 10 | Final | 7 | End-to-end: trend picker → 8 scene prompts via grok → 7×1080p Imagine clips (one timed out) → local re-render with cleaned VO. See Production below. specialist-review not run as a separate Workflow this pass; security + copy + frontend-qa agents covered those lanes. |

**Overall: 7.5 / 10** — shippable for a local desktop factory. Not 10/10 while the tour still desyncs, 1000×700 hides Generate, and systemd --user stays wedged.

## Production video

**Deliverable: `storage/tasks/certfind-final/final-1.mp4`** — 65.5s, 1080×1920@30, h264+aac, loudnorm to -14 LUFS (measured mean -17.3 dB, max -1.4 dB vs -24.4/-6.3 raw). Caption + hashtags in `TIKTOK.txt` alongside.

Pipeline of record:
1. Trend picker (grok via cli_agent) picked the r/personalfinance rising post: old family stock certificate the bank called worthless. Framed as best bet, not guaranteed viral.
2. grok wrote the 188-word script; a leaked meta-preamble sentence was caught in the first VO and root-caused into `llm._strip_meta_preamble`.
3. 8 scene prompts (`llm.generate_scene_prompts`) → 7 native 1080p portrait Imagine clips (task `8d065cb4-*`), returned in scene order.
4. Frame QA caught two source-clip artifacts: a stacked-duplicate aerial and garbled AI lettering in the archive scene. Both regenerated with portrait-safe prompts (task `certfind-fix`) and swapped into `storage/local_videos/certfind-scene03/07.mp4`.
5. Final render headless via `tm.start(video_source=local)` — clips reused, nothing re-billed. BeVietnamPro-Bold 70px, stroke 3, custom position 70%, en-GB-RyanNeural at 1.05, random BGM at 0.2, sequential concat.

Interrupted attempts kept for provenance: `2fc4bae5-*` (render died with the session restart mid-combine; audio/subtitles clean).

## NEEDS-ALEX

- **Port 8080 is squatted** by an unrelated `go run main.go` binary (parent `/usr/bin/go run`, listening on `*:8080` — all interfaces). The MPT API can't bind until it exits. Check `ss -tlnp | grep 8080` and decide whether that go process should be running at all; it holds an open port on every interface.
- Re-exec or reboot the user systemd manager (`user@1000` busy-spinning) so `systemctl --user start moneyprinter.service` works again.
- Streamlit was restarted on the new code at 18:xx (Simple Mode / Video Source help + tour CSS live at `127.0.0.1:8501`). Nothing to do unless it's down.
- Confirm `app.api_key` in `config.toml` (set this session). The WebUI talks to the API in-process and does not send `x-api-key`; if you drive `/api/v1` from curl/another client, pass `x-api-key`.
- Optional: raise the Chrome `--app` min height above ~950px, or add a sticky Generate bar, so 1000×700 isn't a hidden-button trap.
- Optional: decide whether the onboarding tour should stay auto-start (it fights `stMain` scroll).

## Honest ceilings

- Preview/Streamlit evidence ≠ a packaged installer on a fresh profile.
- Linux only. Windows/macOS not run.
- axe + Tab ≠ a screen reader.
- 1080p Imagine is native; MoviePy still re-encodes clips to 1080×1920 @ 30fps for concat.
