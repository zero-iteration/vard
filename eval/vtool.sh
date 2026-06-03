#!/bin/bash
source .venv/bin/activate 2>/dev/null
python -m eval.agent_tools "$@" 2>/dev/null | grep -vE "Loading weights|HF_TOKEN|it/s|Warning|warn"
