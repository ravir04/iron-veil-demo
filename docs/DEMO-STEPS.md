# Iron-Veil Demo — Drone COP (10 min)

---

## Setup (before the room fills)

- Signet running at `http://localhost:4774` / Admin at `http://localhost:4775`
- Iron-Veil stack running (matrix-proxy :8009, catalog :8082)
- Iron-Veil Demo stack running (drone-sim internal, cop-ui :8090)
- Three COP-UI browser tabs open, each logged in as a different operator:

| Who | Browser Tab | Clearance |
|---|---|---|
| **Alice** | Tab 1 — `http://localhost:8090?user=alice` | SECRET — all nations |
| **Bob** | Tab 2 — `http://localhost:8090?user=bob` | SECRET — CAN only |
| **Dave** | Tab 3 — `http://localhost:8090?user=dave` | PROTECTED |

- Signet Admin audit log open: `http://localhost:4775/audit`
- drone-sim NOT yet running (start it live during demo)

---

## 1 — The Problem (1 min)

> "Coalition operations mean multiple nations, multiple classification levels, sharing a single picture. The traditional answer is separate networks — one per nation, one per classification. JTARS networks, SIPRNet, BICES. Every connection between them is a manual relay that introduces delay and classification risk."

> "The question is: can you run a single Drone COP — one screen, one data feed — and have it automatically show each operator exactly what they're cleared to see? Not by logging them into different servers. The same server. The same feed. Enforced cryptographically."

---

## 2 — The Operators (30 sec)

**[Show all three COP tabs side by side — all showing an empty map with the mission terrain]**

> "Alice is a US liaison officer with full FVEY access. Bob is a Canadian operator — SECRET clearance, but his releasability is limited to CAN. Dave is a British officer with PROTECTED clearance."

> "All three are watching the same feed. Right now the drone hasn't launched. Let's start the mission."

---

## 3 — Launch (1 min)

**[In a terminal window:]**
```bash
docker compose exec drone-sim python simulator.py --mission standard
```

**[Watch all three tabs — the drone appears on the map over the Canadian base]**

> "The drone has taken off from the Canadian base. Alice sees the track. Bob sees it too — he's Canadian, this is Canadian sovereign airspace, and his releasability covers it."

> "Dave — nothing. PROTECTED clearance. The drone is in a SECRET zone."

---

## 4 — Crossing the Boundary (2 min)

**[Watch the drone track move toward the exercise corridor — approximately T+30s]**

> "The drone is leaving the Canadian base perimeter. Watch Bob's screen."

**[At the geofence boundary: Bob's last track dot appears, then nothing new]**

> "Bob's track just stopped. The drone crossed from the Canadian base zone — releasable to CAN — into the joint exercise corridor, which is releasable to FVEY. Bob holds CAN releasability only. Signet denied his unwrap request the moment the zone changed."

> "This wasn't a UI decision. No one flipped a switch. The drone-sim labeled that frame FVEY. Signet evaluated Bob's JWT. Releasability mismatch. Denied."

**[Switch to Signet Admin audit log — `http://localhost:4775/audit`]**

> "Here's the audit trail. You can see the DENY entries for Bob's subject ID — reason: `releasability_mismatch`. Alice's entries — all ALLOW."

---

## 5 — The Blind Leg (1 min)

**[Drone continues over UK base and toward target — T+90s to T+210s]**

> "The drone is flying over the UK base now, then on to the target. Only Alice sees this. Bob and Dave have no visibility. Their COPs haven't updated since the last frame they were authorized to see."

> "From a server perspective, all three clients are receiving the same stream of object IDs from the drone-sim. The difference is what happens when each client asks Signet to decrypt: Alice gets the plaintext, Bob and Dave get a 403."

---

## 6 — Dave's Moment (1 min)

**[Watch Dave's tab — at approximately T+240s the drone exits the target area]**

> "The drone is turning around. It's exiting the target zone and entering transit airspace — PROTECTED classification. Watch Dave's screen."

**[A track dot appears on Dave's map]**

> "Dave just got his first frame of the mission. The drone has been flying for four minutes. Dave sees it for the first time now — not because we changed any permissions, but because the data label changed. PROTECTED, releasable to FVEY. Dave qualifies."

---

## 7 — The Return (1 min)

**[Drone heads back toward Canadian base — T+270s to T+300s]**

> "On the return leg, Dave tracks the drone through transit airspace. Alice still has full coverage. Bob — still nothing, because transit airspace is FVEY releasability, and Bob only holds CAN."

**[As drone enters Canadian base perimeter]**

> "Canadian base zone again. Bob's track reappears. Three operators. Three completely different pictures. One feed."

---

## 8 — The Audit (1 min)

**[Switch to Signet Admin — `http://localhost:4775/audit`]**

> "The complete audit trail for this mission. Every frame, every operator, every decision. ALLOW with reason `policy_allow`. DENY with reason `releasability_mismatch` or `clearance_insufficient`. Correlation IDs trace each frame from the drone-sim through Signet to each operator's COP."

**[Show the policy page — `http://localhost:4775/policy`]**

> "The policy is three rules: classification dominance, releasability OR, caveats AND. They've been running unchanged for the entire mission. No one reconfigured anything."

---

## 9 — Takeaway (30 sec)

- **One data feed.** No separate networks per nation or classification.
- **Policy travels with every frame.** Not in a database the server checks — cryptographically bound to the telemetry data itself.
- **The server cannot be overridden.** Even if someone breaks into the server, the ciphertext is meaningless without Signet's key release. And Signet won't release without OPA saying allow.
- **Full audit.** Every access decision, for every operator, for every frame of the mission.

---

## Fallback / Contingency

If drone-sim fails to start:
1. Pre-recorded mission replay: `python simulator.py --replay mission_recording.jsonl`
2. This replays a pre-generated sequence of envelopes without needing the mission path computation

If Signet is unreachable:
1. The COP-UI shows connection error — demo does not degrade gracefully without Signet (by design — this is the point)
2. Fallback: show the pre-recorded audit log from `docs/sample-audit.json`
