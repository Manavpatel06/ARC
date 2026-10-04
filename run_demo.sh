#!/usr/bin/env bash
# run_demo.sh — macOS/Linux version of run_demo.ps1 (same order, logs to harness/out/run_*.log).
#   ./run_demo.sh [scenario.json] [--arc] [--no-verify] [--spoof | --spoof-mode=impossible] [--stubs] [--no-live]   ./run_demo.sh --stop
#   default: world (advisory only) + one verification unit per judge (verify/unit.py)
#   --arc (= --legacy): also radio channel + collision-avoidance nodes, world allows takeover
set -u
cd "$(dirname "$0")"
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
PIDS=.demo_pids; mkdir -p harness/out
if [ "${1:-}" = "--stop" ]; then [ -f $PIDS ] && xargs kill < $PIDS 2>/dev/null; rm -f $PIDS; echo "ARC demo stopped."; exit 0; fi
SC=harness/scenarios/judges.json; SPOOF=0; STUBS=0; NOLIVE=0; ARC=0; VERIFY=1
SPMODE=unsigned
for a in "$@"; do case $a in --spoof) SPOOF=1;; --spoof-mode=*) SPOOF=1; SPMODE=${a#*=};; --stubs) STUBS=1;; --no-live) NOLIVE=1;; --arc|--legacy) ARC=1;; --no-verify) VERIFY=0;; *.json) SC=$a;; esac; done
W=ws://localhost:8765; : > $PIDS
start() { name=$1; shift; "$PY" "$@" > "harness/out/run_${name// /_}.log" 2>&1 & echo $! >> $PIDS; echo "  started $name: $*"; }
$PY data/metar.py
TRAF=$($PY -c "import json;print(1 if json.load(open('$SC')).get('traffic') else 0)")
WARGS=(world/world_server.py --scenario "$SC" --http 8080)
[ $ARC = 1 ] && WARGS+=(--allow-takeover)
[ $ARC = 1 ] && [ "$TRAF" = 1 ] && [ $STUBS = 0 ] && WARGS+=(--traffic-nodes ws://localhost:8766 --pidfile $PIDS)   # live_kdvt: a node per AI aircraft
if [ -f world/world_server.py ]; then start world "${WARGS[@]}"
else start world-stub stubs/fake_world.py --scenario "$SC"; start web -m http.server 8080 -d web; fi
for i in $(seq 1 60); do (echo > /dev/tcp/127.0.0.1/8765) 2>/dev/null && break; sleep 0.25; done
JUDGES=$($PY -c "import json;print(' '.join(a['id'] for a in json.load(open('$SC'))['aircraft'] if a.get('human') and a.get('flock',True)))")
[ $VERIFY = 1 ] && for id in $JUDGES; do start "verify $id" verify/unit.py --id $id --world $W; done
if [ $ARC = 1 ]; then
  if [ -f radio/channel.py ] && [ $STUBS = 0 ]; then start channel radio/channel.py --world $W --loss 0.1 --latency 0.3
  else if [ $SPOOF = 1 ]; then start channel-stub stubs/fake_channel.py --world $W --spoof; else start channel-stub stubs/fake_channel.py --world $W; fi; fi
  IDS=$($PY -c "import json,sys;print(' '.join(a['id'] for a in json.load(open('$SC'))['aircraft'] if a.get('flock',True)))")
  if [ -f node/node.py ] && [ $STUBS = 0 ]; then sleep 2; for id in $IDS; do start "node $id" node/node.py --id $id --world $W --via-channel ws://localhost:8766; done
  else start node-stub stubs/fake_node.py --id N101 --world $W; fi
  [ $SPOOF = 1 ] && [ -f radio/spoofer.py ] && start spoofer radio/spoofer.py --mode $SPMODE --via-channel ws://localhost:8766
fi
[ $NOLIVE = 0 ] && start "live sky" data/live_traffic.py --world $W --record     # real ADS-B on god view + log
IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
echo "cockpit A http://$IP:8080/index.html?role=cockpitA | B ...role=cockpitB | god ...role=god | log http://$IP:8080/log.html"
echo "logs in harness/out/run_*.log   stop: ./run_demo.sh --stop"
