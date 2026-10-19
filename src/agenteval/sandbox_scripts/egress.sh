#!/bin/sh
# agenteval egress policy for the agent user (POSIX sh, runs as root).
#
#   egress.sh apply allow|deny (--user NAME | --uid N)
#   egress.sh status
#
# deny: an nftables output rule rejects NEW connections from the agent uid that
#       leave through any interface other than loopback, with ICMP
#       admin-prohibited (connect() fails with EHOSTUNREACH/EPERM, so a policy
#       block is distinguishable from a closed port's ECONNREFUSED). Loopback
#       stays open (Inspect model bridge, local collector) and connections opened
#       by other users are untouched (SSH from the host, root setup). Established
#       flows are never matched, so the SSH session that runs this keeps working.
#       Best effort on top of that:
#       - resolver guard: the local DNS resolver (systemd-resolved) may not send
#         DNS/LLMNR/mDNS off the box, so the agent cannot exfiltrate through
#         lookups it asks the resolver to forward (stub, D-Bus or varlink).
#         Counted separately as dns_rejected.
#       - attribution: destinations (addr:port/proto) of rejected agent
#         connections are recorded in a bounded set and reported.
# allow: no rule (any earlier agenteval rule is removed).
#
# Skips gracefully: when the network namespace has no non-loopback interface that
# is up the policy already holds; when nft is unavailable or fails the result
# says so. Prints one JSON object. Exit code is 0 unless the usage is wrong.
#
# Test overrides: AGENTEVAL_STATE_DIR, AGENTEVAL_SYS_NET, AGENTEVAL_NFT,
# AGENTEVAL_RESOLVER_USER.

set -u

STATE_DIR="${AGENTEVAL_STATE_DIR:-/var/lib/agenteval}"
SYS_NET="${AGENTEVAL_SYS_NET:-/sys/class/net}"
NFT="${AGENTEVAL_NFT:-nft}"
RESOLVER_USER="${AGENTEVAL_RESOLVER_USER:-systemd-resolve}"
STATE_FILE="$STATE_DIR/egress.state"
TABLE="agenteval"
REJECT="reject with icmpx type admin-prohibited"
MAX_DESTINATIONS=50
# resolver guard: on | failed | no-resolver | n/a
RESOLVER_GUARD="n/a"

usage() {
    echo "usage: egress.sh apply allow|deny (--user NAME | --uid N) | egress.sh status" >&2
    exit 2
}

json_str() {
    # minimal JSON string escaping: drop quotes/backslashes/control chars
    printf '"%s"' "$(printf '%s' "$1" | tr -d '"\\' | tr '\n\t\r' '   ' | cut -c1-300)"
}

have_nft() {
    command -v "$NFT" >/dev/null 2>&1
}

iface_up() {
    # iface_up DIR: false only when the flags are readable and lack IFF_UP
    flags=$(cat "$1/flags" 2>/dev/null) || return 0
    case "$flags" in
        0x*) ;;
        *) return 0 ;;
    esac
    case "${flags#0x}" in
        '' | *[!0-9a-fA-F]*) return 0 ;;
    esac
    [ $((flags & 1)) -eq 1 ]
}

external_ifaces() {
    # Real interfaces are directories (symlinks to them). Skip plain files such
    # as bonding_masters and the down fallback tunnels every netns carries.
    [ -d "$SYS_NET" ] || return 0
    for path in "$SYS_NET"/*; do
        [ -d "$path" ] || continue
        name=$(basename "$path")
        [ "$name" = "lo" ] && continue
        iface_up "$path" || continue
        printf '%s ' "$name"
    done
}

table_dump() {
    have_nft || return 0
    "$NFT" list table inet "$TABLE" 2>/dev/null || true
}

rule_packets() {
    # rule_packets DUMP TAG: packets counted by the rule commented "agenteval:TAG"
    n=$(printf '%s\n' "$1" | grep -F "comment \"agenteval:$2\"" |
        sed -n 's/.*counter packets \([0-9][0-9]*\).*/\1/p' | head -n 1)
    echo "${n:-0}"
}

set_elements() {
    # set_elements SET: one "addr . proto . port" element per line
    have_nft || return 0
    out=$("$NFT" list set inet "$TABLE" "$1" 2>/dev/null) || return 0
    printf '%s\n' "$out" | tr '\n\t' '  ' |
        sed -n 's/.*elements = {\([^}]*\)}.*/\1/p' | tr ',' '\n'
}

destinations_json() {
    {
        set_elements agent_dst4 | awk -F ' [.] ' 'NF == 3 {
            for (i = 1; i <= 3; i++) gsub(/ /, "", $i)
            printf "%s:%s/%s\n", $1, $3, $2 }'
        set_elements agent_dst6 | awk -F ' [.] ' 'NF == 3 {
            for (i = 1; i <= 3; i++) gsub(/ /, "", $i)
            printf "[%s]:%s/%s\n", $1, $3, $2 }'
    } | head -n "$MAX_DESTINATIONS" | awk '
        BEGIN { printf "[" }
        { gsub(/[^0-9A-Za-z.:\[\]\/_-]/, ""); printf "%s\"%s\"", (NR > 1 ? "," : ""), $0 }
        END { printf "]" }'
}

emit() {
    # emit POLICY EFFECTIVE METHOD UID REASON
    dump=$(table_dump)
    printf '{"policy":%s,"effective":%s,"method":%s,"uid":%s,"reason":%s,' \
        "$(json_str "$1")" "$2" "$(json_str "$3")" "${4:-null}" "$(json_str "$5")"
    printf '"rejected_packets":%s,"resolver_guard":%s,"dns_rejected":%s,"destinations":%s}\n' \
        "$(rule_packets "$dump" agent)" "$(json_str "$RESOLVER_GUARD")" \
        "$(rule_packets "$dump" dns)" "$(destinations_json)"
}

save_state() {
    mkdir -p "$STATE_DIR" 2>/dev/null
    umask 077
    {
        echo "POLICY='$1'"
        echo "EFFECTIVE='$2'"
        echo "METHOD='$3'"
        echo "UID_='$4'"
        printf "REASON='%s'\n" "$(printf '%s' "$5" | tr -d "'" | tr '\n' ' ')"
        echo "RESOLVER_GUARD='$RESOLVER_GUARD'"
    } >"$STATE_FILE"
}

finish() {
    save_state "$@"
    emit "$@"
    exit 0
}

ruleset() {
    # ruleset UID WITH_CT
    ct=""
    [ "$2" = "yes" ] && ct="ct state new "
    cat <<EOF
add table inet $TABLE
delete table inet $TABLE
table inet $TABLE {
    chain record {
    }
    chain output {
        type filter hook output priority 0; policy accept;
        oifname "lo" accept
        meta skuid $1 ${ct}jump record
        meta skuid $1 ${ct}counter $REJECT comment "agenteval:agent"
    }
}
EOF
}

attribution_rules() {
    # destinations of rejected agent connections, in bounded dynamic sets
    cat <<EOF
add set inet $TABLE agent_dst4 { type ipv4_addr . inet_proto . inet_service; flags dynamic; size $MAX_DESTINATIONS; }
add set inet $TABLE agent_dst6 { type ipv6_addr . inet_proto . inet_service; flags dynamic; size $MAX_DESTINATIONS; }
add rule inet $TABLE record meta nfproto ipv4 meta l4proto { tcp, udp } add @agent_dst4 { ip daddr . meta l4proto . th dport }
add rule inet $TABLE record meta nfproto ipv6 meta l4proto { tcp, udp } add @agent_dst6 { ip6 daddr . meta l4proto . th dport }
EOF
}

resolver_rule() {
    # resolver_rule UID: the resolver may not forward lookups off the box
    cat <<EOF
add rule inet $TABLE output meta skuid $1 meta l4proto { tcp, udp } th dport { 53, 853, 5353, 5355 } counter $REJECT comment "agenteval:dns"
EOF
}

apply_extras() {
    # apply_extras AGENT_UID: best effort, the agent rule is already in place
    attribution_rules | "$NFT" -f - >/dev/null 2>&1 || true
    ruid=$(id -u "$RESOLVER_USER" 2>/dev/null) || ruid=""
    case "$ruid" in
        '' | *[!0-9]*) RESOLVER_GUARD="no-resolver" ;;
        *)
            if [ "$ruid" = "$1" ]; then
                RESOLVER_GUARD="on"  # the agent rule already rejects it
            elif resolver_rule "$ruid" | "$NFT" -f - >/dev/null 2>&1; then
                RESOLVER_GUARD="on"
            else
                RESOLVER_GUARD="failed"
            fi
            ;;
    esac
}

apply_policy() {
    policy="$1"
    uid="$2"
    case "$policy" in
        allow)
            if have_nft; then
                "$NFT" delete table inet "$TABLE" >/dev/null 2>&1 || true
            fi
            finish allow true none "$uid" "egress allowed, no rule installed"
            ;;
        deny)
            ifaces=$(external_ifaces)
            if [ -z "$ifaces" ]; then
                finish deny true no-external-interface "$uid" \
                    "network namespace has no non-loopback interface that is up; egress already impossible"
            fi
            if ! have_nft; then
                finish deny false none "$uid" "nft not available; egress NOT restricted"
            fi
            if err=$(ruleset "$uid" yes | "$NFT" -f - 2>&1); then
                apply_extras "$uid"
                finish deny true nftables "$uid" "reject new non-loopback connections from uid $uid"
            fi
            # conntrack unavailable: fall back to matching every non-loopback packet
            if err2=$(ruleset "$uid" no | "$NFT" -f - 2>&1); then
                apply_extras "$uid"
                finish deny true nftables "$uid" "reject non-loopback packets from uid $uid (no conntrack: $err)"
            fi
            finish deny false none "$uid" "nft failed: $err2"
            ;;
        *)
            usage
            ;;
    esac
}

status() {
    if [ -f "$STATE_FILE" ]; then
        POLICY="" EFFECTIVE="false" METHOD="" UID_="" REASON="" RESOLVER_GUARD="n/a"
        # shellcheck disable=SC1090
        . "$STATE_FILE"
        emit "$POLICY" "$EFFECTIVE" "$METHOD" "$UID_" "$REASON"
    else
        emit unknown false none "" "egress policy was never applied"
    fi
    exit 0
}

[ $# -ge 1 ] || usage
cmd="$1"
shift
case "$cmd" in
    apply)
        [ $# -ge 1 ] || usage
        policy="$1"
        shift
        uid=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --user)
                    [ $# -ge 2 ] || usage
                    uid=$(id -u "$2" 2>/dev/null) || {
                        echo "egress.sh: unknown user $2" >&2
                        exit 2
                    }
                    shift 2
                    ;;
                --uid)
                    [ $# -ge 2 ] || usage
                    uid="$2"
                    shift 2
                    ;;
                *)
                    usage
                    ;;
            esac
        done
        case "$uid" in
            '' | *[!0-9]*) usage ;;
        esac
        apply_policy "$policy" "$uid"
        ;;
    status)
        status
        ;;
    *)
        usage
        ;;
esac
