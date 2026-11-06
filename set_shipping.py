"""Set international shipping rates in Shopify from the Pakistan Post parcel tariff (w.e.f. 1 February 2026).

    python set_shipping.py            test: reads the current shipping setup and lists what would change; changes nothing
    python set_shipping.py --apply    writes the zones and rates

The tariff (data/pakistan_post_tariff.csv) gives, per country, the price of the first kg and of each additional kg in
PKR. The store sells in USD, so every price is divided by 280 (PKR_PER_USD) and rounded to the cent.

Rates are weight based, one rate per kg: up to 1 kg = first kg; 1-2 kg = first kg + 1 additional kg; and so on up to
MAX_KG. Countries with the same two prices share one shipping zone (fewer zones to look at in the admin). Rates have no
delivery time (no description).

Safety: the program only works in the General (default) shipping profile. It never touches a zone that has a country
outside the tariff (e.g. Pakistan). An existing zone whose countries are all in the tariff is replaced, but only with
--apply and only after the test run has listed it. Each run writes output/shipping_report.csv: every country, its PKR
prices, the USD rate per kg, and the zone it goes to; plus the zones that would be replaced, with their countries and
rates, so they can be put back by hand.
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

from lc_images.shopify import Shopify

TARIFF = Path(__file__).resolve().parent / "data" / "pakistan_post_tariff.csv"
REPORT = Path("output/shipping_report.csv")
PKR_PER_USD = 280
MAX_KG = 20  # heaviest order that gets a rate (20 kg is about 20-28 abayas); heavier orders get no rate
RATE_NAME = "International Shipping"
ZONE_PREFIX = "Pakistan Post"
# Countries Shopify may refuse as shipping destinations: each gets a zone of its own, so a refusal never blocks others
OWN_ZONE = {"KP", "CU", "IR", "SY"}

# Read in small pages: Shopify refuses a single query that asks for too much at once
PROFILES = """
query {
  deliveryProfiles(first: 20) {
    nodes { id name default profileLocationGroups { locationGroup { id } } }
  }
}"""

ZONES = """
query($profile: ID!, $group: ID!, $after: String) {
  deliveryProfile(id: $profile) {
    profileLocationGroups(locationGroupId: $group) {
      locationGroupZones(first: 10, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          zone { id name countries { code { countryCode restOfWorld } } }
          methodDefinitions(first: 25) {
            nodes {
              id name active
              rateProvider { ... on DeliveryRateDefinition { price { amount currencyCode } } }
              methodConditions { field operator conditionCriteria { ... on Weight { unit value } ... on MoneyV2 { amount currencyCode } } }
            }
          }
        }
      }
    }
  }
}"""


def location_group_zones(shopify, profile_id, group_id):
    after = None
    while True:
        groups = shopify.gql(ZONES, {"profile": profile_id, "group": group_id, "after": after})["deliveryProfile"]["profileLocationGroups"]
        page = groups[0]["locationGroupZones"] if groups else {"nodes": [], "pageInfo": {"hasNextPage": False}}
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = page["pageInfo"]["endCursor"]


UPDATE = """
mutation($id: ID!, $profile: DeliveryProfileInput!) {
  deliveryProfileUpdate(id: $id, profile: $profile) {
    profile { id }
    userErrors { field message }
  }
}"""


def usd(pkr):
    return round(pkr / PKR_PER_USD + 1e-9, 2)


def load_tariff(path=TARIFF):
    """[(code, country, first PKR, additional PKR)] for the countries Shopify can ship to."""
    rows, skipped = [], []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            entry = (row["code"], row["country"], int(row["first_kg_pkr"]), int(row["additional_kg_pkr"]))
            (skipped if row["code"] == "--" else rows).append(entry)
    return rows, skipped


def kg_prices(first, additional):
    """USD price for each kg band: [(band upper kg, price)]."""
    return [(kg, usd(first + (kg - 1) * additional)) for kg in range(1, MAX_KG + 1)]


def methods(first, additional):
    """One weight-based rate per kg band. Bands do not overlap: 0-1000 g, 1001-2000 g, ..."""
    out = []
    for kg, price in kg_prices(first, additional):
        low = 0 if kg == 1 else (kg - 1) * 1000 + 1
        out.append({
            "name": RATE_NAME,
            "active": True,
            "rateDefinition": {"price": {"amount": f"{price:.2f}", "currencyCode": "USD"}},
            "weightConditionsToCreate": [
                {"criteria": {"unit": "GRAMS", "value": low}, "operator": "GREATER_THAN_OR_EQUAL_TO"},
                {"criteria": {"unit": "GRAMS", "value": kg * 1000}, "operator": "LESS_THAN_OR_EQUAL_TO"},
            ],
        })
    return out


def plan_zones(tariff):
    """Group countries with the same two prices into one zone: [(zone name, first, additional, [(code, country)])]."""
    groups = {}
    for code, country, first, additional in tariff:
        key = (first, additional, code if code in OWN_ZONE else "")
        groups.setdefault(key, []).append((code, country))
    zones = []
    for (first, additional, own), countries in sorted(groups.items(), key=lambda g: (g[0][0], g[0][1], g[0][2])):
        name = f"{ZONE_PREFIX} {first:,}+{additional:,} PKR"
        if own:
            name += f" ({own})"
        zones.append((name, first, additional, countries))
    return zones


def describe_rates(method_nodes):
    parts = []
    for m in method_nodes:
        price = ((m.get("rateProvider") or {}).get("price") or {})
        conditions = ", ".join(
            f"{c['field'].lower()} {'>=' if c['operator'].startswith('GREATER') else '<='} "
            + (f"{c['conditionCriteria'].get('value')} {c['conditionCriteria'].get('unit', '').lower()}" if "value" in c["conditionCriteria"]
               else f"{c['conditionCriteria'].get('amount')} {c['conditionCriteria'].get('currencyCode')}")
            for c in m.get("methodConditions") or []
        )
        parts.append(f"{m['name']} {price.get('amount', '?')} {price.get('currencyCode', '')}" + (f" [{conditions}]" if conditions else ""))
    return "; ".join(parts)


def run(shopify, apply=False, replace=(), log=print):
    """replace: names of existing zones to delete even though they also hold countries outside the tariff (those
    countries then have no shipping rate)."""
    tariff, skipped = load_tariff()
    codes = {code for code, *_ in tariff}
    zones = plan_zones(tariff)

    currency = shopify.gql("query { shop { currencyCode } }")["shop"]["currencyCode"]
    log(f"Store currency: {currency}")
    profiles = shopify.gql(PROFILES)["deliveryProfiles"]["nodes"]
    general = next((p for p in profiles if p["default"]), None)
    if general is None:
        raise RuntimeError("No General (default) shipping profile found")
    groups = general["profileLocationGroups"]
    if not groups:
        raise RuntimeError("The General shipping profile has no locations")
    log(f"Shipping profile: {general['name']} ({len(groups)} location group(s))")

    report, to_delete, blocked = [], [], []
    for group in groups:
        for node in location_group_zones(shopify, general["id"], group["locationGroup"]["id"]):
            zone = node["zone"]
            zone_codes = [c["code"]["countryCode"] or "REST_OF_WORLD" for c in zone["countries"]]
            rates = describe_rates(node["methodDefinitions"]["nodes"])
            inside = [c for c in zone_codes if c in codes]
            if not inside:
                status = "Kept (no tariff country)"
            elif len(inside) == len(zone_codes):
                status = "Replaced"
                to_delete.append(zone["id"])
            elif zone["name"] in replace:
                others = [c for c in zone_codes if c not in codes]
                status = "Replaced (asked for); these countries will have no shipping: " + " ".join(others)
                to_delete.append(zone["id"])
            else:
                others = [c for c in zone_codes if c not in codes]
                status = "BLOCKED: also holds countries outside the tariff: " + " ".join(others)
                blocked.append(zone["name"])
            report.append(["Existing zone", zone["name"], " ".join(zone_codes), "", "", "", rates, status])
            log(f"Existing zone '{zone['name']}' ({len(zone_codes)} countries): {status}")

    if currency != "USD":
        blocked.append(f"store currency is {currency}, not USD")
        log(f"\nStopped: the rates are worked out in USD (PKR / {PKR_PER_USD}) but Shopify stores shipping rates in the "
            f"store currency, {currency}. Nothing will be written until this is settled.")
    if any(b for b in blocked if not b.startswith("store currency")):
        log("\nStopped: these existing zones mix tariff countries with other countries, so the program will not touch "
            "them. Move the other countries to their own zone (or delete the zone) in Shopify, then run again: "
            + ", ".join(blocked))

    for name, first, additional, countries in zones:
        prices = kg_prices(first, additional)
        for code, country in countries:
            report.append(["New zone", name, code, country, first, additional,
                           " | ".join(f"{kg} kg ${p:.2f}" for kg, p in prices[:5]) + f" ... {MAX_KG} kg ${prices[-1][1]:.2f}",
                           "Would create" if not apply else ""])
    for no_code in skipped:
        report.append(["Not set", "", "", no_code[1], no_code[2], no_code[3], "", "Not a separate country in Shopify (covered by its country's rate)"])

    results = {}
    if apply and not blocked:
        group_id = groups[0]["locationGroup"]["id"]
        if to_delete:
            data = shopify.gql(UPDATE, {"id": general["id"], "profile": {"zonesToDelete": to_delete}})["deliveryProfileUpdate"]
            if data["userErrors"]:
                raise RuntimeError("Shopify refused to replace the old zones: " + json.dumps(data["userErrors"]))
            log(f"Removed {len(to_delete)} old zone(s)")
        for name, first, additional, countries in zones:
            zone = {"name": name, "countries": [{"code": code, "includeAllProvinces": True} for code, _ in countries],
                    "methodDefinitionsToCreate": methods(first, additional)}
            data = shopify.gql(UPDATE, {"id": general["id"], "profile": {"locationGroupsToUpdate": [{"id": group_id, "zonesToCreate": [zone]}]}})["deliveryProfileUpdate"]
            errors = data["userErrors"]
            results[name] = "Created" if not errors else "Failed: " + "; ".join(e.get("message", "") for e in errors)
            log(f"{results[name]}: {name} ({', '.join(c for c, _ in countries)})")
            time.sleep(0.5)
        for row in report:
            if row[0] == "New zone":
                row[7] = results.get(row[1], "")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
        out = csv.writer(f)
        out.writerow(["What", "Zone", "Country code(s)", "Country", "First kg PKR", "Additional kg PKR", "Rates (USD)", "Result"])
        out.writerows(report)

    created = sum(1 for v in results.values() if v == "Created")
    failed = [k for k, v in results.items() if v != "Created"]
    log(f"\n{len(tariff)} countries in {len(zones)} zones, {MAX_KG} rates each (1 to {MAX_KG} kg). "
        f"Existing zones to replace: {len(to_delete)}. Report: {REPORT}")
    if apply and not blocked:
        log(f"Created {created} zones" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return report


def main():
    ap = argparse.ArgumentParser(description="Set international shipping rates from the Pakistan Post tariff")
    ap.add_argument("--apply", action="store_true", help="write the zones and rates (without it: test only)")
    ap.add_argument("--replace", default="", help="existing zone names to delete although they hold other countries, comma-separated")
    args = ap.parse_args()
    started = time.time()
    run(Shopify(), apply=args.apply, replace=[z.strip() for z in args.replace.split(",") if z.strip()])
    print(f"Finished in {int(time.time() - started)} s.")


if __name__ == "__main__":
    sys.exit(main())
