#!/usr/bin/env bash
# Starts Xvfb + x11vnc + noVNC websockify on port 6081.
# Safe to re-run on every container start — kills prior instances via PID files.
set -u

DISPLAY_NUM=99
VNC_PORT=5900
NOVNC_PORT=6081

XVFB_PID=/tmp/xvfb.pid
X11VNC_PID=/tmp/x11vnc.pid
NOVNC_PID=/tmp/novnc.pid

# Kill prior instances
for pidfile in "$XVFB_PID" "$X11VNC_PID" "$NOVNC_PID"; do
  if [ -f "$pidfile" ]; then
    old=$(cat "$pidfile" 2>/dev/null || echo "")
    if [ -n "$old" ] && [ -d "/proc/$old" ]; then
      kill "$old" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  fi
done
sleep 1

# Start Xvfb
nohup Xvfb :${DISPLAY_NUM} -screen 0 1920x1080x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
echo $! > "$XVFB_PID"
disown 2>/dev/null || true
sleep 1

# Start x11vnc — unset WAYLAND_DISPLAY so x11vnc targets Xvfb on :${DISPLAY_NUM}, not Wayland
nohup env -u WAYLAND_DISPLAY x11vnc -display :${DISPLAY_NUM} -forever -nopw -quiet -rfbport ${VNC_PORT} >/tmp/x11vnc.log 2>&1 &
echo $! > "$X11VNC_PID"
disown 2>/dev/null || true
sleep 0.5

# Start noVNC websockify (web files at /usr/share/novnc/)
nohup websockify --web=/usr/share/novnc/ --wrap-mode=ignore ${NOVNC_PORT} localhost:${VNC_PORT} >/tmp/novnc.log 2>&1 &
echo $! > "$NOVNC_PID"
disown 2>/dev/null || true

echo "noVNC ready: http://localhost:${NOVNC_PORT}/vnc.html"
