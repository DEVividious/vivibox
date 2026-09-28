#!/usr/bin/env bash
# Undoes what host/setup.sh set up, and removes what vivibox made since. Run as yourself; it asks
# for sudo when needed. It first lists what is there, removes only that after you confirm, and
# asks separately, item by item, before anything of yours is deleted: tasks, configuration, keys.
#
#   host/uninstall.sh           list what is there, remove it after confirmation
#   host/uninstall.sh --check   only list; exit status 1 if anything is there
#
# What it removes:
#   - every task's pod: containers, networks, volumes (the shared caches too) and the agent image;
#   - the vivibox command (uv tool), the sudo rule and the firewall helper;
#   - the vivibox skill for Claude Code and Codex, where vivibox installed it;
#   - the tasks directory's bind mount and its /etc/fstab line;
#   - Sysbox CE (restarts Docker);
#   - the Docker network ranges setup.sh wrote: /etc/docker/daemon.json goes back to the copy
#     setup.sh kept, or loses only those two keys (restarts Docker).
# What it asks about one by one: the tasks directory, ~/.config/vivibox, the stored API keys.
# What it leaves: the packages (tmux, jq, libnotify-bin, curl) and uv, which are not vivibox's.
set -euo pipefail

TASKS_DIR=${VIVIBOX_TASKS_DIR:-/srv/vivibox}
HELPER=/usr/local/libexec/vivibox-netns
SUDOERS=/etc/sudoers.d/vivibox
DOCKER_CFG=/etc/docker/daemon.json
CONFIG_DIR=${VIVIBOX_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/vivibox}
KEYS_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/vivibox
BIN=$HOME/.local/bin

CHECK_ONLY=false
[[ "${1:-}" == --check ]] && CHECK_ONLY=true
[[ $# -eq 0 || "$CHECK_ONLY" == true ]] || { echo "usage: $0 [--check]" >&2; exit 2; }

die() { echo "uninstall: $*" >&2; exit 1; }

# Where each agent CLI reads the vivibox skill, as in setup.sh; keep in step with PLACES in
# vivibox/skill.py.
SKILL_PLACES=(".claude .claude/skills" ".codex .agents/skills")

# The copies of the skill vivibox installed (with .vivibox-version beside SKILL.md), a path a line.
skill_copies() {
  local place skills
  for place in "${SKILL_PLACES[@]}"; do
    skills=${place#* }
    [[ -f "$HOME/$skills/vivibox/.vivibox-version" ]] && echo "$HOME/$skills/vivibox"
  done
  return 0
}

# Sourced by the tests for the functions above.
[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0

[[ $EUID -ne 0 ]] || die "run as your user, not root; the script uses sudo where needed"
[[ "$TASKS_DIR" == /* && "$TASKS_DIR" != "$HOME"* ]] || die "VIVIBOX_TASKS_DIR must be absolute and outside \$HOME"
command -v docker >/dev/null || die "Docker is not installed"
docker info >/dev/null 2>&1 || die "cannot talk to Docker; is your user in the docker group?"

present() { printf '  present  %s\n' "$1"; }
absent() { printf '  -        %s\n' "$1"; }

todo=() actions=()
found() {
  present "$1"
  todo+=("$1")
  actions+=("$2")
}

echo "Checking host…"

# --- pods and images ----------------------------------------------------------------------------

mapfile -t CONTAINERS < <(docker ps -aq --filter label=vivibox.task)
mapfile -t NETWORKS < <(docker network ls -q --filter 'name=^vivibox-.*-net$')
mapfile -t VOLUMES < <(docker volume ls -q | grep '^vivibox-' || true)
mapfile -t IMAGES < <(docker images -q vivibox-agent)
if ((${#CONTAINERS[@]} + ${#NETWORKS[@]} + ${#VOLUMES[@]} + ${#IMAGES[@]})); then
  found "pods in Docker: ${#CONTAINERS[@]} containers, ${#NETWORKS[@]} networks, ${#VOLUMES[@]} volumes (caches included), ${#IMAGES[@]} agent images" do_pods
else
  absent "pods in Docker"
fi

# --- the command, the sudo rule, the helper -----------------------------------------------------

UV=$(command -v uv || echo "$BIN/uv")
if [[ -x "$UV" ]] && "$UV" tool list 2>/dev/null | grep -q '^vivibox '; then
  found "vivibox command (uv tool)" do_vivibox
else
  absent "vivibox command"
fi

mapfile -t SKILLS < <(skill_copies)
if ((${#SKILLS[@]})); then found "the vivibox skill in ${SKILLS[*]}" do_skill; else absent "the vivibox skill"; fi

if [[ -e "$SUDOERS" ]]; then found "sudo rule $SUDOERS" do_sudoers; else absent "sudo rule $SUDOERS"; fi

if [[ -e "$HELPER" ]]; then found "firewall helper $HELPER" do_helper; else absent "firewall helper $HELPER"; fi

# --- the tasks directory's mount ----------------------------------------------------------------

FSTAB_LINE="$TASKS_DIR $TASKS_DIR none bind,nosuid,nodev 0 0"
if grep -qxF "$FSTAB_LINE" /etc/fstab 2>/dev/null || findmnt -n --mountpoint "$TASKS_DIR" >/dev/null 2>&1; then
  found "$TASKS_DIR bind mount and its /etc/fstab line" do_mount
else
  absent "$TASKS_DIR bind mount"
fi

# --- Sysbox and Docker's ranges -----------------------------------------------------------------

if dpkg-query -W -f='${Status}' sysbox-ce 2>/dev/null | grep -q 'install ok installed'; then
  found "Sysbox CE $(dpkg-query -W -f='${Version}' sysbox-ce) (restarts Docker)" do_sysbox
else
  absent "Sysbox CE"
fi

# setup.sh keeps a dated copy before its first change; the oldest is the file as it was.
OLDEST_BACKUP=""
for f in "$DOCKER_CFG".bak-*; do
  [[ -e "$f" ]] || continue
  [[ -z "$OLDEST_BACKUP" || "$f" < "$OLDEST_BACKUP" ]] && OLDEST_BACKUP=$f
done
if [[ -n "$OLDEST_BACKUP" ]]; then
  found "Docker network ranges in $DOCKER_CFG: back to $OLDEST_BACKUP, the copy setup.sh kept (restarts Docker)" do_docker_networks
elif [[ -s "$DOCKER_CFG" ]] && jq -e '.bip // ."default-address-pools"' "$DOCKER_CFG" >/dev/null 2>&1; then
  found "Docker network ranges in $DOCKER_CFG: bip and default-address-pools (restarts Docker)" do_docker_networks
else
  absent "Docker network ranges in $DOCKER_CFG"
fi

# --- yours: asked about one by one, never in the list above -------------------------------------

yours=() yours_actions=()
if [[ -d "$TASKS_DIR" ]]; then
  count=$(find "$TASKS_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
  yours+=("tasks directory $TASKS_DIR with $count tasks: every clone, plan, log and review copy")
  yours_actions+=(do_tasks_dir)
fi
if [[ -d "$CONFIG_DIR" ]]; then
  yours+=("configuration $CONFIG_DIR: config.toml, projects, providers")
  yours_actions+=(do_config)
fi
if [[ -d "$KEYS_DIR" ]]; then
  yours+=("stored API keys in $KEYS_DIR/keys")
  yours_actions+=(do_keys)
fi
for item in "${yours[@]}"; do present "$item  (asked about separately)"; done

# --- actions ------------------------------------------------------------------------------------

do_pods() {
  ((${#CONTAINERS[@]})) && docker rm -f "${CONTAINERS[@]}" >/dev/null
  ((${#NETWORKS[@]})) && docker network rm "${NETWORKS[@]}" >/dev/null
  ((${#VOLUMES[@]})) && docker volume rm "${VOLUMES[@]}" >/dev/null
  ((${#IMAGES[@]})) && docker rmi -f "${IMAGES[@]}" >/dev/null
  return 0
}

do_vivibox() { "$UV" tool uninstall vivibox; }

do_skill() { rm -rf "${SKILLS[@]}"; }

do_sudoers() { sudo rm -f "$SUDOERS"; }

do_helper() { sudo rm -f "$HELPER"; }

do_mount() {
  if findmnt -n --mountpoint "$TASKS_DIR" >/dev/null 2>&1; then
    sudo umount "$TASKS_DIR" || die "$TASKS_DIR is busy; stop what uses it (a pod, a shell) and run again"
  fi
  if grep -qxF "$FSTAB_LINE" /etc/fstab; then
    sudo cp -a /etc/fstab "/etc/fstab.bak-$(date +%Y%m%d-%H%M%S)"
    grep -vxF "$FSTAB_LINE" /etc/fstab | sudo tee /etc/fstab.new >/dev/null
    sudo chmod 644 /etc/fstab.new
    sudo mv /etc/fstab.new /etc/fstab
    sudo systemctl daemon-reload
  fi
}

do_sysbox() { sudo apt-get remove -y -q sysbox-ce; }

do_docker_networks() {
  local now
  now=$(date +%Y%m%d-%H%M%S)
  sudo cp -a "$DOCKER_CFG" "$DOCKER_CFG.uninstall-$now"
  if [[ -n "$OLDEST_BACKUP" ]]; then
    sudo cp -a "$OLDEST_BACKUP" "$DOCKER_CFG"
  else
    sudo jq --indent 4 'del(.bip, ."default-address-pools")' "$DOCKER_CFG" | sudo tee "$DOCKER_CFG.new" >/dev/null
    sudo mv "$DOCKER_CFG.new" "$DOCKER_CFG"
  fi
  sudo systemctl restart docker
}

do_tasks_dir() {
  # Still mounted when the mount was kept above: a mount point cannot be removed, its contents can.
  findmnt -n --mountpoint "$TASKS_DIR" >/dev/null 2>&1 && sudo umount "$TASKS_DIR"
  sudo rm -rf "$TASKS_DIR"
}

do_config() { rm -rf "$CONFIG_DIR"; }

do_keys() { rm -rf "$KEYS_DIR"; }

# --- apply --------------------------------------------------------------------------------------

if ((${#todo[@]} + ${#yours[@]} == 0)); then
  echo "Nothing of vivibox's is here."
  exit 0
fi
$CHECK_ONLY && exit 1

if ((${#todo[@]})); then
  echo
  echo "To remove:"
  printf '  - %s\n' "${todo[@]}"
  [[ " ${actions[*]} " == *" do_sysbox "* || " ${actions[*]} " == *" do_docker_networks "* ]] \
    && echo "Docker restarts; every running container stops for a moment."
  read -r -p "Continue? [y/N] " answer
  if [[ "$answer" == [yY] ]]; then
    for action in "${actions[@]}"; do "$action"; done
  else
    echo "Left as it is."
  fi
fi

for i in "${!yours[@]}"; do
  echo
  read -r -p "Delete ${yours[$i]}? This cannot be undone. [y/N] " answer
  if [[ "$answer" == [yY] ]]; then "${yours_actions[$i]}"; else echo "Kept."; fi
done

echo
echo "Done. Left in place: the packages (tmux, jq, libnotify-bin, curl) and uv. Copies of what was"
echo "changed: $DOCKER_CFG.uninstall-* and /etc/fstab.bak-*, when those files were touched."
