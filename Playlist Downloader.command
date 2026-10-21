#!/bin/bash
# Double-click to run. Creates a virtualenv on first run and keeps dependencies up to date.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
exec .venv/bin/python app.py
