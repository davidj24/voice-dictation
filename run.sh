#!/usr/bin/env bash
# Starts the dictation daemon with the CUDA libraries from the local venv.
cd "$(dirname "$0")"
SP=$(.venv/bin/python -c "import nvidia, os; print(nvidia.__path__[0])")
export LD_LIBRARY_PATH="$SP/cublas/lib:$SP/cudnn/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HF_HUB_OFFLINE=1
exec .venv/bin/python -u dictate.py
