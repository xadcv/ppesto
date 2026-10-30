# ppesto

`pepesto_cart.py` turns a grocery list into a ready-to-pay **Migros** or **Coop** cart using the
[Pepesto API](https://www.pepesto.com/ai-grocery-shopping-agent/). It remembers which exact product you chose for
each word on your list (`eggs` → *Demeter Free-Range Eggs 6 Pieces* at Migros) in a small markdown file, so
next time those items skip the product search entirely.

Single file, Python 3.9+, no dependencies.

## API key

Looked up in this order, and never written anywhere by the tool:

1. `PEPESTO_API_KEY` environment variable (bash `export`, or a cloud-agent secret).
2. A `.env` file containing `PEPESTO_API_KEY=...` in the current directory, next to `pepesto_cart.py`, or in
   `~/.pepesto/.env`. `.env` is git-ignored.
3. `PEPESTO_API_KEY_CMD`: a command that prints the key, for vaults, e.g.
   `export PEPESTO_API_KEY_CMD="op read op://Private/Pepesto/credential"` or
   `"bw get password pepesto"` or `"pass show pepesto"`. It can be set in the environment or in a `.env` file.

## Usage

```bash
# Interactive (in a terminal): pick a product for each new item, then get the checkout link
./pepesto_cart.py shop --store migros "eggs, milk, sourdough bread, 2 tomatoes"

# Agent / non-interactive, two steps:
./pepesto_cart.py shop -s coop -f list.txt --no-prompt     # prints numbered choices, exits 2
./pepesto_cart.py shop -s coop -f list.txt --pick eggs=3 --pick milk=1 --pick butter=skip
#   the second run reuses the cached search (6 h), so you only pay once for it

# Take Pepesto's top match for anything not remembered or picked
./pepesto_cart.py shop -s migros "eggs, butter, rice" --auto

./pepesto_cart.py shop ... --json       # machine-readable cart and pending choices
./pepesto_cart.py shop ... --dry-run    # build the cart but don't create the checkout link
./pepesto_cart.py memory                # list associations
./pepesto_cart.py memory forget eggs -s migros
./pepesto_cart.py credits               # remaining Pepesto credit
```

The list can be passed as arguments, with `--file PATH`, or on stdin. Items can be separated by commas,
semicolons, or newlines, and markdown bullets or checkboxes are accepted. `3x eggs` sets the number of units
explicitly. Otherwise Pepesto's suggested quantity is used, and remembered items default to 1.

The output is the cart (product, quantity, last known price, estimated total) and a **Ready to pay** link.
Open it on a phone, or scan the QR code it shows on desktop. The Pepesto app fills your Migros or Coop basket
and you check out and pay there. Pepesto does not automate payment.

Exit codes: `0` means the cart was created, `2` means choices are still needed, and `1` means an error.

## Memory file

The default is `~/.pepesto/memory.md`. Override it with `--memory PATH`, `PEPESTO_MEMORY_FILE`, or
`PEPESTO_HOME`. It is a plain markdown table that you can edit by hand:

| term | store | product | barcode | price | product_id | last_used | session_token |
|---|---|---|---|---|---|---|---|
| eggs | migros.ch | Demeter Free-Range Eggs 6 Pieces | 196045700600 | CHF 6.95 | https://www.migros.ch/de/product/196045700600 | 2026-10-30 | eyJ… |

- Associations are per store: `eggs` can point to different products at Migros and Coop.
- Only explicit choices (interactive or `--pick`) are saved. Add `--remember-auto` to also save `--auto` picks.
- `barcode` is the GTIN when Pepesto provides one. Neither store currently does, so it falls back to the
  store's article number taken from the product URL.
- `session_token` is what Pepesto's checkout needs. Storing it is what lets remembered items skip the search.
- `--fresh` ignores memory for one run, and choosing again overwrites the association.

## Cost per run (Pepesto credits)

| step | when | cost |
|---|---|---|
| `/parse` + `/products` | only if at least one item isn't remembered (one call for all new items) | €0.36 |
| `/mcheckout` | creating the ready-to-pay link | €0.60 |

A list made only of remembered items costs €0.60. `--dry-run` with everything remembered costs nothing.

Items Pepesto can't match are sent to the checkout as free text (at most 10), and the Pepesto app resolves
them when it fills the basket.

## Tests

```bash
python3 -m unittest discover -s tests
```
