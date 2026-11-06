#!/usr/bin/env bash
set -u
export PYTHONPATH="$PWD:$PYTHONPATH"
exec python3 -m unittest discover -s check -p "hidden_*.py"
