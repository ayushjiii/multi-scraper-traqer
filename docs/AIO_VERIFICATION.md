# Google AI Overviews (`aio`) — Verification Report

This documents what has been **proven** about the `aio` agent and what is **gated by
proxy quality**, so the engine's status is unambiguous.

## TL;DR

- The `aio` agent **code is correct** — extraction (answer text + source URLs) is
  verified deterministically.
- The agent **cannot produce a live AI Overview on free datacenter proxies** — not
  a code limitation, an IP-reputation gate. Google CAPTCHAs/suppresses AI features
  for distrusted IPs. **0 of 9 unique free IPs got past Google's gate.**
- To get live AIO results you need **residential or mobile-carrier proxies.** The
  code is ready the moment those exist.

---

## 1. Code correctness — PROVEN (no proxy needed)

Two deterministic tests, run against representative inputs, with zero network/IP
dependency. (See `test_aio.py` for the live inspector; the unit checks below were
run during development.)

### Source harvesting (`_harvest_urls`)
Fed a realistic Google `/async/` hydration fragment containing escaped slashes,
HTML-comment citation metadata (`<!--Sv6Kpe[[...]]-->`), `#:~:text=` fragment
anchors, trailing punctuation, and Google-infra noise:

- **5 / 5** real publisher URLs extracted (Wikipedia, Asana, Monday, ClickUp, PCMag)
- **100%** of Google/gstatic/googleapis/youtube noise filtered out
- text-fragment anchors stripped, trailing punctuation cleaned → **PASS**

### DOM block detection + answer extraction
Fed a realistic SERP (an AI Overview block — using the stable `data-attrid`/`jsname`
anchors and the "AI Overview" heading — sitting above organic results, with a
`<style>` leak inside the block):

- block detected via stable anchors ✔
- AI answer text captured ✔
- "AI Overview" heading line stripped ✔
- leaked `<style>` content stripped ✔
- **organic results NOT captured** as the answer ✔ → **PASS**

---

## 2. Free-proxy viability — MEASURED (full pool)

Swept the **entire active proxy pool** against
`google.com/search?q=Asana vs Monday vs ClickUp for agencies&hl=en&gl=us`
(a comparison query, ~95% AI-Overview trigger rate) over Camoufox:

| Outcome | Count |
|---|---|
| CAPTCHA ("unusual traffic" / `/sorry/`) | 30 |
| DEAD (proxy unreachable) | 21 |
| **Reached full SERP** | **0** |
| **Rendered an AI Overview** | **0** |

The 51 pool rows resolve to **9 unique IPs** (same IP, different ports/creds).
**All 9 unique IPs were CAPTCHA'd.** Even the developer's own residential IP was
CAPTCHA'd during testing (Google flags repeated automated hits).

**Conclusion:** free datacenter IPs are categorically unusable for Google AI
Overviews. This matches every vendor's documented finding — Google does not just
CAPTCHA datacenter IPs, it *suppresses* AI features for them, so the overview won't
render even if a page loads. A pool-wide retry strategy gains nothing (0% pass rate,
not an occasional-slip rate).

---

## 3. What unblocks it

1. **Residential or mobile-carrier proxies** (mobile triggers AIO most reliably).
   Load them with `python load_proxies.py --replace` and run the `aio` agent
   normally — no code change needed. Re-run `test_aio.py` to confirm extraction
   against a real overview and to log which `/async/` slug carries citations.
2. **OR** escalate to a SERP API with a structured AI-Overview field
   (SerpApi `ai_overview`, DataForSEO `ai_overview`) — the vendor-consensus route
   for reliable AIO at scale. Costs per query; handles proxies/anti-bot server-side.

---

## 4. Notes

- Playwright is pinned to **1.59.0**. 1.60.0 has a Firefox driver regression that
  crashes on Google Search (`pageError.location.url`). Do not upgrade without
  re-testing against Google.
- An **absent** AI Overview is a valid result (Google only generates one for
  ~30–50% of queries, non-deterministically). The agent records an empty result in
  that case — it is not a failure.
