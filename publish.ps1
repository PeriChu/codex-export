$ErrorActionPreference = "Stop"

python -m build
python -m twine check dist/*
