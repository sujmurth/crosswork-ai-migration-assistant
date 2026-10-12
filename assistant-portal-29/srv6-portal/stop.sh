#\!/bin/bash
pkill -f "python3 -u server.py" 2>/dev/null && echo stopped || echo "not running"
