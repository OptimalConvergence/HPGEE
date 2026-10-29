#!/bin/bash
set -euo pipefail
exec bash "${RSGENLM_WORKSPACE:-/nfs-data0/home/lyh/workspace/RSGenLM}/slurm/progress.sh" "$@"
