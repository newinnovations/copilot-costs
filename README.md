# copilot-costs

Estimate what your local **GitHub Copilot CLI** sessions would cost under
the post‑June token‑based pricing, and compare that estimate against the
AI credits (AIC) that Copilot actually charged.

Copilot CLI writes a per‑session log to `~/.copilot/session-state/<uuid>/events.jsonl`.
Every session ends with a `session.shutdown` event whose `modelMetrics`
field contains the cumulative token usage per model, and — for sessions
recorded after Copilot started reporting it — the `totalNanoAiu` field with
the actual AI credits billed (`1 AIC = 1e9 nano‑AIU = $0.01`).

This script scans every session under `~/.copilot/session-state`, applies the
public per‑model pricing to the token counters, and prints:

- a per‑session table (date, session id, model, requests, tokens, estimated AIC, actual AIC),
- a monthly roll‑up, and
- a grand total by model, in AIC and USD.

## Requirements

- Python 3.10+
- A populated `~/.copilot/session-state/` directory (i.e. you've used the Copilot CLI at least once)

No third-party dependencies.

## Usage

```bash
python3 copilot_cli_costs.py
```

Example (truncated) output:

```text
=== Grand totals by model ==========================================================================================

Model                   Reqs          Input         Output         CacheR         CacheW       est AIC    actual AIC
--------------------------------------------------------------------------------------------------------------------
claude-haiku-4.5         293     10,613,891        188,937      9,514,374        343,693       308.156         0.000
claude-opus-4.6          628     57,765,031        627,401     54,006,291              0      6148.187         0.000
claude-opus-4.7          104      9,553,583         65,483      8,853,911        699,523      1043.679      1043.679
claude-sonnet-4.5        206      9,802,508        122,455      8,839,733        853,611       801.728         0.000
claude-sonnet-4.6       8009    615,189,327      5,578,988    582,537,584      3,154,105     35876.690         0.000
gpt-5.4                 5668    491,942,702      5,132,336    466,333,568              0     25759.127        42.139
gpt-5.4-mini             372      9,992,875        146,503      8,131,584              0       266.510         0.000
--------------------------------------------------------------------------------------------------------------------
                                                                                   TOTAL     70204.078      1085.818


=== Summary ===============================================================================================

Sessions analysed: 224
Estimated cost (post-June pricing):   70,204.08 AIC  = $  702.04 USD
Actual charged (nano-AIU/1e9):         1,085.82 AIC  = $   10.86 USD   (only for sessions that recorded it)
```

## Pricing

The per‑model rates (USD per 1M tokens: input, cached input, output, and for
Anthropic models cache‑write) are hard‑coded near the top of
[`copilot_cli_costs.py`](copilot_cli_costs.py) in the `PRICING` dict, taken
from the official
[Copilot models and pricing docs](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing).

If a session used a model that is not in the `PRICING` dict, its estimated
cost is skipped (reported as `n/a`) and the model name is listed at the end of
the run. Add it to the dict to include it.

Cost formula per model, per session:

```text
billable_input = inputTokens - cacheReadTokens - cacheWriteTokens   (clamped to 0)
usd = (billable_input * in
     + outputTokens   * out
     + cacheReadTokens  * cache
     + cacheWriteTokens * cw)          # cw only for Anthropic
    / 1_000_000
AIC = usd * 100
```

## Notes and caveats

- **Estimates only.** Copilot's actual bill uses the `totalNanoAiu` field
  when present. The estimate is what you would have paid at list token
  prices; it can differ slightly from the charged amount due to rounding,
  discounts, or promotional pricing.
- Sessions without a `session.shutdown` event are skipped.
- Token counts are read from the last `session.shutdown` event, which
  Copilot writes with cumulative counters for the whole session.
- Timestamps come from `sessionStartTime` in the shutdown event, falling
  back to the first event timestamp.

## License

MIT
