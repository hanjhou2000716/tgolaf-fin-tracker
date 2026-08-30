# PRStK Growth Transaction V3

This is the production current-input contract.  It must live in a new Google
Form and a clean response tab; the retired branched form remains read-only
under `LEGACY_TRANSACTION_SOURCES`.

## Business questions

| Question | Type | Allowed values / validation |
| --- | --- | --- |
| 交易主體 | Dropdown | 台股、 美股、 台幣、 美金、 質押、 擔保品 |
| 交易標的 | Short answer | Required for 台股／美股／擔保品; blank for 台幣／美金／質押 |
| 交易動作 | Dropdown | 買入、賣出、存入、提領、全數取代、借款、還款、利率 |
| 交易單位 | Dropdown | 張、股、台幣、美金、% |
| 交易數量 | Short answer | Non-negative decimal only; ordinary actions must be greater than zero |

Google Forms may add `Timestamp` and `Email Address`.  They are transport
metadata and do not change the `FORM_V3` schema identity.

## Validation matrix

- 台股: symbol required; 買入／賣出／全數取代; 張／股; 張 is normalised to
  1,000 股; currency is TWD.
- 美股: symbol required; 買入／賣出／全數取代; 股 only; fractional shares
  are preserved; currency is USD.
- 台幣／美金: symbol blank; 存入／提領／全數取代; matching currency unit;
  maps to `現金_TWD`／`現金_USD` and `SET_BALANCE` for replacement.
- 質押: symbol blank; 借款／還款 with 台幣, or 利率 with `%`; maps to
  `質押負債`／`質押利率`; USD pledge debt and replacement are rejected.
- 擔保品: symbol required; 存入／提領 with 張／股; remains the separate
  `擔保品` bucket and is never added to total holdings.

The backend is authoritative and rejects unknown combinations, negative or
non-numeric quantities, ambiguous duplicate columns, and missing required
symbols.  No historical response rows are deleted during cutover.

## Deployment settings

Set these workflow secrets/variables after the clean response tab exists:

- `CURRENT_TRANSACTION_SOURCE=TRANSACTIONS_CURRENT` (exact tab name)
- `LEGACY_TRANSACTION_SOURCES=表單回覆 3,Form Responses 1,Form Responses 2,Form V2`
- `HISTORY_SOURCE=History`
- `FORM_V3_URL=<published responder URL>`
- `FORM_V3_CUTOVER_AT=<ISO-8601 timestamp>`

The dashboard only treats the exact current tab as active input.  Unknown tabs
are not discovered by name fragments and cannot silently enter the ledger.
