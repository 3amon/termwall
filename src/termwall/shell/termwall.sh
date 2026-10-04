# termwall shell hook (bash + zsh). Installed by `termwall install-shell`.
#
# Interactive `claude` / `codex` start inside a tmux session named after the
# current directory, so termwall can show and open them from any machine.
# When the agent exits the tmux session ends and you're back at your prompt.
#
#   TERMWALL_TMUX=0 claude ...    run inline this once
#   Ctrl-b d                      detach (session keeps running, open it later)
#
# Optional per-tool pre-hooks run right before the real binary, inside tmux:
#   termwall_pre_claude() { ...; }   (e.g. unlocking the macOS keychain)

_termwall_exec() {
  local tool=$1; shift
  if typeset -f "termwall_pre_$tool" >/dev/null 2>&1; then "termwall_pre_$tool"; fi
  command "$tool" "$@"
}

_termwall_wrap() {
  local tool=$1; shift
  local a inline=
  # already in tmux, opted out, not a terminal, or no tmux: run as usual
  if [ -n "$TMUX" ] || [ "${TERMWALL_TMUX:-1}" = 0 ] || [ ! -t 0 ] || [ ! -t 1 ] \
     || ! command -v tmux >/dev/null 2>&1; then
    _termwall_exec "$tool" "$@"; return
  fi
  # one-shot / admin invocations stay inline: subcommands, --print, --help ...
  case "${1:-}" in
    ""|-*|resume) ;;
    *) inline=1 ;;
  esac
  for a in "$@"; do
    case "$a" in -p|--print|-h|--help|-v|--version) inline=1 ;; esac
  done
  if [ -n "$inline" ]; then _termwall_exec "$tool" "$@"; return; fi

  local base name i=2
  base=$(basename "$PWD" | tr -c 'A-Za-z0-9_\n-' '-')
  [ "$PWD" = "$HOME" ] && base=$tool
  name=$base
  while tmux has-session -t "=$name" 2>/dev/null; do name="$base-$i"; i=$((i + 1)); done
  # run through an interactive shell so rc-defined hooks (and this file) apply
  tmux new-session -s "$name" -c "$PWD" -- "${SHELL:-/bin/sh}" -ic '"$0" "$@"' "$tool" "$@"
}

claude() { _termwall_wrap claude "$@"; }
codex() { _termwall_wrap codex "$@"; }
