#!/usr/bin/env bash
# Keep only this project's WSL session alive; never signal arbitrary processes.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'The project WSL keepalive requires root.' >&2
    exit 1
fi

lock_file=/run/lock/agent-backend-aegra.keepalive
stop_file=/run/lock/agent-backend-aegra.keepalive.stop
umask 077
mkdir -p /run/lock
exec 9>"$lock_file"

case "${1:-}" in
    hold)
        # A second hidden helper exits immediately, leaving the first intact.
        flock -n 9 || exit 0
        rm -f -- "$stop_file"
        trap 'rm -f -- "$stop_file"' EXIT
        trap 'exit 0' INT TERM
        while [ ! -e "$stop_file" ]; do
            sleep 1
        done
        ;;
    status)
        # Lock contention means the project's foreground keeper is alive.
        if flock -n 9; then exit 1; else exit 0; fi
        ;;
    stop)
        if flock -n 9; then exit 0; fi
        : >"$stop_file"
        for _ in $(seq 1 40); do
            if flock -n 9; then exit 0; fi
            sleep 0.25
        done
        printf '%s\n' 'The project WSL keepalive did not release its lock.' >&2
        exit 1
        ;;
    *)
        printf '%s\n' 'Usage: wsl_keepalive.sh hold|status|stop' >&2
        exit 2
        ;;
esac
