#\!/bin/bash
DIR="$HOME/srv6-portal/srv6-migration-portal"
pkill -f "python3 -u server.py" 2>/dev/null
sleep 1
cd "$DIR"
setsid python3 -u server.py > "$DIR/portal.log" 2>&1 < /dev/null &
sleep 2
echo "portal started on http://0.0.0.0:8077 (see $DIR/portal.log)"
