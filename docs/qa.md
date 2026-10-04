# Judge Q&A — likely questions, two-line answers

Numbers marked (sim) come from `python harness/montecarlo.py` / `python harness/accept_b.py`: simulated KDVT pattern encounters, 10 % packet loss, 0.3 s latency, pilots that react in 5 s and follow an advisory 70 % of the time. They show a timely intervention under that geometry; they are not a claim about any real accident.

1. **How is this different from TCAS?**
   TCAS is built for airliners (transponder interrogation, vertical RAs, ~$100k avionics). ARC is peer-to-peer for GA: every aircraft runs the same node, predicts pattern turns, and negotiates horizontal and vertical maneuvers aircraft-to-aircraft.

2. **Why not just use ADS-B?**
   ADS-B is a broadcast with no negotiation, many GA aircraft only receive it, and a position you cannot authenticate is a position you cannot act on. ARC signs every message and scores each target on evidence before it may trigger anything.

3. **What is "turn-aware" prediction and why does it matter?**
   Near an airport aircraft turn: a straight-line projection sees a base-to-final conflict only once the other aircraft has turned (about 43 s before it starts); we classify the pattern leg and predict the turn, so the same conflict shows at the full 90 s horizon (sim: median first-detection lead 51 s vs 37 s).

4. **What if the leg classifier is wrong?**
   Every classification carries a confidence. Below 0.6 the node falls back to straight-line with wider uncertainty, and a peer's declared leg and INTENT are used when present.

5. **When does ARC take control, and how far?**
   Only inside printed bounds, enforced by a separate small monitor: max bank 30°, speed floor 62 kt (1.3 Vs), no automatic action below 300 ft AGL on final, no descent below pattern altitude minus 300 ft, at most 10 s. Anything outside is clipped or rejected.

6. **What happens if the pilot disagrees?**
   Any stick input makes the world send STICK and the node issues RELEASE in the same event, then will not re-grab for 5 s. It announces the hand-back ("your aircraft, continue left turn").

7. **What if there is no safe maneuver?**
   It says so: "NO SAFE MANEUVER - YOUR AIRCRAFT", with every candidate and its rejection reason in the log (terrain floor, performance, authority bound, traffic). It never improvises outside the bounds.

8. **How do you avoid nuisance alerts?**
   A conflict needs a predicted miss under 500 ft horizontal and 100 ft vertical inside 90 s, after taking prediction uncertainty off. On benign encounters (spaced pattern traffic, 500+ ft vertical crossings, 2,000+ ft lateral passes) ARC alerted on 2 % (sim), with no unnecessary maneuvers.

9. **How do you know it works?**
   Monte Carlo against a straight-line + fixed right-turn baseline on the same 77 NMAC-prone encounters: NMAC rate 100 % with no avoidance, 18 % baseline, 6 % ARC; median miss 864 ft vs 1,150 ft (sim). Baseline and ARC share thresholds, radio and bounds, so the difference is turn-aware prediction plus escape/negotiation.

10. **What are the limits of those numbers?**
    It is our own simulation, with a nominal pattern model (real pilots vary), a 70 % compliance assumption, and one aircraft model (C172S-class). Out-of-sample validation on recorded KDVT ADS-B is planned, not done.

11. **How do two aircraft agree on a maneuver?**
    The lower ID commits first with the best sense; the higher ID answers with a complementary one (it pre-computes the lower's choice, so commits land within one radio latency). If the link is silent for 3 s both default to a right turn (14 CFR 91.113), tested with a radio blackout.

12. **What stops a fake aircraft from making you maneuver?**
    Messages are Ed25519-signed with replay protection; each target is scored on signature, kinematic plausibility, emulated RF consistency (RSSI/Doppler) and peer corroboration. Only TRUSTED targets (score 0.7+) can trigger RESOLVE or TAKEOVER; a FAKE one is shown to the pilot and never acted on.

13. **Can an attacker with many valid keys fool it?**
    A Sybil attacker with several valid keys and transmitters placed to fake geometry could corroborate itself. Registration-bound keys and RF checks from several receivers make that expensive, not impossible; we say so rather than claim otherwise.

14. **Does it work at 3 miles and in real radio conditions?**
    The channel emulator drops anything beyond 4,828 m, loses 10 % of packets, delays 0.3 s and models slot collisions; the node dead-reckons stale peer states and widens uncertainty with age. A real over-the-air test is not done.

15. **What does it cost and what would it take to fly?**
    ARC is software only: no new box. It runs on avionics the aircraft already has (ADS-B, the navigator / flight display, a digital autopilot) and is delivered as a software update through the avionics maker, sold as a per-aircraft subscription. The logic is deterministic and runs in about 1–2 ms per tick; certification and flight test are the real remaining work.

16. **"Software only" — so what hardware does it actually need?**
    Nothing new. Own position goes out on ADS-B Out (required since 2020 in the controlled airspace around busy airports); other aircraft come in on ADS-B In, which modern panel avionics and the portable receivers most pilots already use provide. The autopilot command goes through the digital autopilot's existing steering input (the same input the GPS navigator already uses to steer it). ARC installs as a software update on the panel avionics that already sit between the two. Aircraft with older avionics get the advisory-only version on the pilot's tablet with an ADS-B receiver they usually already own; no autopilot takeover there.

17. **What carries the negotiation if you add no radio?**
    ARC does not depend on the negotiation getting through. Both aircraft run the same deterministic rules (14 CFR 91.113 right-of-way, then the escape search) on the same shared ADS-B picture, so each can compute what the other will do and they reach complementary maneuvers without talking — like TCAS coordination done implicitly. Tested: radio blackout once the conflict is seen, both still separate by rule (917 ft, no NMAC, sim); head-on median ~850 ft from 0 % to 50 % packet loss, though about 1 run in 5 still ends inside 500 ft, so the link does help. When a data link exists (an avionics datalink, the cockpit's existing connectivity) ARC uses it to confirm and to sign messages.

18. **Then how does spoof detection work on plain ADS-B, which has no signatures?**
    Signatures are a bonus when a link exists; the core check is physics. The received signal strength must match the claimed distance (ADS-B receivers already measure it), the claimed motion must be flyable, and where a link exists other ARC aircraft that should hear the target must confirm it. A spoofed target fails within 1–4 s (sim) and is never allowed to move an airplane; anything not yet trusted gets a warning at most.

19. **What if the aircraft has no autopilot or no ADS-B In?**
    Then it is advisory-only (tablet app, voice + display), still with turn-aware prediction and right-of-way guidance. The autopilot layer is for the growing share of GA aircraft with modern digital autopilots, which is where new avionics are going.
