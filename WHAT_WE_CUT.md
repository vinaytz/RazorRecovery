# What we cut, and why

Every omission here was deliberate. Listing them is cheaper than being caught
by them, and a reviewer who finds an undisclosed gap stops trusting the numbers
that *are* real.

---

## Cut on purpose (judgment calls, not time)

**Server-initiated retries are `INTENT_ONLY`.** `RazorpayExecutor.execute`
never debits a card. `DRY_RUN=true` is the default and returns a would-do
string; with `DRY_RUN=false` it creates payment links but records RETRY as
`INTENT_ONLY` rather than charging. A hackathon build should not move money on
someone's card, and a debit is not reversible. The decision path, gates,
idempotency key and outcome recording are all real — only the final debit call
is withheld.

**Webhook fixtures are hand-built, not live captures.** The 5 payloads in
`fixtures/webhooks/` follow Razorpay's documented event schema — real field
names, real nesting, real `error_reason` values — but the ids are `_TEST`
placeholders and no live account produced them. We had no tunnel binary
(`ngrok`/`cloudflared` absent), no credentials, and the pinned `razorpay==1.4.2`
SDK fails to import on Python 3.12 (`pkg_resources` was removed in setuptools
81+). TASKS.md P7 permits fixtures as the fallback. What *is* genuinely
exercised: HMAC-SHA256 over the raw body, 400-not-500 on a bad signature,
`UNIQUE(events.dedupe_key)` absorbing retries, and success events closing cases.

**The unverified-webhook path stays open, loudly.** With
`RAZORPAY_WEBHOOK_SECRET` unset the endpoint accepts unsigned requests and puts
a warning in the response body. A hard failure would make the demo
un-runnable for anyone without a secret; a *silent* skip is how an open endpoint
ships to production. So: open, but it tells you.

**`BASELINE_RECHECKS_BEFORE_SEND = False`.** The fixed-schedule baseline fires
without re-reading payment state, which is where its ~90 false chases per 10k
come from. That is the common real-world pattern, but it is a modelled choice
and it flatters us. Flip the flag in `sim/runner.py` and the false-chase gap
closes while our gate and uplift advantages remain.

**B2B / multi-party recovery is config, not code.** Marketplace splits,
partial settlements against one obligation, and dunning across a payer
hierarchy are all real Track 03 territory. We model one payer per obligation.
The `Obligation` abstraction (debt, not payment) is the seam that would carry
it, but nothing above that seam knows about multiple payers.

---

## Cut for time

**LLM jobs ③ and ④ (SPEC §17).** Only job ① — error string → `FailureClass` —
is built. Missing: ③ DLT-template selection with slot filling, and ④
plain-English narration of a decision trace on the case card. Both are
presentation-layer; neither touches a money decision. The interface they would
sit behind (`app/services/llm.py`, `LLM` Protocol, sha1 disk cache) exists, so
adding them is additive rather than structural.

**GeminiLLM is untested against the live API.** `GEMINI_API_KEY` was not set in
this environment. The class is written and its four failure paths are covered by
injected fakes — no key, API exception, malformed JSON, invented category — each
falling back to the keyword classifier and returning an enum member. But no real
Gemini response has ever passed through it. `LLM_MODE=stub` is the default and
the benchmark is byte-identical under it.

**Notifier is a counter, not a sender.** No SMS or WhatsApp leaves the process.
Contact actions increment `contacts` and feed the friction cost and the 7-day
cap. DLT template compliance is therefore asserted in config, not enforced
against a real gateway.

**Reversals are modelled, not observed.** `reversal_rate` flips a fixed fraction
of recoveries at T+30 to produce net-vs-gross. There is no chargeback webhook
wired to it.

**No per-merchant calibration.** One `config/default.yaml` for every merchant.
Real deployment needs per-merchant friction costs and contact caps, since a
₹200 D2C order and a ₹90,000 SaaS invoice do not tolerate the same nudge rate.

---

## Known limits of the evidence

**The data is synthetic.** Treat every absolute rupee figure as directional. The
method — holdout arm, uplift scoring, oracle ceiling, false-chase counter — is
the contribution. The numbers demonstrate the method runs; they are not a claim
about your book.

**`where_we_lost` is empty at n=2000.** The metric is computed and displayed,
but on the default preset the engine does not underperform control in any
failure-class segment, so the table renders empty. That is a real result, not a
broken query — with a coarse segment key (failure class only) the engine's
gate-and-uplift discipline means it rarely loses a whole class. A finer key
would surface losses; we did not tune the key to manufacture one.

**Five seeds is a small sample.** The seed-to-seed range
(Rs 855k – Rs 1,013k) came out *narrower* than the within-run bootstrap CI
(Rs 724k – Rs 1,180k), which is the opposite of what we predicted when we added
the sweep. Read them as complementary measures of different variance sources,
not one superseding the other.

**One arm's rung ceiling is untested at scale.** `max_rung` is respected and
gate-tested, but no case in the benchmark climbs past HUMAN, so the
`WRITE_OFF`-at-ladder-top branch is exercised only by unit tests.
