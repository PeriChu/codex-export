# codex-export / codex-import

<!--
machine-readable-summary:
  project: codex-export
  package: codex-export
  commands:
    - codex-export
    - codex-import
  python: ">=3.9"
  dependencies: "stdlib only"
  branches:
    macos_linux: macos
    windows: windows
  default_safety: "no auth is exported or imported unless explicitly confirmed"
  export_outputs:
    - session.html
    - session.md
    - session.json
    - session.csv
    - metadata.json
    - transcript.jsonl
    - inputs/
    - outputs/
  high_risk_auth_flags:
    export_interactive: "codex-export export latest --include-auth"
    export_automation: "codex-export export latest --include-auth --yes-i-know-this-is-risky"
    import_skip: "codex-import import ./exports/<session-id> --skip-auth"
    import_interactive: "codex-import import ./exports/<session-id> --include-auth"
    import_automation: "codex-import import ./exports/<session-id> --include-auth --yes-i-know-this-is-risky"
-->

Zero-dependency tools for backing up and restoring local Codex conversations.

- `codex-export`: export local Codex sessions to portable HTML / Markdown / JSON / CSV bundles.
- `codex-import`: restore a `codex-export` bundle into a Codex home and a workspace.
- Default mode is safe: auth files are not exported or imported unless you explicitly opt in.
- Cross-platform restore is supported across macOS, Windows, and Linux path styles.

Languages: [中文](#中文说明) / [English](#english)

## Quick Commands

macOS / Linux:

```bash
pipx install "git+https://github.com/PeriChu/codex-export.git@macos"
codex-export list
codex-export export latest --output ./exports
codex-import inspect ./exports/<session-id>
codex-import import ./exports/<session-id>
```

Windows PowerShell:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install "git+https://github.com/PeriChu/codex-export.git@windows"
codex-export list
codex-export export latest --output .\exports
codex-import inspect .\exports\<session-id>
codex-import import .\exports\<session-id>
```

High-risk same-account auth migration:

```bash
# Export auth: interactive prompt asks you to type I UNDERSTAND.
codex-export export latest --include-auth

# Import bundle but ignore auth.
codex-import import ./exports/<session-id> --skip-auth

# Import auth: interactive prompt asks for confirmation.
codex-import import ./exports/<session-id> --include-auth
```

For non-interactive automation only:

```bash
codex-export export latest --include-auth --yes-i-know-this-is-risky
codex-import import ./exports/<session-id> --include-auth --yes-i-know-this-is-risky
```

---

## 中文说明

### 这个项目做什么

Codex Desktop/CLI 会把本地会话记录为 JSONL 文件，通常在：

- macOS / Linux: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Windows: `%USERPROFILE%\.codex`、`%APPDATA%\Codex`、`%LOCALAPPDATA%\Codex` 或 `%APPDATA%\OpenAI\Codex`

会话标题索引在：

```text
<codex-home>/session_index.jsonl
```

`codex-export` 把这些本地记录导出成一个便携 bundle，适合阅读、归档、发给另一个 LLM、做数据分析，或者迁移到另一台机器。`codex-import` 则把这个 bundle 恢复成新的工作区和 Codex 本地会话。

### 安全默认值

默认不会导出或导入任何鉴权文件。默认 bundle 只包含会话、输入文件、输出文件、渲染视图和结构化数据。

只有你主动使用 `--include-auth` 时，才会处理 Codex auth 文件。auth 模式属于高风险模式，只建议用于：

- 同一个 Codex 账号，从自己的旧设备迁移到自己的新设备。
- 放进加密介质里的私人备份，比如 1Password、GPG、age 或加密磁盘。

不要把带 `auth/` 的 bundle 发给别人，也不要未加密上传到网盘、聊天软件或邮件。拿到 auth bundle 的人可能能以你的 Codex 身份继续使用，直到你主动退出设备、改密或撤销凭证。

### 安装

推荐用 `pipx`，这样命令会全局可用，而且不会污染你的普通 Python 环境。

macOS / Linux:

```bash
brew install pipx
pipx ensurepath
pipx install "git+https://github.com/PeriChu/codex-export.git@macos"
```

没有 Homebrew 时：

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install "git+https://github.com/PeriChu/codex-export.git@macos"
```

Windows PowerShell:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install "git+https://github.com/PeriChu/codex-export.git@windows"
```

运行 `ensurepath` 后请重开终端。安装成功后检查：

```bash
codex-export --help
codex-import --help
```

如果 Windows 上 GitHub clone 不稳定，可以下载 `windows` 分支 ZIP 后本地安装：

```powershell
Invoke-WebRequest `
  -Uri "https://github.com/PeriChu/codex-export/archive/refs/heads/windows.zip" `
  -OutFile "$env:TEMP\codex-export-windows.zip"

Expand-Archive "$env:TEMP\codex-export-windows.zip" -DestinationPath "$env:TEMP" -Force
pipx install "$env:TEMP\codex-export-windows"
```

也可以不安装，直接在仓库目录运行：

```bash
python3 codex_export.py --help
python3 codex_import.py --help
```

### 导出会话

列出本机 Codex 会话：

```bash
codex-export list
```

导出最近一个会话：

```bash
codex-export export latest --output ./exports
```

按 session id 前缀或标题关键词导出：

```bash
codex-export export <session-id-prefix> --output ./exports
codex-export export "<session-title>" --output ./exports
```

导出全部可见会话：

```bash
codex-export export all --output ./exports
```

只导出部分格式：

```bash
codex-export export latest --formats html,json
```

不复制输入/输出文件，只保留 transcript 和结构化记录：

```bash
codex-export export latest --no-files
```

包含隐藏的 subagent/background rollout：

```bash
codex-export list --include-subagents
codex-export export all --include-subagents
```

指定另一个 Codex home：

```bash
codex-export list --codex-home ~/.codex-backup
codex-export export latest --codex-home ~/.codex-backup --output ./exports
```

### 导出结果结构

每个会话会导出到一个独立目录：

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

常见文件含义：

- `session.html`: 适合浏览器阅读的视图，有明暗模式、Markdown 渲染、代码高亮、折叠的工具调用和 commentary。
- `session.md`: 适合 GitHub 或普通 Markdown 阅读的文本导出。
- `session.json`: 结构化逐 block 导出，适合再喂给 LLM 或程序处理。
- `session.csv`: 表格化逐 block 导出，适合 Excel/Numbers/脚本处理；过大的单元格会截断，全文仍在 JSONL/JSON 中。
- `metadata.json`: 会话元信息。
- `transcript.jsonl`: Codex 原始 JSONL transcript，是最完整的原始记录。
- `inputs/`: 用户上传、提到或本地引用的输入文件。
- `outputs/`: Codex 查看、生成、编辑或 shell/PowerShell 命令明显写出的输出文件。

### 导入会话

先查看 bundle 会恢复什么：

```bash
codex-import inspect ./exports/<session-id>
```

导入到默认位置：

```bash
codex-import import ./exports/<session-id>
```

默认会创建：

```text
~/CodexImported/<title>-<session-prefix>/
|-- CONTINUE_PROMPT.md
|-- RESTORE_REPORT.md
|-- inputs/
|-- outputs/
`-- original_bundle/

~/.codex/
|-- session_index.jsonl
`-- sessions/YYYY/MM/DD/rollout-*.jsonl
```

指定恢复工作区：

```bash
codex-import import ./exports/<session-id> --workspace ~/Documents/RestoredProject
```

导入到测试用 Codex home，避免碰真实 `~/.codex`：

```bash
codex-import import ./exports/<session-id> --codex-home ~/.codex-restore-test
codex-export list --codex-home ~/.codex-restore-test
```

如果目标机器已经有同 session id，会拒绝覆盖。可以导入为新 id：

```bash
codex-import import ./exports/<session-id> --new-id
```

或者明确覆盖 transcript/index：

```bash
codex-import import ./exports/<session-id> --force
```

### 高风险 auth 模式

默认 no-auth 模式足够完成大部分迁移：新机器用自己的 Codex 登录状态，只恢复历史、文件和工作区。

如果你确实要迁移同账号鉴权，可以导出 auth：

```bash
codex-export export latest --include-auth
```

它会要求你手动输入：

```text
I UNDERSTAND
```

自动化脚本可以跳过交互，但必须使用一个很长的风险 flag：

```bash
codex-export export latest --include-auth --yes-i-know-this-is-risky
```

带 auth 的 bundle 会额外出现：

```text
exports/<session-id>/
`-- auth/
    |-- _manifest.json
    |-- _origin.json
    |-- README.txt
    |-- auth.json
    `-- installation_id
```

导入时，如果 bundle 里有 `auth/`，默认会展示来源信息并询问 `[y/N]`。回车或非交互环境默认跳过 auth。

明确忽略 auth：

```bash
codex-import import ./exports/<session-id> --skip-auth
```

请求恢复 auth：

```bash
codex-import import ./exports/<session-id> --include-auth
```

自动化恢复 auth：

```bash
codex-import import ./exports/<session-id> --include-auth --yes-i-know-this-is-risky
```

如果目标 Codex home 已有 `auth.json` 或 `installation_id`，默认不会覆盖。你必须再加：

```bash
codex-import import ./exports/<session-id> --include-auth --force-auth
```

覆盖前会写 `.bak` 备份。

auth 文件不会出现在 `session.html`、`session.md`、`session.json`、`session.csv`，导入时也不会被复制到 `workspace/original_bundle/`。它们只存在于导出 bundle 顶层 `auth/`，以及你明确恢复后的目标 `<codex-home>`。

### 跨平台恢复

导入器会把旧机器路径改写为新 workspace 路径，覆盖这些常见形式：

- macOS/Linux: `/Users/<USER>/Documents/project`
- Windows: `C:\Users\<USER>\Documents\project`
- Windows slash form: `C:/Users/<USER>/Documents/project`
- file URI: `file:///C:/Users/<USER>/Documents/project/file.pdf`

输入/输出文件会恢复到新 workspace 的 `inputs/` 和 `outputs/`，所以 transcript 中引用的文件在新机器上也能打开。

### 常见问题

看不到导入后的会话：

```bash
codex-export list --codex-home <target-codex-home>
```

如果这里能看到，但 Codex app 侧边栏没有显示，重启 Codex app，让它重新读取 `session_index.jsonl` 和 `sessions/`。

Windows 安装时报 GitHub clone 断开：使用上面的 ZIP 安装方式，或者稍后重试网络。

导入时担心覆盖真实 Codex 数据：先用临时 home 测试：

```bash
codex-import import ./exports/<session-id> --codex-home ~/.codex-restore-test
codex-export list --codex-home ~/.codex-restore-test
```

### 限制

- HTML/Markdown 是阅读视图，会隐藏很多噪声上下文。
- JSON/CSV 是归档视图，会尽量保留上下文、工具调用和推理摘要。
- 原始 `transcript.jsonl` 永远是最完整记录。
- 文件复制依赖可推断路径。复杂 shell 脚本动态生成的路径不一定能被完全发现。
- 精确“无缝续写”取决于 Codex app 是否只依赖本地 JSONL/index。若未来版本还需要服务端线程状态，恢复工作区仍会包含 `CONTINUE_PROMPT.md` 和 `original_bundle/` 作为稳妥 fallback。

---

## English

### What This Project Does

Codex Desktop/CLI stores local conversations as JSONL files, usually under:

- macOS / Linux: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Windows candidates: `%USERPROFILE%\.codex`, `%APPDATA%\Codex`, `%LOCALAPPDATA%\Codex`, or `%APPDATA%\OpenAI\Codex`

The human-readable title index lives at:

```text
<codex-home>/session_index.jsonl
```

`codex-export` turns those local records into a portable bundle for reading, archiving, LLM ingestion, spreadsheet analysis, or migration. `codex-import` restores that bundle into a workspace and a Codex home.

### Safety Defaults

By default, no authentication files are exported or imported. A normal bundle contains only transcripts, rendered views, structured data, and copied input/output files.

Auth is handled only when you explicitly use `--include-auth`. This is a high-risk mode intended only for:

- migrating your own Codex account from one device you control to another device you control;
- encrypted personal backup stored in a password manager, GPG, age, or encrypted disk.

Do not send an auth bundle to other people. Do not upload it unencrypted to cloud drives, chat, or email. Anyone with the auth bundle may be able to act as your Codex account until you rotate or revoke credentials.

### Install

`pipx` is recommended because it makes the commands globally available without mixing anything into your normal Python environment.

macOS / Linux:

```bash
brew install pipx
pipx ensurepath
pipx install "git+https://github.com/PeriChu/codex-export.git@macos"
```

Without Homebrew:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install "git+https://github.com/PeriChu/codex-export.git@macos"
```

Windows PowerShell:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install "git+https://github.com/PeriChu/codex-export.git@windows"
```

Restart the terminal after `ensurepath`. Then verify:

```bash
codex-export --help
codex-import --help
```

If GitHub clone is unstable on Windows, install from the Windows branch ZIP:

```powershell
Invoke-WebRequest `
  -Uri "https://github.com/PeriChu/codex-export/archive/refs/heads/windows.zip" `
  -OutFile "$env:TEMP\codex-export-windows.zip"

Expand-Archive "$env:TEMP\codex-export-windows.zip" -DestinationPath "$env:TEMP" -Force
pipx install "$env:TEMP\codex-export-windows"
```

You can also run directly from a clone:

```bash
python3 codex_export.py --help
python3 codex_import.py --help
```

### Export Sessions

List local Codex sessions:

```bash
codex-export list
```

Export the newest session:

```bash
codex-export export latest --output ./exports
```

Export by session id prefix or title substring:

```bash
codex-export export <session-id-prefix> --output ./exports
codex-export export "Review paper" --output ./exports
```

Export every visible session:

```bash
codex-export export all --output ./exports
```

Choose formats:

```bash
codex-export export latest --formats html,json
```

Transcript-only bundle, without copied input/output files:

```bash
codex-export export latest --no-files
```

Include hidden subagent/background rollouts:

```bash
codex-export list --include-subagents
codex-export export all --include-subagents
```

Use another Codex home:

```bash
codex-export list --codex-home ~/.codex-backup
codex-export export latest --codex-home ~/.codex-backup --output ./exports
```

### Export Bundle Layout

Each session is exported into one directory:

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

File meanings:

- `session.html`: browser-friendly reading view with light/dark mode, Markdown rendering, syntax highlighting, collapsible tool activity, and collapsible commentary.
- `session.md`: Markdown reading export.
- `session.json`: structured per-block export for LLMs or scripts.
- `session.csv`: flat per-block table for spreadsheets; huge cells are capped, with full content kept in JSON/JSONL.
- `metadata.json`: extracted session metadata.
- `transcript.jsonl`: raw Codex JSONL transcript, the most complete source.
- `inputs/`: user-provided, user-mentioned, or locally referenced input files.
- `outputs/`: viewed/generated/edited files and common shell/PowerShell output targets.

### Import Sessions

Inspect a bundle first:

```bash
codex-import inspect ./exports/<session-id>
```

Import into default locations:

```bash
codex-import import ./exports/<session-id>
```

Default import creates:

```text
~/CodexImported/<title>-<session-prefix>/
|-- CONTINUE_PROMPT.md
|-- RESTORE_REPORT.md
|-- inputs/
|-- outputs/
`-- original_bundle/

~/.codex/
|-- session_index.jsonl
`-- sessions/YYYY/MM/DD/rollout-*.jsonl
```

Restore into a specific workspace:

```bash
codex-import import ./exports/<session-id> --workspace ~/Documents/RestoredProject
```

Test against a temporary Codex home first:

```bash
codex-import import ./exports/<session-id> --codex-home ~/.codex-restore-test
codex-export list --codex-home ~/.codex-restore-test
```

Import as a new session id when the destination already has the same id:

```bash
codex-import import ./exports/<session-id> --new-id
```

Overwrite transcript/index intentionally:

```bash
codex-import import ./exports/<session-id> --force
```

### High-Risk Auth Mode

Default no-auth mode is enough for most migrations: the destination machine keeps its own Codex login state, while history, files, and workspace context are restored.

Export auth only when you really need same-account credential migration:

```bash
codex-export export latest --include-auth
```

It asks you to type:

```text
I UNDERSTAND
```

Automation can skip the prompt only with the long risk flag:

```bash
codex-export export latest --include-auth --yes-i-know-this-is-risky
```

An auth bundle also contains:

```text
exports/<session-id>/
`-- auth/
    |-- _manifest.json
    |-- _origin.json
    |-- README.txt
    |-- auth.json
    `-- installation_id
```

When importing a bundle that contains `auth/`, the importer shows source metadata and asks `[y/N]`. Pressing Enter, or running in a non-interactive environment, skips auth by default.

Explicitly ignore auth:

```bash
codex-import import ./exports/<session-id> --skip-auth
```

Request auth restore:

```bash
codex-import import ./exports/<session-id> --include-auth
```

Automation restore:

```bash
codex-import import ./exports/<session-id> --include-auth --yes-i-know-this-is-risky
```

Existing destination auth files are not overwritten unless you add:

```bash
codex-import import ./exports/<session-id> --include-auth --force-auth
```

Existing files are backed up with a `.bak` suffix before overwrite.

Auth files never appear in `session.html`, `session.md`, `session.json`, or `session.csv`. During import, they are also excluded from `workspace/original_bundle/`. They live only in the top-level bundle `auth/` directory and, if explicitly restored, in the destination `<codex-home>`.

### Cross-Platform Restore

The importer rewrites old absolute paths to the restored workspace. It recognizes common forms:

- macOS/Linux: `/Users/<USER>/Documents/project`
- Windows: `C:\Users\<USER>\Documents\project`
- Windows slash form: `C:/Users/<USER>/Documents/project`
- file URI: `file:///C:/Users/<USER>/Documents/project/file.pdf`

Input/output files are restored under `inputs/` and `outputs/` in the new workspace, so transcript references point at files that exist on the destination machine.

### Troubleshooting

Imported session does not appear in Codex:

```bash
codex-export list --codex-home <target-codex-home>
```

If the command sees the session but the Codex app sidebar does not, restart Codex so it reloads `session_index.jsonl` and `sessions/`.

GitHub clone fails on Windows: use the ZIP install path above or retry on a more stable network.

Worried about touching real Codex data: import into a temporary Codex home first:

```bash
codex-import import ./exports/<session-id> --codex-home ~/.codex-restore-test
codex-export list --codex-home ~/.codex-restore-test
```

### Limitations

- HTML/Markdown are reading views and hide noisy context by default.
- JSON/CSV are archival views and preserve more context, tool calls, and reasoning summaries.
- Raw `transcript.jsonl` is always the most complete record.
- File copying depends on inferable paths. Complex shell scripts may create files in ways no exporter can fully infer.
- Perfect in-app continuation depends on whether the Codex app only needs local JSONL/index. If a future Codex version also requires server-side thread state, the restored workspace still includes `CONTINUE_PROMPT.md` and `original_bundle/` as a fallback.

## License

MIT. See [LICENSE](LICENSE).
