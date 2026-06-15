# Albion Market Math Examples

Keywords: albion market math profit buy order sell order spread tax setup fee transport risk arbitrage refining crafting focus resource return rate rrr margin black market hauling price volume liquidity

Use this file when members ask whether a trade, craft, refine, or arbitrage is
worth doing.

Market rule:

- A listed bot price is a target, not a promise.
- Buy order means wait to buy at your target.
- Sell order means wait to sell at your target.
- Instant buying at the ask and instant selling to the bid usually destroys the
  margin.

Simple flip math:

- Expected revenue = sell order price minus taxes/fees.
- Expected cost = buy order price plus taxes/fees.
- Expected profit = revenue minus cost.
- Real profit must also account for hauling time, risk, item volume, price
  movement, and whether the order actually fills.

Transport risk:

- Safe city-to-city trade is lower risk but often lower margin.
- Caerleon/Black Market, red zones, black zones, Roads, and overloaded routes
  need higher margin because death can erase the load.
- Split loads if the total value is painful to lose.

Refining/crafting math:

- Input cost: raw resources/materials.
- Return value: resources returned through RRR/focus/local bonus.
- Output value: expected sell order after taxes.
- Profit = output value + returned-resource value - input cost - fees - hauling
  cost/risk.

Focus use:

- Focus is limited. Spending focus on low-margin items can be worse than saving
  it for higher-spec/high-return activities.
- Higher spec usually improves focus efficiency.
- Do not tell someone a craft is profitable without current market prices.

Liquidity:

- High listed price does not matter if nobody buys.
- Check volume and order depth.
- Big stacks can crash a thin market.

Good short answer:

"Treat the bot's arbitrage as a trade idea: place a buy order near the target,
wait for fill, haul only if the route risk is worth it, then list a sell order.
Do not assume the instant market price will still be there."
