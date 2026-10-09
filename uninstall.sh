#!/bin/sh
# Saca Arxiv Times del arranque automático. No borra tus datos.
LABEL="local.arxiv-times"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Arxiv Times ya no arranca solo."
echo "Tus datos siguen en ~/.arxiv_times.json y ~/.arxiv_times_ai (borralos a mano si no los querés)."
