# Development VM

## Workspace

The persistent checkout is `/home/exedev/workspace/metalclaw`.
Open a new login shell, then:

```bash
cwork                  # enter the checkout (interactive Bash alias)
devbox                 # attach/create the persistent "dev" tmux session
hx .                   # Helix
pi                     # Pi coding agent
codex                  # Codex coding agent
uv sync --locked
task build-ci
```

Detach tmux with `Ctrl-b d`; reattach with `devbox`. Files survive VM restarts;
tmux processes survive SSH disconnection, not a VM reboot. No bot daemon has been
enabled: a development box should not send messages or run private jobs implicitly.
Use a systemd unit if a live bot is intentionally deployed later.

## Installed tooling

- uv-managed Python 3.14 and project `.venv` with locked runtime/dev dependencies.
- Helix (`hx`) with a matching runtime, relative line numbers, BasedPyright,
  Ruff diagnostics/code actions, and Ruff formatting on save.
- Standalone `ruff`, `basedpyright`, and `pre-commit` via `uv tool`.
  Project checks use the project's locked versions through `uv run`.
- Pi and Codex, configured in `~/.pi/agent/` and `~/.codex/config.toml` for
  the attached exe.dev managed LLM gateway, without stored provider credentials.
  The literal Pi key `implicit` is a placeholder, not a secret.
- Node.js LTS under `~/node`, with `node`, `npm`, and `npx` on PATH.
- Git, GitHub CLI, Task, gitleaks 8.30.1, C/C++ build tooling, Python headers,
  OpenSSL/libffi headers, ripgrep, fd, fzf, tmux, jq, SQLite, and ShellCheck.

Shell environment is in `~/.config/devbox/env.sh`, sourced from `.profile` and
`.bashrc`. `EDITOR` and `VISUAL` select Helix. Verify editor support with
`hx --health python`; use `uv run` for project commands or activate `.venv` if
desired. Helix's BasedPyright discovers the project `.venv` automatically.

## GitHub integration

`origin` uses the connected `lepinkainen-metalclaw` integration:

```bash
git remote -v
# https://github.int.exe.xyz/lepinkainen/metalclaw.git
git fetch origin
git switch -c your-change
# edit, test, commit
git push -u origin your-change
```

Authentication is injected by exe.dev for the integration host; do not add PATs
or rewrite the remote to an unauthenticated GitHub URL. The GitHub CLI being
installed does not imply its separate API authentication is configured.
The integration grants access to this connected repository, not arbitrary repos.

## Running the bot intentionally

Follow `AGENTS.md` and `config.example.yaml`. A private development template is
available at `~/.config/metalclaw/config.yaml`, with a separate empty vault at
`~/workspace/vault`, heartbeat and self-modification disabled. It deliberately
contains no frontend credentials. Select a working LLM provider and supply a
Telegram or Discord token before `task run`.

Do not globally export bot tokens or cloud keys into shell startup files.
Keep credentials in ignored configuration or a private environment file.
The coding agents' gateway configuration does not configure the bot's litellm
provider. Default bot data/log directories are persistent and outside Git.
