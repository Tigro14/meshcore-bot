#!/bin/bash
# Preserve systemd enablement and activity across Debian package upgrades.

set -e

STATE_DIR="${MESHCORE_MAINT_STATE_DIR:-/run}"
SERVICE_NAME="${MESHCORE_MAINT_SERVICE_NAME:-meshcore-bot.service}"
ACTIVE_MARKER="${STATE_DIR}/meshcore-bot-was-active"
ENABLED_MARKER="${STATE_DIR}/meshcore-bot-was-enabled"
POLICY_MARKER="${MESHCORE_MAINT_POLICY_MARKER:-/opt/meshcore-bot/.service-state-policy-v1}"

preinstall() {
    local action="${1:-}"
    # Debian invokes old-prerm before new-preinst.  Once the hardened policy is
    # installed, old-prerm has already written the markers and preinst must
    # preserve them.  A fresh install or first transition clears stale state.
    if [ "${action}" != "upgrade" ] || [ ! -f "${POLICY_MARKER}" ]; then
        rm -f "${ACTIVE_MARKER}" "${ENABLED_MARKER}"
    fi
}

preremove() {
    local action="${1:-}"
    command -v systemctl >/dev/null 2>&1 || return 0

    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        if [ "${action}" = "upgrade" ]; then
            touch "${ACTIVE_MARKER}"
        fi
        systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    fi
    if [ "${action}" = "upgrade" ] && systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
        touch "${ENABLED_MARKER}"
    elif [ "${action}" != "upgrade" ]; then
        systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    fi
}

restore() {
    local previous_version="${1:-}"
    command -v systemctl >/dev/null 2>&1 || return 0
    local first_hardened_upgrade=false
    if [ -n "${previous_version}" ] && [ ! -f "${POLICY_MARKER}" ]; then
        first_hardened_upgrade=true
    fi

    systemctl daemon-reload
    if [ "${first_hardened_upgrade}" = true ] || [ -z "${previous_version}" ] || [ -f "${ENABLED_MARKER}" ]; then
        systemctl enable "${SERVICE_NAME}" || true
    else
        systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    fi

    if [ "${first_hardened_upgrade}" = true ] || [ -f "${ACTIVE_MARKER}" ]; then
        systemctl start "${SERVICE_NAME}"
        echo "Upgraded service restarted."
    else
        echo "Service installed but not started."
    fi
    rm -f "${ACTIVE_MARKER}" "${ENABLED_MARKER}"
    touch "${POLICY_MARKER}"
    chmod 0600 "${POLICY_MARKER}"
}

case "$(basename "$0")" in
    preinst)
        preinstall "${1:-}"
        ;;
    *)
        operation="${1:-}"
        shift || true
        case "${operation}" in
            preinstall) preinstall "${1:-}" ;;
            preremove) preremove "${1:-}" ;;
            restore) restore "${1:-}" ;;
            *) echo "Usage: $0 {preinstall|preremove|restore} [dpkg-argument]" >&2; exit 2 ;;
        esac
        ;;
esac
