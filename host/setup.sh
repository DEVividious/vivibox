#!/usr/bin/env bash
# One-time host preparation for vivibox (Ubuntu with Docker installed). Run as yourself; it asks
# for sudo when needed. It first lists what is missing, changes only that after you confirm,
# and changes nothing on a second run.
#
#   host/setup.sh           check, list the changes, apply them after confirmation
#   host/setup.sh --check   only check; exit status 1 if something is missing
#
# What it sets up:
#   - packages: tmux (agent sessions), jq, libnotify-bin (desktop notifications), curl;
#   - Docker networks moved off the ranges Docker uses inside Sysbox containers (restarts Docker);
#   - Sysbox CE, the runtime for the per-task Docker sidecar;
#   - the tasks directory, outside $HOME, yours only (750), mounted nosuid,nodev;
#   - the egress firewall helper and a sudo rule that allows running only that helper.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
TASKS_DIR=${VIVIBOX_TASKS_DIR:-/srv/vivibox}
HELPER=/usr/local/libexec/vivibox-netns
SUDOERS=/etc/sudoers.d/vivibox
DOCKER_CFG=/etc/docker/daemon.json
PACKAGES=(tmux jq libnotify-bin curl)
# Docker inside Sysbox containers uses its default 172.17.0.0/16; the host daemon must not.
BIP=172.20.0.1/16
POOL=172.25.0.0/16
SYSBOX_VERSION=0.7.1
SYSBOX_DEB="sysbox-ce_${SYSBOX_VERSION}.linux_amd64.deb"
SYSBOX_SHA256=9d6d5484f980d0a17f86c492c1262015c2afb66280bdb97215b79fde6a0261c5

CHECK_ONLY=false
[[ "${1:-}" == --check ]] && CHECK_ONLY=true
[[ $# -eq 0 || "$CHECK_ONLY" == true ]] || { echo "usage: $0 [--check]" >&2; exit 2; }

die() { echo "setup: $*" >&2; exit 1; }
ok() { printf '  ok       %s\n' "$1"; }

todo=() actions=()
need() {
  printf '  missing  %s\n' "$1"
  todo+=("$1")
  actions+=("$2")
}

# --- checks -----------------------------------------------------------------------------------

[[ $EUID -ne 0 ]] || die "run as your user, not root; the script uses sudo where needed"
[[ "$TASKS_DIR" == /* && "$TASKS_DIR" != "$HOME"* ]] || die "VIVIBOX_TASKS_DIR must be absolute and outside \$HOME"
command -v docker >/dev/null || die "Docker is not installed"
docker info >/dev/null 2>&1 || die "cannot talk to Docker; is your user in the docker group?"

echo "Checking host…"

missing_packages=()
for p in "${PACKAGES[@]}"; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing_packages+=("$p")
done
if ((${#missing_packages[@]})); then
  need "packages: ${missing_packages[*]}" do_packages
else
  ok "packages: ${PACKAGES[*]}"
fi

docker_networks_set() {
  [[ -r "$DOCKER_CFG" ]] || return 1
  command -v jq >/dev/null || return 1
  jq -e '(.bip // "") != "" and ((."default-address-pools" // []) | length) > 0' "$DOCKER_CFG" >/dev/null 2>&1
}
if docker_networks_set; then
  ok "Docker networks moved off 172.17.0.0/16 ($(jq -r .bip "$DOCKER_CFG"))"
else
  need "Docker networks: bip $BIP, address pool $POOL in $DOCKER_CFG (restarts Docker)" do_docker_networks
fi

sysbox_registered() { docker info --format '{{range $k, $v := .Runtimes}}{{$k}} {{end}}' | grep -qw sysbox-runc; }
if dpkg-query -W -f='${Status}' sysbox-ce 2>/dev/null | grep -q 'install ok installed' && sysbox_registered; then
  ok "Sysbox CE $(dpkg-query -W -f='${Version}' sysbox-ce)"
else
  need "Sysbox CE $SYSBOX_VERSION (restarts Docker)" do_sysbox
fi

if [[ -d "$TASKS_DIR" && "$(stat -c '%U %a' "$TASKS_DIR")" == "$USER 750" ]]; then
  ok "tasks directory $TASKS_DIR (yours, 750)"
else
  need "tasks directory $TASKS_DIR owned by $USER, mode 750" do_tasks_dir
fi

FSTAB_LINE="$TASKS_DIR $TASKS_DIR none bind,nosuid,nodev 0 0"
mount_ok() {
  local opts
  opts=$(findmnt -n -o OPTIONS --mountpoint "$TASKS_DIR" 2>/dev/null) || return 1
  [[ ",$opts," == *,nosuid,* && ",$opts," == *,nodev,* ]]
}
if grep -qxF "$FSTAB_LINE" /etc/fstab && mount_ok; then
  ok "$TASKS_DIR mounted nosuid,nodev (/etc/fstab)"
else
  need "$TASKS_DIR as a nosuid,nodev bind mount in /etc/fstab" do_mount
fi

if cmp -s "$HERE/vivibox-netns" "$HELPER" && [[ "$(stat -c '%U %a' "$HELPER")" == "root 755" ]]; then
  ok "firewall helper $HELPER"
else
  need "firewall helper $HELPER (root-owned copy of host/vivibox-netns)" do_helper
fi

SUDO_RULE="$USER ALL=(root) NOPASSWD: $HELPER"
if sudo -n -l "$HELPER" >/dev/null 2>&1; then
  ok "sudo rule for $HELPER"
else
  need "sudo rule in $SUDOERS: $SUDO_RULE" do_sudoers
fi

# --- actions ----------------------------------------------------------------------------------

do_packages() {
  sudo apt-get install -y -q "${missing_packages[@]}"
}

do_docker_networks() {
  local net current updated
  for net in 172.20.0.0/16 "$POOL"; do
    if ip -4 route | awk '{print $1}' | grep -q "^${net%.*.*}\."; then
      ip -4 route | grep "^${net%.*.*}\." >&2
      die "$net overlaps an existing route (VPN?); change BIP/POOL in this script"
    fi
  done
  if sudo test -s "$DOCKER_CFG"; then
    sudo cp -a "$DOCKER_CFG" "$DOCKER_CFG.bak-$(date +%Y%m%d-%H%M%S)"
    current=$(sudo cat "$DOCKER_CFG")
  else
    current='{}'
  fi
  # Indented by 4: the Sysbox installer detects existing settings with a regex on indented keys.
  updated=$(jq --indent 4 --arg bip "$BIP" --arg pool "$POOL" '
    (if (.bip // "") == "" then .bip = $bip else . end)
    | (if ((."default-address-pools" // []) | length) == 0
         then ."default-address-pools" = [{"base": $pool, "size": 24}] else . end)' <<<"$current")
  sudo mkdir -p "$(dirname "$DOCKER_CFG")"
  echo "$updated" | sudo tee "$DOCKER_CFG" >/dev/null
  sudo systemctl restart docker
}

do_sysbox() {
  local tmp
  if dpkg-query -W -f='${Status}' sysbox-ce 2>/dev/null | grep -qE 'half-configured|unpacked'; then
    sudo dpkg --configure sysbox-ce
  elif ! dpkg-query -W -f='${Status}' sysbox-ce 2>/dev/null | grep -q 'install ok installed'; then
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/$SYSBOX_DEB" \
      "https://github.com/nestybox/sysbox/releases/download/v$SYSBOX_VERSION/$SYSBOX_DEB"
    echo "$SYSBOX_SHA256  $tmp/$SYSBOX_DEB" | sha256sum -c --quiet -
    # apt reads local packages as the _apt user.
    chmod 755 "$tmp"
    chmod 644 "$tmp/$SYSBOX_DEB"
    sudo apt-get install -y -q "$tmp/$SYSBOX_DEB"
    rm -rf "$tmp"
  fi
  for _ in $(seq 30); do
    sysbox_registered && return 0
    sleep 1
  done
  die "sysbox-runc is not registered in Docker; see: systemctl status sysbox"
}

do_tasks_dir() {
  sudo mkdir -p "$TASKS_DIR"
  sudo chown "$USER:$(id -gn)" "$TASKS_DIR"
  sudo chmod 750 "$TASKS_DIR"
}

do_mount() {
  [[ -d "$TASKS_DIR" ]] || do_tasks_dir
  grep -qxF "$FSTAB_LINE" /etc/fstab || {
    sudo cp -a /etc/fstab "/etc/fstab.bak-$(date +%Y%m%d-%H%M%S)"
    echo "$FSTAB_LINE" | sudo tee -a /etc/fstab >/dev/null
    sudo systemctl daemon-reload
  }
  if findmnt -n --mountpoint "$TASKS_DIR" >/dev/null; then
    sudo mount -o remount,bind,nosuid,nodev "$TASKS_DIR"
  else
    sudo mount "$TASKS_DIR"
  fi
  mount_ok || die "$TASKS_DIR is mounted without nosuid,nodev"
}

do_helper() {
  sudo install -d -o root -g root -m 755 "$(dirname "$HELPER")"
  sudo install -o root -g root -m 755 "$HERE/vivibox-netns" "$HELPER"
}

do_sudoers() {
  local tmp
  tmp=$(mktemp)
  echo "$SUDO_RULE" >"$tmp"
  sudo visudo -cqf "$tmp" || { rm -f "$tmp"; die "generated sudo rule is invalid"; }
  sudo install -o root -g root -m 440 "$tmp" "$SUDOERS"
  rm -f "$tmp"
}

# --- apply ------------------------------------------------------------------------------------

if ((${#todo[@]} == 0)); then
  echo "Nothing to do."
  exit 0
fi
$CHECK_ONLY && exit 1

running=$(docker ps -q | wc -l)
echo
echo "Changes to make:"
printf '  - %s\n' "${todo[@]}"
[[ " ${actions[*]} " == *" do_docker_networks "* || " ${actions[*]} " == *" do_sysbox "* ]] \
  && echo "Docker restarts; running containers: $running (stopped containers are kept)."
read -r -p "Continue? [y/N] " answer
[[ "$answer" == [yY] ]] || exit 1

for action in "${actions[@]}"; do
  "$action"
done
echo "Done. Run '$0 --check' to confirm."
