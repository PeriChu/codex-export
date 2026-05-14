# codex-export

Export local Codex sessions to clean HTML / Markdown / JSON / CSV bundles, with
the raw JSONL transcript plus readable input and output files packaged alongside.

Zero dependencies. Pure Python stdlib, 3.9+. One file, one command.

## Why

Codex records local threads as JSONL under `~/.codex/sessions/...` (or the
equivalent Codex home directory on Windows), while the human-readable thread
names live in `session_index.jsonl`. Reading the
raw records by hand is possible, but not pleasant: messages, reasoning records,
tool calls, tool outputs, and app events are interleaved.

This tool flattens a session into:

- `session.html` - formatted reading view with user-prompt TOC, Markdown
  rendering, syntax highlighting, collapsible tool activity, collapsible
  assistant commentary, and a light/dark theme toggle.
- `session.md` - GitHub-flavored Markdown reading export.
- `session.json` - structured per-block archival dump for another LLM or script.
- `session.csv` - flat archival per-block table for spreadsheets or ingestion;
  very large cells are capped with length/truncation columns, with full text in
  `session.json` and `transcript.jsonl`.
- `metadata.json` - extracted session metadata.
- `transcript.jsonl` - the lossless raw Codex source.
- `inputs/` and `outputs/` - readable files mentioned by the user, local image
  inputs, viewed/generated files, and files inferred from edit tool calls.
  Each folder includes `_manifest.json` so missing or unreadable references are
  still visible.

## Install

Recommended install uses `pipx`, so the `codex-export` command is available
globally without mixing dependencies into your normal Python environment.

### 1. Install pipx

Windows PowerShell:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

Close and reopen PowerShell after `ensurepath`.

macOS / Linux:

```bash
brew install pipx
pipx ensurepath
```

If you do not use Homebrew:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

### 2. Install codex-export

macOS / Linux:

```bash
pipx install "git+https://github.com/PeriChu/codex-export.git@macos"
```

Windows PowerShell:

```powershell
pipx install "git+https://github.com/PeriChu/codex-export.git@windows"
```

If GitHub cloning is unstable on Windows, download the Windows branch ZIP and
install from the extracted folder:

```powershell
Invoke-WebRequest `
  -Uri "https://github.com/PeriChu/codex-export/archive/refs/heads/windows.zip" `
  -OutFile "$env:TEMP\codex-export-windows.zip"

Expand-Archive "$env:TEMP\codex-export-windows.zip" -DestinationPath "$env:TEMP" -Force
pipx install "$env:TEMP\codex-export-windows"
```

Confirm the command is installed:

```bash
codex-export --help
```

Or run directly from a clone without installing:

```bash
python3 codex_export.py --help
```

## Usage

```bash
# list all local Codex sessions, newest first
codex-export list

# export the most recent session
codex-export export latest

# export by session-id prefix
codex-export export <session-id> --output ./exports

# export every session
codex-export export all --output ./exports

# choose formats
codex-export export latest --formats html,json

# transcript-only bundle, no copied input/output files
codex-export export latest --no-files

# include developer/system context in rendered outputs
codex-export export latest --include-context

# include readable reasoning summaries, which are hidden by default
codex-export export latest --include-reasoning

# point at a backup or custom Codex home
codex-export list --codex-home ~/.codex-backup
```

## Output Bundle Layout

Each exported session gets its own folder:

```text
exports/<session-id>/
|-- README.md
|-- session.html
|-- session.md
|-- session.json
|-- session.csv
|-- metadata.json
|-- transcript.jsonl
|-- inputs/
`-- outputs/
```

## Where Codex Data Lives

- macOS/Linux default: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Windows candidates: `%USERPROFILE%\.codex`, `%APPDATA%\Codex`,
  `%LOCALAPPDATA%\Codex`, and `%APPDATA%\OpenAI\Codex`
- Title index: `<codex-home>/session_index.jsonl`
- Optional shell snapshots and other app state may also exist under
  `~/.codex/`, but the JSONL transcript is the source of truth for this
  exporter.

## Notes And Limitations

- HTML/Markdown use a reading view: synthetic environment context and file-only
  wrappers are not displayed as standalone user prompts.
- JSON/CSV use an archival view and preserve as much of the flattened event
  stream as possible, including context/tool records. The raw `transcript.jsonl`
  remains the lossless source.
- AI reasoning records are omitted from rendered HTML/Markdown by default. Use
  `--include-reasoning` to include readable reasoning summaries/content there.
- File snapshots are inferred mainly from `apply_patch` and a few editor-style
  tool calls. Arbitrary shell commands can create files in ways no exporter can
  reliably infer.
- The rendered HTML uses `marked.js` and `highlight.js` from a CDN at runtime.
  The Markdown, JSON, CSV, metadata, and raw transcript are fully local.
- By default, developer/system context is not shown in rendered exports because
  it is usually noisy. Use `--include-context` when you want it.

## License

MIT. See [LICENSE](LICENSE).
