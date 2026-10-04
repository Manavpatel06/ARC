# Running Lane B (Reya) — copy/paste

World laptop (Manav's hotspot): `192.168.137.1`   ·   this laptop: `192.168.137.142`
Run from the repo root. On Windows use your Python 3.13 (`C:\Users\reyaa\AppData\Local\Programs\Python\Python313\python.exe`) if plain `python` is broken.

```bash
git pull origin main
git checkout lane-b-node
pip install -r requirements.txt            # needs pynacl, or the node silently falls back to the stub radio

python stubs/ping_world.py --world ws://192.168.137.1:8765      # network check: expect HELLO + ~40-50 TRUTH frames in 5 s

# one per demo, any laptop (the radio "air"); needs the world running
python radio/channel.py --world ws://192.168.137.1:8765 --loss 0.1 --latency 0.3

# your node (use an id nobody else runs: N101, N102, N201..N204, N311, N399)
python node/node.py --id N101 --world ws://192.168.137.1:8765 -v

# if multicast is blocked on the hotspot, relay through the channel process
python node/node.py --id N101 --world ws://192.168.137.1:8765 --via-channel ws://192.168.137.1:8766 -v
# no channel at all (stub-like, no RF emulation)
python node/node.py --id N101 --world ws://192.168.137.1:8765 --direct -v
```

Checks and evidence (no network needed):

```bash
python -m pytest node/tests -q            # 25 tests
python harness/accept_b.py                # 1 PM / 4 PM / 7 PM rows + B12, ~60 s;  add --live to also run against stubs/fake_world.py
python harness/montecarlo.py --n 30       # chart -> harness/out/arc_vs_baseline.png (~3 min)
```

Spoof demo on top of the judges scenario (Mansi's spoofer): `python radio/spoofer.py`, then watch GHOST7 turn FAKE/SUSPICIOUS in the log page and never raise RESOLVE.
