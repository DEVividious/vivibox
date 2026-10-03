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
#   - every task's disk still mounted under the tasks directory (its loop device freed; the
#     image stays with the task);
#   - the vivibox command (uv tool), the sudo rule, the root helper (the old firewall helper too)
#     and the tasks directory recorded for it in /etc/vivibox/host.json;
#   - the pods' egress rules (nft table inet vivibox), the host's address for the pods (vivibox0)
#     and the DNS forwarder (vivibox hostd);
#   - the vivibox skill for Claude Code and Codex, where vivibox installed it;
#   - the tasks directory's bind mount and its /etc/fstab line;
#   - Sysbox CE (restarts Docker);
#   - the Docker network ranges setup.sh wrote: /etc/docker/daemon.json goes back to the copy
#     setup.sh kept, or loses only those two keys (restarts Docker).
# What it asks about one by one: the tasks directory, ~/.config/vivibox, the stored API keys.
# What it leaves: the packages (tmux, jq, libnotify-bin, curl) and uv, which are not vivibox's.
set -euo pipefail

TASKS_DIR=${VIVIBOX_TASKS_DIR:-/srv/vivibox}
HELPER=/usr/local/libexec/vivibox-host
OLD_HELPER=/usr/local/libexec/vivibox-netns
HOST_FILE=/etc/vivibox/host.json
HOSTD_PID=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/vivibox/hostd.pid
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

# The forwarder's pid, when its pid file names a vivibox hostd that runs; nothing otherwise: a
# file left from before names a pid another process may have taken since.
hostd_pid() {
  local pid
  [[ -r "$HOSTD_PID" ]] || return 0
  pid=$(cat "$HOSTD_PID")
  [[ $pid =~ ^[0-9]+$ ]] || return 0
  if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q 'vivibox hostd'; then echo "$pid"; fi
  return 0
}

# The tasks' disks still mounted, one level under the tasks directory, a mount point a line.
task_disks() {
  findmnt -rn -o TARGET 2>/dev/null | awk -v root="$TASKS_DIR" '
    index($0, root "/") == 1 { rest = substr($0, length(root) + 2); if (rest ~ /^[^\/]+\/disk$/) print }'
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

mapfile -t DISKS < <(task_disks)
if ((${#DISKS[@]})); then
  found "${#DISKS[@]} task disks mounted under $TASKS_DIR (unmounted, their images kept with the tasks)" do_disks
else
  absent "task disks mounted under $TASKS_DIR"
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

if [[ -e "$HELPER" ]]; then found "root helper $HELPER" do_helper; else absent "root helper $HELPER"; fi

if [[ -e "$OLD_HELPER" ]]; then found "old firewall helper $OLD_HELPER" do_old_helper; fi

if [[ -e "$HOST_FILE" ]]; then found "tasks directory recorded for the helper in $HOST_FILE" do_host_file; else absent "$HOST_FILE"; fi

# The rules' table cannot be listed without root; it is there where vivibox0 or the helper is.
if ip -o link show vivibox0 >/dev/null 2>&1 || [[ -e "$HELPER" || -e "$HOSTD_PID" ]]; then
  found "the pods' egress rules (nft table inet vivibox), vivibox0 and the DNS forwarder" do_engine
else
  absent "the pods' egress rules and vivibox0"
fi

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

# Each disk: trimmed (its room back to the host; not fatal), unmounted, and the loop device its
# image is on freed; the image stays in the task.
do_disks() {
  local mnt dev
  for mnt in "${DISKS[@]}"; do
    sudo fstrim "$mnt" || echo "the room $mnt took is not returned to the host" >&2
    sudo umount "$mnt" || die "$mnt is busy; stop what uses it (a pod) and run again"
    dev=$(losetup -j "${mnt%/disk}/disk.img" -O NAME -n 2>/dev/null | head -1)
    [[ -n $dev ]] && sudo losetup -d "$dev"
  done
  return 0
}

do_vivibox() { "$UV" tool uninstall vivibox; }

do_skill() { rm -rf "${SKILLS[@]}"; }

do_sudoers() { sudo rm -f "$SUDOERS"; }

do_helper() { sudo rm -f "$HELPER"; }

do_old_helper() { sudo rm -f "$OLD_HELPER"; }

do_host_file() {
  sudo rm -f "$HOST_FILE"
  sudo rmdir "$(dirname "$HOST_FILE")" 2>/dev/null || true
}

do_engine() {
  # The forwarder (vivibox hostd) first: it listens on vivibox0's address.
  local pid
  pid=$(hostd_pid)
  if [[ -n $pid ]]; then kill "$pid" 2>/dev/null || true; fi
  rm -f "$HOSTD_PID"
  sudo nft delete table inet vivibox 2>/dev/null || true
  sudo ip link del vivibox0 2>/dev/null || true
}

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
