@echo off
REM Clean stale VS Code Server installs from Docker Desktop WSL root (tiny 135MB FS)
wsl -d docker-desktop -- rm -rf /root/.vscode-remote-containers /root/.vscode-server /root/.vscode 2>nul
REM Capture git identity for post-create.sh
git config --global user.name > .devcontainer\.gituser.tmp 2>nul
git config --global user.email >> .devcontainer\.gituser.tmp 2>nul
exit /b 0
