"""
stubs/ping_world.py — network check for the hotspot test (D3). Run on any laptop:
    python stubs/ping_world.py --world ws://<world-laptop-ip>:8765
Prints HELLO (scenario + aircraft) and how many TRUTH frames arrive in 5 s (expect ~50).
"""
import argparse, asyncio, json, time
import websockets

async def main(world):
    t0 = time.time()
    async with websockets.connect(f"{world}?role=god", open_timeout=5) as ws:
        hello = json.loads(await ws.recv())
        print(f"connected in {1000*(time.time()-t0):.0f} ms -> {hello.get('type')} scenario={hello.get('scenario')} "
              f"aircraft={[a['id'] for a in hello.get('aircraft', [])]}")
        n, end = 0, time.time() + 5
        while time.time() < end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time()))
                n += m.get("type") == "TRUTH"
            except asyncio.TimeoutError:
                break
        print(f"TRUTH frames in 5 s: {n}  ({'OK' if n >= 40 else 'LOW - check Wi-Fi / firewall'})")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--world", required=True)
    try:
        asyncio.run(main(ap.parse_args().world))
    except Exception as e:
        print("FAILED:", type(e).__name__, e)
        print("-> same hotspot? world laptop firewall allowed Python on Private networks? right IP (ipconfig)?")
