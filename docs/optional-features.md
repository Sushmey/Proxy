---
title: "Optional features"
description: "Places, web search, and shopping."
---

Each feature below needs its own key or login. Without it, that one tool
returns an error and everything else keeps working. Key files live under
`credentials/`, which is gitignored.

## Places (Mapbox)

Finds restaurants, cafes, and other places near an address you give it.

1. Create a free account at [https://mapbox.com](https://mapbox.com) and copy a **public access token** (starts with `pk.`).
2. Save it as `credentials/backend/mapbox_access_token.json`:

```json
{ "access_token": "pk.xxxxxxxx" }
```

Results include name, address, distance, phone, and website. There are no star
ratings or price levels, because this data source doesn't provide them. If an
address like "Springfield" matches several real places, the bot asks which one
you mean.

## Web search (Tavily)

Lets the agent look things up on the web.

1. Create a free account at [https://tavily.com](https://tavily.com) and copy your API key (starts with `tvly-`).
2. Save it as `credentials/backend/tavily_api_key.json`:

```json
{ "api_key": "tvly-xxxxxxxx" }
```

Tavily's free plan has a monthly allowance; check their site for the current
limit.

## Shopping (Amazon, demo only)

Finds a product, signs in, adds it to your cart, reads the checkout page, and
asks you to confirm. **It never places a real order**: `PLACE_ORDERS_ENABLED`
in `shopping_agent.py` is `False`. Be aware that when you confirm, the bot
still replies "Order placed" even though nothing was ordered, so don't rely
on that message. Check your Amazon account if in doubt.

1. Download the browser it drives: `playwright install chromium`.
2. Save your login as `credentials/users/owner/amazon_credentials.json`:

```json
{ "email": "you@example.com", "password": "your-amazon-password" }
```

The password is stored as plain text on your machine, so use this only on a
computer you trust.

Things to know:

- It opens a visible browser window while it works.
- It automates your Amazon account with a browser, which Amazon's terms of use may not allow, and Amazon can rate-limit or block it. Use it at your own risk.
- Only the owner can use it. Friends can't use your account; a friend's own credentials would have to be registered by hand (`amazon_credentials_path` in `state/users.json`).
- How the confirm step works: [SHOPPING_AGENT_FLOW](/SHOPPING_AGENT_FLOW).
