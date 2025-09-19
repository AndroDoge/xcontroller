# XController 🚀  
High‑precision, privacy‑aware Telegram administration & cross‑group intelligence bot.

> Focused on deterministic moderation, controlled propagation, and low‑noise automated enforcement.

---

## 🧭 Feature Map

| Category | Highlights |
|----------|------------|
| 🛡 Moderation Core | Username enforcement, banned word engine (progressive discipline), global ban propagation |
| 📡 Forwarding Engine | True Telegram forwarding (retains "Forwarded from"), randomized per‑target delay, 24h per‑user cooldown |
| 🔐 Security & Privacy | HMAC (SHA‑256) user pseudonymization, configurable thresholds, minimal data retention |
| ⚙️ Operations & Maintenance | Deleted account cleanup (rotational), adaptive auto‑discovery fallback, flood wait handling |
| ⚡ Performance & Control | Token bucket rate limiting, retry wrapper for RPC, selective forwarding logic |
| 🧩 Extensibility Hooks | Topic ID parsing (reserved for future filtering), modular ENV-based policy surface |
| 🧪 Integrity & Safety | No silent escalation, explicit event logging, reproducible deterministic policies |

---

## 🛡 Core Moderation

### ✅ Username Enforcement  
Optionally auto‑kicks new members who lack a username.  
Configurable notice message before removal.

### 🧪 Banned Word Filter  
- Dual strategy: word‑boundary + substring scan  
- First violation → delete only  
- Second violation → global ban + propagation  
- Constant‑time DB operations (hashed user key)  

### 🌐 Global Ban Propagation  
When threshold reached:
1. User ID → HMAC hash
2. Stored in global ban table
3. Ban replicated across all configured forwarding targets

---

## 📡 Forwarding Engine (Cross‑Group Distribution)

| Mechanic | Behavior |
|----------|----------|
| Mode | True `forward_messages` (retains metadata & origin) |
| Media | Forwarded as-is |
| Random Delay | Per‑target delay ∈ `[FORWARD_DELAY_MIN, FORWARD_DELAY_MAX]` |
| Cooldown | One forward opportunity per user every 24h |
| Filtering | Skips banned-term content |
| Loop Safety | Token bucket + FloodWait backoff |
| Topic Syntax | `chat_id:topic_id` parsed & stored (not yet applied to routing) |

---

## 🔐 Security & Privacy Layer

| Element | Rationale |
|---------|-----------|
| HMAC SHA‑256 hashing of user IDs | Pseudonymizes without reversible storage |
| SALT required | Prevents rainbow table correlation |
| No plaintext IDs in violation tables | Limits data sensitivity |
| Progressive enforcement | Avoids one‑strike false positives |
| Optional username policy | Deploy only where aligned with group rules |

---

## ⚡ Performance & Reliability

- Token Bucket for controlled outbound RPC pressure  
- Retry wrapper for FloodWait with single safe retry  
- Asynchronous flow prevents long blocking  
- Rotational cleanup spreads load (one group per 12h window)  
- Forward jitter reduces spam signature footprint  

---

## 🧹 Maintenance & Hygiene

| Task | Interval | Notes |
|------|----------|-------|
| Deleted account sweep | 12h | Paginates in slices (25 per pass) |
| Forward eligibility check | Continuous | Timestamp-based cooldown validation |
| Group rotation cleanup | 12h cycle | Time-derived index (stateless) |

---

## 🧩 Topic ID Handling (Pre‑Wired)

`FORWARD_GROUP_IDS` supports entries like:
```
-1001234567890,-1002223334444:42,-1009998887777
```
Topic IDs are stored for future selective monitoring / per‑topic policies — they do **not** alter forwarding destinations yet (forwarding always targets the chat root to preserve native metadata integrity).

---

## 🔧 Configuration (Environment Variables)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `API_ID` | ✅ | – | Telegram API ID |
| `API_HASH` | ✅ | – | Telegram API hash |
| `BOT_TOKEN` | ✅ | – | Bot token from @BotFather |
| `SALT` | ✅ | – | Secret salt for HMAC hashing user IDs |
| `BANNED_WORDS` | ❌ | (empty) | Comma list: `spam,badword,...` |
| `ENFORCE_USERNAME` | ❌ | `1` | `1/true/yes/on` enables username kicking |
| `USERNAME_KICK_NOTICE` | ❌ | (empty) | Optional message posted before kick |
| `FORWARD_GROUP_IDS` | ❌ | (autodiscover) | Comma list of `chat_id` or `chat_id:topic_id` |
| `FORWARD_DELAY_MIN` | ❌ | `0.8` | Lower bound (seconds) for random forward jitter |
| `FORWARD_DELAY_MAX` | ❌ | `2.4` | Upper bound (seconds) for random forward jitter |

---

## ⚙️ Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Minimal `.env` example:
```env
API_ID=123456
API_HASH=0123456789abcdef0123456789abcdef
BOT_TOKEN=123456:AA...token
SALT=change_this_to_long_random_string
FORWARD_GROUP_IDS=-1001234567890,-1009876543210:55
BANNED_WORDS=spam,scam,fraud
ENFORCE_USERNAME=1
FORWARD_DELAY_MIN=0.9
FORWARD_DELAY_MAX=2.2
```

---

## 🧠 Execution Flow

```
[new message]
  └─> global ban check
        ├─> banned term? -> delete -> violation++ -> (threshold?) -> global propagate
        └─> forward eligible? -> forward_messages() with jitter
```

```
[new member joins]
  └─> username?
        ├─> yes -> allow
        └─> no  -> optional notice -> kick cycle
```

---

## 🧪 Enforcement Stages

| Stage | Trigger | Action |
|-------|---------|--------|
| 1 | First banned word | Delete message |
| 2 | Second offense | Global ban + propagation |

---

## 🛠 Operational Notes
- Forwards always preserve original metadata (no copy mode fallback).
- Media is forwarded directly; captions still scanned.
- Flood resilience: waits & single retry on FloodWait.
- Deterministic policy: no hidden ML heuristics.

---

## 🧭 Roadmap Ideas
| Idea | Status |
|------|--------|
| Topic-scoped selective forwarding | Future |
| Advanced pattern engine (Aho-Corasick) | Future |
| Whitelist override for global bans | Future |
| Metrics endpoint (Prometheus) | Future |
| Admin commands (/status, /violations) | Future |
| Structured JSON logs | Future |
| Forward deduplication hash | Future |

---

## 🔍 Troubleshooting

| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| No forwards | Cooldown active / no groups configured | Check DB forward_state / FORWARD_GROUP_IDS |
| Frequent FloodWait | Too many targets or too low delays | Increase delay range |
| Username kicks absent | ENFORCE_USERNAME disabled | Set `ENFORCE_USERNAME=1` |
| No escalation | Only one violation observed | Working as designed |
| No propagation | No forward groups configured | Add FORWARD_GROUP_IDS |

---

## 🧾 Legal / Ethical
Use only in communities where you have moderation authority. Do not pair with a user session to bypass platform safeguards.

---

## 🤝 Contributions
Focused PRs welcome for: performance, observability, policy modularity.

---

## 📜 License
MIT

---

## ⭐ Support
If this bot helps harden your Telegram infrastructure:
- Fork the repo
- Open well‑scoped issues
- Propose policy extensions

Happy controlling. 🛡 And use Linux! 
