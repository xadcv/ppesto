#!/usr/bin/env python3
"""pepesto-cart: turn a grocery list into a ready-to-pay Migros or Coop cart via the Pepesto API.

Stdlib only. See README.md for usage.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API_BASE = os.environ.get("PEPESTO_API_BASE", "https://s.pepesto.com/api")
STORES = {
    "migros": "migros.ch",
    "migros.ch": "migros.ch",
    "coop": "coop.ch",
    "coop.ch": "coop.ch",
}
DEFAULT_HOME = Path(os.environ.get("PEPESTO_HOME", Path.home() / ".pepesto"))
CACHE_TTL_SECONDS = 6 * 3600
MAX_CHECKOUT_ITEMS = 50
MAX_UNRESOLVED_ITEMS = 10
MEMORY_COLUMNS = ["term", "store", "product", "barcode", "price", "product_id", "last_used", "session_token"]


class CliError(Exception):
    pass


# --------------------------------------------------------------------------- API key


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def resolve_api_key(env: dict[str, str] | None = None, search_dirs: list[Path] | None = None) -> str:
    """Environment variable, then .env files, then a vault command (PEPESTO_API_KEY_CMD)."""
    env = os.environ if env is None else env
    if env.get("PEPESTO_API_KEY"):
        return env["PEPESTO_API_KEY"].strip()

    if search_dirs is None:
        search_dirs = [Path.cwd(), Path(__file__).resolve().parent, DEFAULT_HOME]
    cmd = env.get("PEPESTO_API_KEY_CMD")
    for directory in search_dirs:
        dotenv = _read_dotenv(directory / ".env")
        if dotenv.get("PEPESTO_API_KEY"):
            return dotenv["PEPESTO_API_KEY"]
        cmd = cmd or dotenv.get("PEPESTO_API_KEY_CMD")

    if cmd:
        try:
            out = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as exc:
            raise CliError(f"PEPESTO_API_KEY_CMD failed: {exc}") from exc
        key = out.stdout.strip()
        if key:
            return key

    raise CliError(
        "No Pepesto API key found. Set PEPESTO_API_KEY in the environment, put it in a .env file "
        "(current dir, next to pepesto_cart.py, or ~/.pepesto/.env), or set PEPESTO_API_KEY_CMD to a "
        "command that prints it (e.g. 'op read op://Private/Pepesto/credential')."
    )


# --------------------------------------------------------------------------- API client


class PepestoClient:
    def __init__(self, api_key: str, base_url: str = API_BASE, verbose: bool = False):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.verbose = verbose
        self.eurocents_charged = 0

    def call(self, endpoint: str, payload: dict, timeout: int = 90) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/{endpoint}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charged = int(resp.headers.get("Pepesto-Eurocents-Charged") or 0)
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise CliError(f"Pepesto /{endpoint} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CliError(f"Could not reach Pepesto /{endpoint}: {exc.reason}") from exc
        self.eurocents_charged += charged
        if self.verbose:
            log(f"  /{endpoint}: charged {charged / 100:.2f} EUR")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            # Pepesto reports some backend failures as HTTP 200 with a plain-text body.
            raise CliError(f"Pepesto /{endpoint} failed: {body.strip()[:300]}") from None

    def search_products(self, lines: list[str], store: str, locale: str | None) -> dict:
        parsed = self.call("parse", {"recipe_text": "Shopping list:\n" + "\n".join(lines)})
        token = parsed.get("kg_token") or (parsed.get("recipe") or {}).get("kg_token")
        if not token:
            raise CliError("Pepesto /parse returned no kg_token")
        payload = {"recipe_kg_tokens": [token], "supermarket_domain": store}
        if locale:
            payload["item_names_locale"] = locale
        return self.call("products", payload)

    def checkout(self, store: str, skus: list[dict], unresolved: list[str], locale: str | None) -> dict:
        payload: dict = {"supermarket_domain": store, "skus": skus}
        if unresolved:
            payload["unresolved_items"] = unresolved
        if locale:
            payload["user_locale"] = locale
        return self.call("mcheckout", payload)

    def credits(self) -> int:
        return int(self.call("credits", {}).get("euro_cents", 0))


# --------------------------------------------------------------------------- list parsing

_QTY_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*(?:x|×|pcs?|pieces?|packs?|g|kg|ml|cl|dl|l)?\s+(.+)$", re.IGNORECASE
)
_EXPLICIT_UNITS_RE = re.compile(r"^\s*(\d+)\s*[x×]\s*(.+)$", re.IGNORECASE)


@dataclass
class ListItem:
    raw: str
    term: str
    units: int | None  # only set for an explicit "3x eggs"


def normalize_term(text: str) -> str:
    text = text.strip().lower()
    match = _QTY_RE.match(text)
    if match:
        text = match.group(2)
    return re.sub(r"\s+", " ", text).strip(" .-*")


def parse_grocery_list(text: str) -> list[ListItem]:
    items: list[ListItem] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\n,;]+", text):
        raw = re.sub(r"^\s*(?:(?:[-*•]|\[[ xX]?\])\s*)+", "", chunk).strip()
        if not raw:
            continue
        term = normalize_term(raw)
        if not term or term in seen:
            continue
        seen.add(term)
        explicit = _EXPLICIT_UNITS_RE.match(raw)
        items.append(ListItem(raw=raw, term=term, units=int(explicit.group(1)) if explicit else None))
    return items


# --------------------------------------------------------------------------- memory (markdown)


@dataclass
class Association:
    term: str
    store: str
    product: str
    product_id: str
    session_token: str
    barcode: str = ""
    price: str = ""
    last_used: str = ""


def _md_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _split_md_row(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    cells = re.split(r"(?<!\\)\|", inner)
    return [c.strip().replace("\\|", "|") for c in cells]


class Memory:
    HEADER = (
        "# Pepesto grocery memory\n\n"
        "Maps words from your grocery list to the exact product you picked last time, per store.\n"
        "Remembered items skip the Pepesto product search. Edit or delete rows freely; "
        "`pepesto_cart.py memory forget <term>` also works.\n\n"
    )

    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[tuple[str, str], Association] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        columns: list[str] | None = None
        for line in self.path.read_text().splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = _split_md_row(line)
            if columns is None:
                columns = [c.lower() for c in cells]
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue
            row = dict(zip(columns, cells))
            if not row.get("term") or not row.get("store") or not row.get("session_token"):
                continue
            assoc = Association(
                term=normalize_term(row["term"]),
                store=STORES.get(row["store"].lower(), row["store"].lower()),
                product=row.get("product", ""),
                product_id=row.get("product_id", ""),
                session_token=row["session_token"],
                barcode=row.get("barcode", ""),
                price=row.get("price", ""),
                last_used=row.get("last_used", ""),
            )
            self.rows[(assoc.term, assoc.store)] = assoc

    def get(self, term: str, store: str) -> Association | None:
        return self.rows.get((normalize_term(term), store))

    def put(self, assoc: Association) -> None:
        assoc.term = normalize_term(assoc.term)
        self.rows[(assoc.term, assoc.store)] = assoc

    def forget(self, term: str, store: str | None) -> int:
        term = normalize_term(term)
        keys = [k for k in self.rows if k[0] == term and (store is None or k[1] == store)]
        for key in keys:
            del self.rows[key]
        return len(keys)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [self.HEADER.rstrip("\n"), ""]
        lines.append("| " + " | ".join(MEMORY_COLUMNS) + " |")
        lines.append("|" + "|".join("---" for _ in MEMORY_COLUMNS) + "|")
        for key in sorted(self.rows):
            a = self.rows[key]
            values = [a.term, a.store, a.product, a.barcode, a.price, a.product_id, a.last_used, a.session_token]
            lines.append("| " + " | ".join(_md_cell(v) for v in values) + " |")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(self.path)


# --------------------------------------------------------------------------- search cache


class SearchCache:
    """Caches /products responses so a follow-up `--pick` run doesn't pay for the same search again."""

    def __init__(self, directory: Path, ttl: int = CACHE_TTL_SECONDS):
        self.directory = directory
        self.ttl = ttl

    def _path(self, store: str, terms: list[str]) -> Path:
        digest = hashlib.sha256(json.dumps([store, sorted(terms)]).encode()).hexdigest()[:16]
        return self.directory / f"search-{digest}.json"

    def get(self, store: str, terms: list[str]) -> dict | None:
        path = self._path(store, terms)
        try:
            entry = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - entry.get("saved_at", 0) > self.ttl:
            return None
        return entry.get("response")

    def put(self, store: str, terms: list[str], response: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(store, terms).write_text(json.dumps({"saved_at": time.time(), "response": response}))


# --------------------------------------------------------------------------- matching & selection


@dataclass
class Candidate:
    name: str
    product_id: str
    session_token: str
    price_cents: int | None
    suggested_units: int
    barcode: str = ""
    size: str = ""
    bio: bool = False
    promo: bool = False


@dataclass
class CartLine:
    term: str
    product: str
    product_id: str
    session_token: str
    units: int
    price_cents: int | None
    source: str  # "memory", "picked", "auto"
    barcode: str = ""


@dataclass
class Plan:
    store: str
    currency: str = "CHF"
    lines: list[CartLine] = field(default_factory=list)
    choices: dict[str, list[Candidate]] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def _size_label(quantity: dict | None) -> str:
    if not quantity:
        return ""
    if quantity.get("pieces"):
        return f"{quantity['pieces']:g} pcs"
    if quantity.get("milliliters"):
        ml = quantity["milliliters"]
        return f"{ml / 1000:g} l" if ml >= 1000 else f"{ml:g} ml"
    if quantity.get("grams"):
        g = quantity["grams"]
        return f"{g / 1000:g} kg" if g >= 1000 else f"{g:g} g"
    if quantity.get("bunches"):
        return f"{quantity['bunches']:g} bunch"
    return ""


def barcode_for(product: dict) -> str:
    """GTIN when Pepesto has one, otherwise the store's article number from the product URL."""
    gtins = product.get("gtin14") or []
    if gtins:
        return gtins[0]
    match = re.search(r"/(?:p|product(?:/mo)?)/(\d+)/?$", product.get("product_id", ""))
    return match.group(1) if match else ""


def candidates_from_item(item: dict) -> list[Candidate]:
    result = []
    for entry in item.get("products") or []:
        product = entry.get("product") or {}
        token = entry.get("session_token") or product.get("session_token")
        if not token:
            continue
        price = (product.get("price") or {}).get("price")
        promo = bool(((product.get("price") or {}).get("promotion") or {}).get("promo"))
        result.append(
            Candidate(
                name=product.get("product_name", "?"),
                product_id=product.get("product_id", ""),
                session_token=token,
                price_cents=price,
                suggested_units=int(entry.get("num_units_to_buy") or 0),
                barcode=barcode_for(product),
                size=_size_label(product.get("quantity")),
                bio=bool((product.get("classification") or {}).get("is_bio")),
                promo=promo,
            )
        )
    return result


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-zà-ÿ]+", text.lower()) if len(w) > 2}


def _similar(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


def _match_score(term: str, item: dict) -> float:
    names = _words(item.get("item_name", ""))
    for cand in item.get("products") or []:
        names |= _words((cand.get("product") or {}).get("category", ""))
    products: set[str] = set()
    for cand in (item.get("products") or [])[:5]:
        products |= _words((cand.get("product") or {}).get("product_name", ""))
    score = 0.0
    for word in _words(term):
        if any(_similar(word, n) for n in names):
            score += 1
        elif any(_similar(word, p) for p in products):
            score += 0.25
    return score


def map_items_to_terms(terms: list[str], items: list[dict]) -> dict[str, dict]:
    """Pepesto renames and reorders items ("milk" -> "Cow's milk"); match them back to the user's words."""
    pairs = sorted(
        ((_match_score(t, it), -abs(ti - ii), ti, ii) for ti, t in enumerate(terms) for ii, it in enumerate(items)),
        reverse=True,
    )
    mapping: dict[str, dict] = {}
    used: set[int] = set()
    for score, _, ti, ii in pairs:
        if score <= 0:
            break
        if terms[ti] in mapping or ii in used:
            continue
        mapping[terms[ti]] = items[ii]
        used.add(ii)
    leftover_terms = [t for t in terms if t not in mapping]
    leftover_items = [it for ii, it in enumerate(items) if ii not in used]
    if len(leftover_terms) == len(leftover_items):
        mapping.update(zip(leftover_terms, leftover_items))
    return mapping


def parse_picks(values: list[str]) -> dict[str, str]:
    picks = {}
    for value in values or []:
        if "=" not in value:
            raise CliError(f"--pick expects TERM=N, TERM=text or TERM=skip, got {value!r}")
        term, _, choice = value.rpartition("=")
        picks[normalize_term(term)] = choice.strip()
    return picks


def build_plan(
    items: list[ListItem],
    store: str,
    memory: Memory,
    client: PepestoClient | None,
    cache: SearchCache | None,
    picks: dict[str, str],
    auto: bool,
    interactive: bool,
    use_memory: bool,
    locale: str | None,
    prompt=input,
) -> Plan:
    plan = Plan(store=store)
    to_search: list[ListItem] = []
    for item in items:
        assoc = memory.get(item.term, store) if use_memory else None
        if assoc and item.term not in picks:
            plan.lines.append(
                CartLine(
                    term=item.term,
                    product=assoc.product,
                    product_id=assoc.product_id,
                    session_token=assoc.session_token,
                    units=item.units or 1,
                    price_cents=_price_to_cents(assoc.price),
                    source="memory",
                    barcode=assoc.barcode,
                )
            )
        else:
            to_search.append(item)

    if not to_search:
        return plan
    if client is None:
        raise CliError("An API client is required to search for new items")

    terms = [i.term for i in to_search]
    response = cache.get(store, terms) if cache else None
    if response is None:
        log(f"Searching {store} for {len(terms)} item(s): {', '.join(terms)}")
        response = client.search_products([i.raw for i in to_search], store, locale)
        if cache:
            cache.put(store, terms, response)
    else:
        log(f"Using cached search results for: {', '.join(terms)}")
    plan.currency = response.get("currency") or plan.currency
    mapping = map_items_to_terms(terms, response.get("items") or [])

    for item in to_search:
        candidates = candidates_from_item(mapping.get(item.term) or {})
        if not candidates:
            plan.unresolved.append(item.raw)
            continue
        plan.choices[item.term] = candidates
        choice = picks.get(item.term)
        if choice is None and interactive:
            choice = prompt_choice(item.term, candidates, plan.currency, prompt)
        if choice is None and not auto:
            plan.pending.append(item.term)
            continue
        if choice is not None and choice.lower() in {"skip", "s", "-"}:
            continue
        if choice is not None and choice.lower() in {"text", "t"}:
            plan.unresolved.append(item.raw)
            continue
        index = 0
        if choice is not None:
            try:
                index = int(choice) - 1
            except ValueError:
                raise CliError(f"Invalid choice {choice!r} for {item.term!r}") from None
            if not 0 <= index < len(candidates):
                raise CliError(f"Choice {choice} for {item.term!r} is out of range 1..{len(candidates)}")
        cand = candidates[index]
        plan.lines.append(
            CartLine(
                term=item.term,
                product=cand.name,
                product_id=cand.product_id,
                session_token=cand.session_token,
                units=item.units or (max(1, cand.suggested_units) if _QTY_RE.match(item.raw) else 1),
                price_cents=cand.price_cents,
                source="auto" if choice is None else "picked",
                barcode=cand.barcode,
            )
        )
    return plan


def _price_to_cents(text: str) -> int | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)", text or "")
    return round(float(match.group(1).replace(",", ".")) * 100) if match else None


def fmt_price(cents: int | None, currency: str) -> str:
    return f"{currency} {cents / 100:.2f}" if cents is not None else "?"


def describe(c: Candidate, currency: str) -> str:
    tags = [t for t, on in (("bio", c.bio), ("promo", c.promo)) if on]
    extra = f" [{', '.join(tags)}]" if tags else ""
    size = f" ({c.size})" if c.size and c.size.split()[0] not in c.name else ""
    return f"{c.name}{size} - {fmt_price(c.price_cents, currency)}{extra}"


def prompt_choice(term: str, candidates: list[Candidate], currency: str, prompt, limit: int = 8) -> str:
    print(f"\n{term}:", file=sys.stderr)
    for i, cand in enumerate(candidates[:limit], 1):
        print(f"  {i}. {describe(cand, currency)}", file=sys.stderr)
    while True:
        answer = prompt(
            f"Pick 1-{min(limit, len(candidates))}, Enter for 1, 't' to let Pepesto find it at checkout, 's' to skip: "
        ).strip()
        if not answer:
            return "1"
        if answer.lower() in {"s", "skip"}:
            return "skip"
        if answer.lower() in {"t", "text"}:
            return "text"
        if answer.isdigit() and 1 <= int(answer) <= min(limit, len(candidates)):
            return answer
        print("  Not a valid choice.", file=sys.stderr)


def remember_choices(plan: Plan, memory: Memory, remember_auto: bool) -> int:
    today = dt.date.today().isoformat()
    saved = 0
    for line in plan.lines:
        if line.source == "picked" or (line.source == "auto" and remember_auto):
            memory.put(
                Association(
                    term=line.term,
                    store=plan.store,
                    product=line.product,
                    product_id=line.product_id,
                    session_token=line.session_token,
                    barcode=line.barcode,
                    price=fmt_price(line.price_cents, plan.currency) if line.price_cents is not None else "",
                    last_used=today,
                )
            )
            saved += 1
        elif line.source == "memory":
            assoc = memory.get(line.term, plan.store)
            if assoc:
                assoc.last_used = today
    return saved


# --------------------------------------------------------------------------- output


def plan_to_dict(plan: Plan, checkout: dict | None) -> dict:
    total = sum((l.price_cents or 0) * l.units for l in plan.lines)
    return {
        "store": plan.store,
        "currency": plan.currency,
        "cart": [
            {
                "term": l.term,
                "product": l.product,
                "product_id": l.product_id,
                "barcode": l.barcode or None,
                "units": l.units,
                "unit_price": l.price_cents / 100 if l.price_cents is not None else None,
                "source": l.source,
            }
            for l in plan.lines
        ],
        "unresolved_items": plan.unresolved,
        "estimated_total": round(total / 100, 2),
        "pending_choices": {
            term: [
                {"choice": i, "product": c.name, "size": c.size, "price": (c.price_cents or 0) / 100,
                 "bio": c.bio, "promo": c.promo, "product_id": c.product_id}
                for i, c in enumerate(plan.choices[term], 1)
            ]
            for term in plan.pending
        },
        "checkout_url": (checkout or {}).get("mobile_hosted_url"),
        "session_id": (checkout or {}).get("session_id"),
    }


def render_markdown(plan: Plan, checkout: dict | None) -> str:
    out = []
    if plan.pending:
        out.append(f"## Choices needed ({plan.store})\n")
        for term in plan.pending:
            out.append(f"**{term}**")
            for i, cand in enumerate(plan.choices[term][:8], 1):
                out.append(f"  {i}. {describe(cand, plan.currency)}")
            out.append("")
        picks = " ".join(f"--pick {shlex.quote(t + '=1')}" for t in plan.pending)
        out.append(f"Re-run with {picks} (or --auto to take the top match). Use TERM=text to let Pepesto "
                   "find the item itself at checkout, TERM=skip to drop it; --json lists every candidate.\n")

    if plan.lines or plan.unresolved:
        out.append(f"## Cart: {plan.store}\n")
        out.append("| # | item | product | qty | unit price | from |")
        out.append("|---|---|---|---|---|---|")
        total = 0
        for i, line in enumerate(plan.lines, 1):
            total += (line.price_cents or 0) * line.units
            out.append(
                f"| {i} | {_md_cell(line.term)} | {_md_cell(line.product)} | {line.units} | "
                f"{fmt_price(line.price_cents, plan.currency)} | {line.source} |"
            )
        for raw in plan.unresolved:
            out.append(f"| - | {_md_cell(raw)} | resolved by Pepesto at checkout | 1 | ? | search |")
        out.append(f"\nEstimated total: **{fmt_price(total, plan.currency)}** (last known prices)")

    if checkout:
        out.append(f"\nReady to pay: {checkout.get('mobile_hosted_url')}")
        out.append("Open it on a phone (or scan the QR code it shows on desktop); the Pepesto app fills your "
                   f"{plan.store} basket and you pay there.")
    return "\n".join(out)


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- commands


def _store(value: str) -> str:
    store = STORES.get(value.lower())
    if not store:
        raise CliError(f"Unknown store {value!r}; use migros or coop")
    return store


def _read_list(args) -> str:
    parts = []
    if args.items:
        parts.append("\n".join(args.items))
    if args.file:
        parts.append(sys.stdin.read() if args.file == "-" else Path(args.file).read_text())
    if not parts and not sys.stdin.isatty():
        parts.append(sys.stdin.read())
    text = "\n".join(parts).strip()
    if not text:
        raise CliError("Empty grocery list. Pass items as arguments, --file PATH, or pipe them on stdin.")
    return text


def cmd_shop(args) -> int:
    store = _store(args.store)
    items = parse_grocery_list(_read_list(args))
    memory = Memory(args.memory)
    picks = parse_picks(args.pick)
    interactive = not args.auto and not args.json and sys.stdin.isatty() and not args.no_prompt
    needs_api = args.fresh or any(memory.get(i.term, store) is None or i.term in picks for i in items)
    client = PepestoClient(resolve_api_key(), verbose=args.verbose) if (needs_api or not args.dry_run) else None
    cache = None if args.no_cache else SearchCache(args.home / "cache")

    plan = build_plan(items, store, memory, client, cache, picks, args.auto, interactive,
                      use_memory=not args.fresh, locale=args.locale)
    saved = remember_choices(plan, memory, args.remember_auto)
    memory.save()
    if saved:
        log(f"Remembered {saved} choice(s) in {args.memory}")

    checkout = None
    if plan.pending:
        log("Some items need a choice; no cart created yet.")
    elif args.dry_run:
        log("Dry run: skipping checkout.")
    elif not plan.lines and not plan.unresolved:
        raise CliError("Nothing to put in the cart.")
    else:
        if len(plan.lines) + len(plan.unresolved) > MAX_CHECKOUT_ITEMS:
            raise CliError(f"Pepesto accepts at most {MAX_CHECKOUT_ITEMS} items per cart")
        if len(plan.unresolved) > MAX_UNRESOLVED_ITEMS:
            raise CliError(f"Pepesto accepts at most {MAX_UNRESOLVED_ITEMS} unmatched items per cart: "
                           + ", ".join(plan.unresolved))
        skus = [{"session_token": l.session_token, "num_units_to_buy": l.units} for l in plan.lines]
        checkout = client.checkout(store, skus, plan.unresolved, args.locale)

    if client and client.eurocents_charged:
        log(f"Pepesto credits used: EUR {client.eurocents_charged / 100:.2f}")
    if args.json:
        print(json.dumps(plan_to_dict(plan, checkout), indent=2, ensure_ascii=False))
    else:
        print(render_markdown(plan, checkout))
    return 2 if plan.pending else 0


def cmd_memory(args) -> int:
    memory = Memory(args.memory)
    if args.action == "forget":
        if not args.term:
            raise CliError("memory forget needs a term")
        removed = memory.forget(args.term, _store(args.store) if args.store else None)
        memory.save()
        print(f"Forgot {removed} association(s) for {normalize_term(args.term)!r}")
        return 0 if removed else 1
    if args.action == "path":
        print(args.memory)
        return 0
    if not memory.rows:
        print(f"No associations yet ({args.memory})")
        return 0
    print("| term | store | product | barcode | price | last used |")
    print("|---|---|---|---|---|---|")
    for key in sorted(memory.rows):
        a = memory.rows[key]
        print(f"| {a.term} | {a.store} | {_md_cell(a.product)} | {a.barcode or '-'} | {a.price} | {a.last_used} |")
    return 0


def cmd_credits(args) -> int:
    client = PepestoClient(resolve_api_key())
    print(f"EUR {client.credits() / 100:.2f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pepesto_cart.py", description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="state directory (default ~/.pepesto)")
    parser.add_argument("--memory", type=Path, help="memory markdown file (default <home>/memory.md)")
    sub = parser.add_subparsers(dest="command", required=True)

    shop = sub.add_parser("shop", help="build a cart from a grocery list")
    shop.add_argument("items", nargs="*", help="grocery items (commas or separate args)")
    shop.add_argument("-f", "--file", help="read the list from a file ('-' for stdin)")
    shop.add_argument("-s", "--store", default=os.environ.get("PEPESTO_STORE", "migros"), help="migros or coop")
    shop.add_argument("--pick", action="append", metavar="TERM=N", help="candidate N for TERM; TERM=text lets Pepesto find it at checkout; TERM=skip drops it")
    shop.add_argument("--auto", action="store_true", help="take the top match for items without a choice")
    shop.add_argument("--remember-auto", action="store_true", help="also remember --auto choices")
    shop.add_argument("--no-prompt", action="store_true", help="never prompt; list choices and exit 2 instead")
    shop.add_argument("--fresh", action="store_true", help="ignore memory and search everything again")
    shop.add_argument("--dry-run", action="store_true", help="plan the cart but don't create the checkout link")
    shop.add_argument("--no-cache", action="store_true", help="don't reuse recent search results")
    shop.add_argument("--locale", default=os.environ.get("PEPESTO_LOCALE"), help="e.g. de-CH, fr-CH")
    shop.add_argument("--json", action="store_true", help="machine-readable output (implies --no-prompt)")
    shop.add_argument("-v", "--verbose", action="store_true")
    shop.set_defaults(func=cmd_shop)

    mem = sub.add_parser("memory", help="show or edit remembered associations")
    mem.add_argument("action", nargs="?", default="list", choices=["list", "forget", "path"])
    mem.add_argument("term", nargs="?")
    mem.add_argument("-s", "--store")
    mem.set_defaults(func=cmd_memory)

    cred = sub.add_parser("credits", help="show remaining Pepesto credits")
    cred.set_defaults(func=cmd_credits)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.memory = args.memory or Path(os.environ.get("PEPESTO_MEMORY_FILE", args.home / "memory.md"))
    try:
        return args.func(args)
    except CliError as exc:
        log(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
