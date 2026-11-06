"""Offline tests for set_shipping.py with a fake Shopify."""
import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import set_shipping  # noqa: E402

set_shipping.time.sleep = lambda s: None  # no pauses in tests


def zone(zid, name, codes, rates=()):
    return {"zone": {"id": zid, "name": name, "countries": [{"code": {"countryCode": c, "restOfWorld": False}} for c in codes]},
            "methodDefinitions": {"nodes": [{"id": "m", "name": "Std", "active": True, "rateProvider": {"price": {"amount": r, "currencyCode": "USD"}}, "methodConditions": []} for r in rates]}}


class FakeShopify:
    def __init__(self, zones):
        self.zones, self.updates = zones, []

    def gql(self, query, variables=None):
        if "deliveryProfileUpdate" in query:
            self.updates.append(variables)
            return {"deliveryProfileUpdate": {"profile": {"id": "p"}, "userErrors": []}}
        if "shop {" in query:
            return {"shop": {"currencyCode": getattr(self, "currency", "USD")}}
        if "deliveryProfiles" in query:
            return {"deliveryProfiles": {"nodes": [
                {"id": "gid://shopify/DeliveryProfile/1", "name": "General profile", "default": True,
                 "profileLocationGroups": [{"locationGroup": {"id": "gid://shopify/DeliveryLocationGroup/9"}}]}]}}
        start = int(variables.get("after") or 0)  # pages of one zone, to test paging
        page = self.zones[start:start + 1]
        more = start + 1 < len(self.zones)
        return {"deliveryProfile": {"profileLocationGroups": [{"locationGroupZones": {
            "pageInfo": {"hasNextPage": more, "endCursor": str(start + 1)}, "nodes": page}}]}}


def run(shop, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        set_shipping.REPORT = Path(tmp) / "r.csv"
        set_shipping.run(shop, log=lambda *a: None, **kw)
        return list(csv.reader(open(set_shipping.REPORT, encoding="utf-8-sig")))


def test_tariff_is_complete_and_prices_are_in_usd():
    tariff, skipped = set_shipping.load_tariff()
    assert len(tariff) == 203 and [s[1] for s in skipped] == ["Chile - Easter Islands"]
    uae = next(t for t in tariff if t[0] == "AE")
    assert uae[2:] == (2500, 600)
    prices = set_shipping.kg_prices(2500, 600)
    assert prices[0] == (1, 8.93) and prices[1] == (2, 11.07) and len(prices) == set_shipping.MAX_KG


def test_bands_do_not_overlap():
    m = set_shipping.methods(2500, 600)
    bands = [[c["criteria"]["value"] for c in x["weightConditionsToCreate"]] for x in m]
    assert bands[0] == [0, 1000] and bands[1] == [1001, 2000] and bands[-1][1] == set_shipping.MAX_KG * 1000
    assert all(x["name"] == set_shipping.RATE_NAME and "description" not in x for x in m)


def test_test_mode_changes_nothing_and_apply_replaces_only_tariff_zones():
    shop = FakeShopify([zone("z1", "Domestic", ["PK"], ["2.00"]), zone("z2", "Gulf", ["AE", "SA"], ["20.00"])])
    rows = run(shop)
    assert shop.updates == []
    status = {r[1]: r[7] for r in rows if r[0] == "Existing zone"}
    assert status == {"Domestic": "Kept (no tariff country)", "Gulf": "Replaced"}
    run(shop, apply=True)
    assert shop.updates[0]["profile"] == {"zonesToDelete": ["z2"]}
    created = [u["profile"]["locationGroupsToUpdate"][0]["zonesToCreate"][0] for u in shop.updates[1:]]
    codes = [c["code"] for z in created for c in z["countries"]]
    assert len(codes) == 203 and len(set(codes)) == 203 and "PK" not in codes
    iran = [z for z in created if any(c["code"] == "IR" for c in z["countries"])][0]
    assert [c["code"] for c in iran["countries"]] == ["IR"]  # own zone


def test_mixed_zone_stops_everything():
    shop = FakeShopify([zone("z1", "Asia", ["PK", "AE"])])
    rows = run(shop, apply=True)
    assert shop.updates == [] and any(r[7].startswith("BLOCKED") for r in rows)


def test_named_mixed_zone_can_be_replaced_and_other_currency_stops():
    shop = FakeShopify([zone("z1", "International", ["IL", "AE"])])
    rows = run(shop, apply=True, replace=["International"])
    assert shop.updates[0]["profile"] == {"zonesToDelete": ["z1"]} and "IL" in rows[1][7]
    shop = FakeShopify([zone("z1", "Domestic", ["PK"])])
    shop.currency = "PKR"
    run(shop, apply=True)
    assert shop.updates == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
