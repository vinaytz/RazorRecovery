#!/bin/sh
# Run the benchmark once, on first boot, if there are no results to show.
#
# A fresh clone has no results.json and no database -- both are gitignored, and
# they have to be, because results.json is generated output and the db is a
# 40MB binary. So a judge who runs `docker compose up` on a clean checkout would
# otherwise land on a dashboard that 404s with "no results yet". This makes the
# first boot slower by about ten seconds and correct instead.
#
# It is a first-boot check, not a rebuild: if /data already holds results, the
# benchmark does not run again. `docker compose down -v` drops the volume and
# gets you a fresh one.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
RESULTS="$DATA_DIR/results.json"

mkdir -p "$DATA_DIR"

# main.py reads results.json from the repo root and has no env override for it,
# so the file lives on the volume and the root gets a symlink. One line here
# beats an env var threaded through the app for the benefit of one deployment.
if [ ! -L /app/results.json ]; then
    ln -sf "$RESULTS" /app/results.json
fi

if [ ! -f "$RESULTS" ]; then
    echo "razorrecovery: no results at $RESULTS -- running the benchmark once."
    echo "razorrecovery: this takes about ten seconds. it will not run again"
    echo "razorrecovery: unless the /data volume is removed."
    # WORKER=off: the live worker would otherwise start ticking against the same
    # sqlite file while the benchmark is writing it.
    WORKER=off python run_benchmark.py --n 2000 --preset default --out "$RESULTS"
    echo "razorrecovery: benchmark done. the other five world presets are not"
    echo "razorrecovery: loaded -- for those, run:"
    echo "razorrecovery:   docker compose exec app python run_benchmark.py \\"
    echo "razorrecovery:       --all-presets --n 1500 --out /data/results_presets.json"
else
    echo "razorrecovery: results found at $RESULTS -- skipping the benchmark."
fi

exec "$@"
