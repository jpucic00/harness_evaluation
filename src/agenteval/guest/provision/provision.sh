#!/usr/bin/env bash
# Provision an Ubuntu 24.04 environment for agenteval. Runs as root and is idempotent.
#
# Used by both providers:
#   - Docker: RUN in the image build (no systemd, no AppArmor, /etc/hosts is not persisted)
#   - Tart:   run once over SSH with sudo while building the base VM image
#
# Optional environment:
#   AGENTEVAL_AGENT_USER  non-root user the agent runs as   (default: agent)
#   AGENTEVAL_WORKSPACE   agent working directory          (default: /workspace)
#
# Writes /etc/agenteval/image.json (agent_user, workspace, provision_sha256) so a
# built image can be matched to the profile and script that produced it.
set -euo pipefail

AGENT_USER="${AGENTEVAL_AGENT_USER:-agent}"
WORKSPACE="${AGENTEVAL_WORKSPACE:-/workspace}"
AGENT_UID=1001
COLLECTOR_HOST="collector.agenteval.test"
BWRAP_GROUP="agenteval-bwrap"   # constants.BWRAP_GROUP
PACKAGES=(
  bubblewrap socat python3 python-is-python3 python3-pytest git curl ca-certificates nftables
  iproute2 procps sudo passwd
)

log() { printf '[provision] %s\n' "$*"; }
die() { printf '[provision] ERROR: %s\n' "$*" >&2; exit 1; }

# Run "$@", retrying while another apt/dpkg process (cloud-init, apt-daily) holds a lock
# it needs. DPkg::Lock::Timeout covers only the dpkg frontend lock: `apt-get update`
# fails at once on the lists lock, and dpkg-statoverride on the status database lock.
retry_on_lock() {
  local attempt out=""
  for attempt in $(seq 1 30); do
    if out="$("$@" 2>&1)"; then
      [ -z "$out" ] || printf '%s\n' "$out"
      return 0
    fi
    case "$out" in
      *"Could not get lock"* | *"Unable to lock"* | *"locked by another process"*)
        log "$1: locked by another process (attempt $attempt/30); retrying in 10s"
        sleep 10
        ;;
      *) break ;;
    esac
  done
  printf '%s\n' "$out" >&2
  return 1
}

[ "$(id -u)" -eq 0 ] || die "must run as root"
[[ "$AGENT_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid agent user: $AGENT_USER"
[ "$AGENT_USER" != root ] || die "agent user must not be root"
[[ "$WORKSPACE" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "invalid workspace path: $WORKSPACE"
case "$WORKSPACE" in
  / | /bin* | /boot* | /dev* | /etc* | /lib* | /proc* | /root* | /sbin* | /sys* | /usr* | /var*)
    die "workspace must not be a system directory: $WORKSPACE" ;;
esac

# --------------------------------------------------------------------------- background jobs

# In a VM (systemd is PID 1), stop the periodic package jobs: they would race the apt
# calls below for the lists lock and, in every sample clone, could download or install
# packages as root (root traffic is not covered by the agent egress rule), making
# samples differ and adding CPU noise. Masking a service does not stop a running
# instance, so an apt run already in progress finishes; retry_on_lock waits for it.
if [ -d /run/systemd/system ]; then
  for unit in apt-daily.timer apt-daily-upgrade.timer motd-news.timer snapd.snap-repair.timer \
      unattended-upgrades.service; do
    systemctl mask --now "$unit" >/dev/null 2>&1 || true
  done
  systemctl mask apt-daily.service apt-daily-upgrade.service >/dev/null 2>&1 || true
  if command -v snap >/dev/null; then
    snap refresh --hold=forever >/dev/null 2>&1 || true
  fi
  log "disabled periodic apt/motd/snap jobs"
fi

# --------------------------------------------------------------------------- packages

missing=()
for pkg in "${PACKAGES[@]}"; do
  if [ "$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null)" != "install ok installed" ]; then
    missing+=("$pkg")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  log "installing: ${missing[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt_opts=(-o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confold)
  retry_on_lock apt-get "${apt_opts[@]}" update -q
  apt-get "${apt_opts[@]}" install -y -q --no-install-recommends "${missing[@]}"
  apt-get clean
  rm -rf /var/lib/apt/lists/*
else
  log "packages already installed"
fi

# --------------------------------------------------------------------------- agent user

if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
  useradd_args=(-m -d "/home/$AGENT_USER" -s /bin/bash)
  if ! getent passwd "$AGENT_UID" >/dev/null; then
    useradd_args+=(-u "$AGENT_UID")
  fi
  if getent group "$AGENT_USER" >/dev/null; then
    useradd_args+=(-g "$AGENT_USER")
  else
    useradd_args+=(-U)
  fi
  log "creating user $AGENT_USER"
  useradd "${useradd_args[@]}" "$AGENT_USER"
fi
AGENT_HOME="$(getent passwd "$AGENT_USER" | cut -d: -f6)"
AGENT_GROUP="$(id -gn "$AGENT_USER")"
[ "$(getent passwd "$AGENT_USER" | cut -d: -f7)" = /bin/bash ] || usermod -s /bin/bash "$AGENT_USER"
install -d -o "$AGENT_USER" -g "$AGENT_GROUP" -m 0750 "$AGENT_HOME"

# No sudo or other administrative access: no password, no admin groups, no sudoers rules.
passwd -l "$AGENT_USER" >/dev/null
for group in sudo admin wheel adm docker lxd "$BWRAP_GROUP"; do
  if id -nG "$AGENT_USER" | tr ' ' '\n' | grep -qx "$group"; then
    log "removing $AGENT_USER from group $group"
    gpasswd -d "$AGENT_USER" "$group" >/dev/null
  fi
done
if grep -rlsE "^[[:space:]]*${AGENT_USER}[[:space:]]" /etc/sudoers /etc/sudoers.d >/dev/null; then
  die "sudoers grants rules to $AGENT_USER; remove them first"
fi

# --------------------------------------------------------------------------- directories

install -d -o "$AGENT_USER" -g "$AGENT_GROUP" -m 0755 "$WORKSPACE"
install -d -o root -g root -m 0755 /opt/agenteval
install -d -o root -g root -m 0700 /var/lib/agenteval
install -d -o root -g root -m 0755 /etc/agenteval
install -d -o root -g root -m 0755 /etc/agenteval/secrets

# --------------------------------------------------------------------------- collector name

# Docker regenerates /etc/hosts for every container (the compose file adds the entry
# via extra_hosts); in a VM the file persists. cloud-init may rewrite it from a template.
hosts_line="127.0.0.1 $COLLECTOR_HOST"
for hosts_file in /etc/hosts /etc/cloud/templates/hosts.debian.tmpl; do
  [ -f "$hosts_file" ] || continue
  if ! awk -v h="$COLLECTOR_HOST" '$1 == "127.0.0.1" { for (i = 2; i <= NF; i++) if ($i == h) f = 1 }
      END { exit !f }' "$hosts_file"; then
    if { printf '%s\n' "$hosts_line" >>"$hosts_file"; } 2>/dev/null; then
      log "added '$hosts_line' to $hosts_file"
    else
      log "could not write $hosts_file (expected during a docker build)"
    fi
  fi
done

# --------------------------------------------------------------------------- bubblewrap

# Only root and members of $BWRAP_GROUP may run bwrap. The agent user is added to the
# group per sample, and only in cells whose harness uses the bwrap inner sandbox, so
# agents in other cells cannot use it to get user namespaces. dpkg-statoverride keeps
# the owner and mode across bubblewrap package upgrades.
groupadd -f -r "$BWRAP_GROUP"
BWRAP=/usr/bin/bwrap
if [ -e "$BWRAP" ]; then
  current="$(dpkg-statoverride --list "$BWRAP" || true)"   # mode printed without leading 0
  if [ "$current" != "root $BWRAP_GROUP 750 $BWRAP" ]; then
    [ -z "$current" ] || retry_on_lock dpkg-statoverride --remove "$BWRAP"
    retry_on_lock dpkg-statoverride --update --add root "$BWRAP_GROUP" 0750 "$BWRAP"
    log "restricted $BWRAP to root:$BWRAP_GROUP mode 0750"
  fi
fi

# --------------------------------------------------------------------------- AppArmor

# Ubuntu 24.04 blocks unprivileged user namespaces unless the binary has an AppArmor
# profile granting `userns`. bubblewrap needs them.
#
# Trade-off: the profile below is unconfined apart from granting `userns`, so whoever
# can run /usr/bin/bwrap gets a user namespace with full capabilities inside it (and
# the kernel attack surface that opens, e.g. nf_tables), which stock Ubuntu 24.04
# denies to unprivileged users. The profile attaches by path, so copies of the binary
# elsewhere stay restricted, and the 0750 root:$BWRAP_GROUP mode above limits it to
# root and group members (the agent only in bwrap cells). A child profile that drops
# capabilities was not used because root-mode bwrap sandboxes need theirs.
apparmor_userns_restricted() {
  [ "$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null)" = Y ] &&
    [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ] &&
    [ -d /etc/apparmor.d ] && command -v apparmor_parser >/dev/null
}
BWRAP_PROFILE=/etc/apparmor.d/agenteval-bwrap
if apparmor_userns_restricted; then
  other="$(grep -lsE '^[^#]*profile[[:space:]]+[^[:space:]]+[[:space:]]+/usr/bin/bwrap' \
    /etc/apparmor.d/* 2>/dev/null | grep -vx "$BWRAP_PROFILE" || true)"
  if [ -n "$other" ]; then
    log "AppArmor profile for /usr/bin/bwrap already provided by: $other"
  else
    cat >"$BWRAP_PROFILE.tmp" <<'EOF'
# Managed by agenteval provision.sh: let bubblewrap create user namespaces.
abi <abi/4.0>,
include <tunables/global>

profile agenteval-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,

  include if exists <local/agenteval-bwrap>
}
EOF
    if cmp -s "$BWRAP_PROFILE.tmp" "$BWRAP_PROFILE"; then
      rm -f "$BWRAP_PROFILE.tmp"
    else
      mv "$BWRAP_PROFILE.tmp" "$BWRAP_PROFILE"
      chmod 0644 "$BWRAP_PROFILE"
      log "wrote $BWRAP_PROFILE"
    fi
    apparmor_parser -r -W "$BWRAP_PROFILE"
    log "loaded AppArmor profile agenteval-bwrap"
  fi
else
  log "AppArmor userns restriction not present; skipping bwrap profile"
fi

# --------------------------------------------------------------------------- git

git config --system --replace-all safe.directory '*'
git config --system user.name "agenteval"
git config --system user.email "agent@agenteval.test"
git config --system init.defaultBranch main
git config --system advice.detachedHead false

# --------------------------------------------------------------------------- image identity

script_path="${BASH_SOURCE[0]:-}"
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
  provision_sha256="\"$(sha256sum "$script_path" | cut -d' ' -f1)\""
else
  provision_sha256=null   # script read from stdin
fi
# Values are validated above, so they need no JSON escaping.
printf '{"agent_user": "%s", "workspace": "%s", "provision_sha256": %s}\n' \
  "$AGENT_USER" "$WORKSPACE" "$provision_sha256" >/etc/agenteval/image.json.tmp
chown root:root /etc/agenteval/image.json.tmp
chmod 0644 /etc/agenteval/image.json.tmp
mv -f /etc/agenteval/image.json.tmp /etc/agenteval/image.json

log "done (user=$AGENT_USER uid=$(id -u "$AGENT_USER") home=$AGENT_HOME workspace=$WORKSPACE)"
