# shellcheck source=/dev/null
source /useremain/rinkhals/.current/tools.sh

APP_ROOT=$(dirname "$(realpath "$0")")

case "$1" in
    status)
        if [ -f /tmp/nozzle-purge.pid ] && kill -0 "$(cat /tmp/nozzle-purge.pid)" 2>/dev/null; then
            report_status $APP_STATUS_STARTED
        else
            report_status $APP_STATUS_STOPPED
        fi
        ;;
    start)
        python3 "$APP_ROOT/nozzle_purge.py" >> "$APP_ROOT/nozzle-purge.log" 2>&1 &
        echo $! > /tmp/nozzle-purge.pid
        ;;
    stop)
        [ -f /tmp/nozzle-purge.pid ] && kill "$(cat /tmp/nozzle-purge.pid)" 2>/dev/null
        rm -f /tmp/nozzle-purge.pid
        ;;
    *)
        echo "Usage: $0 {status|start|stop}" >&2
        exit 1
        ;;
esac
