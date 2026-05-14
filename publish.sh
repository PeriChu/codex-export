#!/usr/bin/env bash
set -euo pipefail

python3 -m build
python3 -m twine check dist/*
