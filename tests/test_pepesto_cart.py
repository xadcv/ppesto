import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pepesto_cart as pc  # noqa: E402


def product(name, pid, price, units=1, **extra):
    p = {"product_name": name, "product_id": pid, "price": {"price": price, "promotion": {}},
         "session_token": f"tok-{pid}"}
    p.update(extra)
    return {"product": p, "session_token": f"tok-{pid}", "num_units_to_buy": units}


PRODUCTS_RESPONSE = {
    "currency": "CHF",
    "items": [
        {"item_name": "Eggs", "products": [
            product("Organic Free-Range Eggs 4 pack", "m/eggs-bio", 350, classification={"is_bio": True}),
            product("Free-range eggs 6 pieces", "m/eggs-6", 330),
        ]},
        {"item_name": "Cow's milk", "products": [
            product("Valflora Whole Milk 1 L", "m/milk", 135, gtin14=["07610200000000"]),
        ]},
    ],
}


class FakeClient:
    def __init__(self, response=PRODUCTS_RESPONSE):
        self.response = response
        self.searches = []
        self.checkouts = []
        self.eurocents_charged = 0

    def search_products(self, lines, store, locale):
        self.searches.append((lines, store))
        return self.response

    def checkout(self, store, skus, unresolved, locale):
        self.checkouts.append((store, skus, unresolved))
        return {"session_id": "s1", "mobile_hosted_url": "https://s.pepesto.com/m/abc"}


class ParsingTests(unittest.TestCase):
    def test_parse_list_strips_quantities_bullets_and_duplicates(self):
        items = pc.parse_grocery_list("- 2 tomatoes\n3x Eggs, eggs\n* [ ] 1 l milk\n500 g flour; lemons")
        self.assertEqual([i.term for i in items], ["tomatoes", "eggs", "milk", "flour", "lemons"])
        self.assertEqual([i.units for i in items], [None, 3, None, None, None])

    def test_barcode_prefers_gtin_then_store_article_number(self):
        self.assertEqual(pc.barcode_for({"gtin14": ["0761"], "product_id": "x"}), "0761")
        self.assertEqual(pc.barcode_for({"product_id": "https://www.migros.ch/de/product/196045700600"}),
                         "196045700600")
        self.assertEqual(pc.barcode_for({"product_id": "https://www.migros.ch/de/product/mo/10441769"}), "10441769")
        self.assertEqual(pc.barcode_for({"product_id": "https://www.coop.ch/de/x/eier/p/3080191"}), "3080191")

    def test_items_mapped_back_to_user_terms_when_counts_differ(self):
        items = [{"item_name": "Cow's milk", "products": []}, {"item_name": "Eggs", "products": []}]
        mapping = pc.map_items_to_terms(["eggs", "milk", "saffron"], items)
        self.assertEqual(mapping["eggs"]["item_name"], "Eggs")
        self.assertEqual(mapping["milk"]["item_name"], "Cow's milk")
        self.assertNotIn("saffron", mapping)


class MemoryTests(unittest.TestCase):
    def test_round_trip_markdown_with_pipes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "memory.md"
            mem = pc.Memory(path)
            mem.put(pc.Association("Eggs", "migros.ch", "Eggs | bio 12x", "m/1", "tok", "0761", "CHF 3.50", "2026-10-30"))
            mem.save()
            text = path.read_text()
            self.assertIn("| eggs | migros.ch | Eggs \\| bio 12x |", text)
            loaded = pc.Memory(path).get("eggs", "migros.ch")
            self.assertEqual(loaded.product, "Eggs | bio 12x")
            self.assertEqual(loaded.session_token, "tok")
            self.assertIsNone(pc.Memory(path).get("eggs", "coop.ch"))

    def test_hand_edited_file_with_store_alias_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "memory.md"
            path.write_text("# notes\n\n| term | store | product | session_token |\n|--|--|--|--|\n"
                            "| 2 Eggs | Coop | Bio eggs | tok-x |\n")
            self.assertEqual(pc.Memory(path).get("eggs", "coop.ch").session_token, "tok-x")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = pc.Memory(Path(self.tmp.name) / "memory.md")

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, text, client, **kw):
        defaults = dict(picks={}, auto=False, interactive=False, use_memory=True, locale=None)
        defaults.update(kw)
        with redirect_stderr(io.StringIO()):
            return pc.build_plan(pc.parse_grocery_list(text), "migros.ch", self.memory, client, None, **defaults)

    def test_without_choices_items_are_pending(self):
        plan = self.plan("eggs, milk", FakeClient())
        self.assertEqual(plan.pending, ["eggs", "milk"])
        self.assertEqual(plan.lines, [])

    def test_pick_is_remembered_and_next_run_skips_api(self):
        client = FakeClient()
        plan = self.plan("eggs, milk", client, picks={"eggs": "1", "milk": "1"})
        self.assertEqual([l.product_id for l in plan.lines], ["m/eggs-bio", "m/milk"])
        self.assertEqual(pc.remember_choices(plan, self.memory, remember_auto=False), 2)
        self.assertEqual(self.memory.get("milk", "migros.ch").barcode, "07610200000000")

        second = FakeClient()
        plan2 = self.plan("3x eggs\nmilk", second)
        self.assertEqual(second.searches, [])
        self.assertEqual([(l.source, l.units) for l in plan2.lines], [("memory", 3), ("memory", 1)])

    def test_auto_not_remembered_unless_asked(self):
        plan = self.plan("eggs", FakeClient(), auto=True)
        self.assertEqual(plan.lines[0].source, "auto")
        self.assertEqual(pc.remember_choices(plan, self.memory, remember_auto=False), 0)
        self.assertEqual(pc.remember_choices(plan, self.memory, remember_auto=True), 1)

    def test_only_unremembered_items_are_searched(self):
        self.memory.put(pc.Association("eggs", "migros.ch", "Eggs", "m/eggs-6", "tok-m/eggs-6"))
        client = FakeClient({"currency": "CHF", "items": [PRODUCTS_RESPONSE["items"][1]]})
        plan = self.plan("eggs, milk", client, auto=True)
        self.assertEqual(client.searches, [(["milk"], "migros.ch")])
        self.assertEqual([l.source for l in plan.lines], ["memory", "auto"])

    def test_unmatched_items_go_to_unresolved_and_skip_works(self):
        client = FakeClient({"currency": "CHF", "items": PRODUCTS_RESPONSE["items"]})
        plan = self.plan("eggs, milk, saffron threads", client, picks={"eggs": "skip"}, auto=True)
        self.assertEqual([l.term for l in plan.lines], ["milk"])
        self.assertEqual(plan.unresolved, ["saffron threads"])

    def test_interactive_prompt(self):
        answers = iter(["9", "2", ""])
        plan = self.plan("eggs, milk", FakeClient(), interactive=True, prompt=lambda _: next(answers))
        self.assertEqual([l.product_id for l in plan.lines], ["m/eggs-6", "m/milk"])
        self.assertEqual({l.source for l in plan.lines}, {"picked"})

    def test_out_of_range_pick_errors(self):
        with self.assertRaises(pc.CliError):
            self.plan("eggs", FakeClient(), picks={"eggs": "7"})


class ApiKeyTests(unittest.TestCase):
    def test_env_then_dotenv_then_command(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pc.resolve_api_key({"PEPESTO_API_KEY": "env"}, [Path(d)]), "env")
            Path(d, ".env").write_text("export PEPESTO_API_KEY='dot'\n")
            self.assertEqual(pc.resolve_api_key({}, [Path(d)]), "dot")
            Path(d, ".env").write_text("PEPESTO_API_KEY_CMD=echo vault-key\n")
            self.assertEqual(pc.resolve_api_key({}, [Path(d)]), "vault-key")
            Path(d, ".env").unlink()
            with self.assertRaises(pc.CliError):
                pc.resolve_api_key({}, [Path(d)])


class ClientTests(unittest.TestCase):
    def test_plain_text_200_error_is_reported(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.headers = {"Pepesto-Eurocents-Charged": "32"}
        resp.read.return_value = b"products_handler.go:177: Error 400 INVALID_ARGUMENT"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            client = pc.PepestoClient("k")
            with self.assertRaisesRegex(pc.CliError, "INVALID_ARGUMENT"):
                client.call("products", {})
            self.assertEqual(client.eurocents_charged, 32)


class CliTests(unittest.TestCase):
    def test_shop_command_end_to_end_with_fake_client(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeClient()
            argv = ["--home", d, "shop", "-s", "migros", "eggs, milk", "--pick", "eggs=2", "--auto", "--json"]
            out = io.StringIO()
            with mock.patch.object(pc, "PepestoClient", return_value=fake), \
                    mock.patch.object(pc, "resolve_api_key", return_value="k"), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(pc.main(argv), 0)
            result = json.loads(out.getvalue())
            self.assertEqual(result["checkout_url"], "https://s.pepesto.com/m/abc")
            self.assertEqual(result["estimated_total"], 4.65)
            self.assertEqual(fake.checkouts[0][1], [{"session_token": "tok-m/eggs-6", "num_units_to_buy": 1},
                                                    {"session_token": "tok-m/milk", "num_units_to_buy": 1}])
            memory = Path(d, "memory.md").read_text()
            self.assertIn("| eggs | migros.ch | Free-range eggs 6 pieces |", memory)
            self.assertNotIn("| milk |", memory)

    def test_pending_choices_exit_code_2(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with mock.patch.object(pc, "PepestoClient", return_value=FakeClient()), \
                    mock.patch.object(pc, "resolve_api_key", return_value="k"), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(pc.main(["--home", d, "shop", "eggs", "--no-prompt"]), 2)
            self.assertIn("--pick eggs=1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
