#!/usr/bin/env bash
# Detached launcher for the production pack.
#
# Why this file exists: run.sh is an hours-long job, so it MUST be detached —
# otherwise a Hermes gateway restart kills a half-rendered film. Hermes'
# terminal tool refuses inline setsid/nohup in the command string, so the
# detach lives in this script and is invoked as a plain foreground command.
set -uo pipefail

cd /root/production_pack

RUN_DIR_ARG="${1:-/root/production_pack/output/run_20260921_185227}"

# keep a copy of the previous console log so the resume is easy to read
if [ -s output/run_latest_console.log ]; then
    cp output/run_latest_console.log \
       "output/run_latest_console.log.before-resume-$(date +%Y%m%d_%H%M%S)"
fi

setsid nohup bash run.sh --resume "$RUN_DIR_ARG" \
    >> output/run_latest_console.log 2>&1 < /dev/null &

echo $! > output/run_latest.launch.pid
echo "launched pid $(cat output/run_latest.launch.pid) (resume $RUN_DIR_ARG)"
