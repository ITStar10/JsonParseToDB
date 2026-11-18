#!/usr/bin/env python3
"""Create a demo database with synthetic data using the real Parser (dbcreator.py).

    python tools/make_sample_db.py                      # data/sample.sqlite3 (plain)
    python tools/make_sample_db.py --password secret    # data/sample-encrypted.sqlite3
    python tools/make_sample_db.py --rows 5000 -o data/big.sqlite3

All values are fake. SSNs use the 9xx area range, which is never issued.
"""
import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

BROWSER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BROWSER_DIR.parent / "Parser"))
from dbcreator import import_folder  # noqa: E402

FIRST = ["Alice", "Bruno", "Chloe", "Diego", "Elena", "Farah", "Gabriel", "Hana", "Ivan",
         "Julia", "Kenji", "Lena", "Mateo", "Nora", "Omar", "Priya", "Quinn", "Rosa",
         "Samuel", "Tara", "Uma", "Victor", "Wen", "Ximena", "Yusuf", "Zoe"]
LAST = ["Anderson", "Bauer", "Castillo", "Dubois", "Evans", "Fischer", "Garcia", "Hughes",
        "Ito", "Jensen", "Kowalski", "Lopez", "Moreau", "Nakamura", "Okafor", "Patel",
        "Quintero", "Rossi", "Silva", "Tanaka", "Ueda", "Varga", "Walsh", "Young"]
PLACES = [("Austin", "TX", "787"), ("Boston", "MA", "021"), ("Chicago", "IL", "606"),
          ("Denver", "CO", "802"), ("Miami", "FL", "331"), ("Portland", "OR", "972"),
          ("Seattle", "WA", "981"), ("Phoenix", "AZ", "850"), ("Newark", "NJ", "071"),
          ("Hartford", "CT", "061")]
STREETS = ["Maple Ave", "Oak St", "Pine Rd", "Cedar Ln", "Elm St", "Birch Way", "Lake Dr"]


def person(rng, n):
    first, last = rng.choice(FIRST), rng.choice(LAST)
    city, state, zip3 = rng.choice(PLACES)
    item = {
        "firstName": first, "lastName": last,
        "phone": f"({rng.randint(200, 989)}) 555-{rng.randint(0, 9999):04d}",
        "address": f"{rng.randint(1, 9999)} {rng.choice(STREETS)}",
        "city": city, "state": state, "zip": f"{zip3}{rng.randint(0, 99):02d}",
        "email": f"{first}.{last}{n}@example.com".lower(),
        "dateOfBirth": f"{rng.randint(1950, 2004)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        "ssn": f"9{rng.randint(0, 99):02d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}",
        "useOrders": json.dumps([f"ORD-{rng.randint(1000, 9999)}" for _ in range(rng.randint(0, 3))]),
        "isSold": rng.choice([True, False, None]),
        "id": f"{n:05d}",
    }
    for key in ("phone", "address", "email"):  # some realistic gaps
        if rng.random() < 0.06:
            item[key] = None
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--rows", type=int, default=750)
    parser.add_argument("--password", default="")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    default = "sample-encrypted.sqlite3" if args.password else "sample.sqlite3"
    output = (args.output or BROWSER_DIR / "data" / default).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.exit(1, f"Error: {output} already exists; delete it first\n")
    rng = random.Random(args.seed)
    with tempfile.TemporaryDirectory() as tmp:
        people = [person(rng, n) for n in range(1, args.rows + 1)]
        half = len(people) // 2  # two files, like a real JSON export folder
        Path(tmp, "part1.json").write_text(json.dumps(people[:half]), encoding="utf-8")
        Path(tmp, "part2.json").write_text(json.dumps(people[half:]), encoding="utf-8")
        result = import_folder(tmp, output, password=args.password)
    print(f"Created {result['output']} with {result['rows']} rows"
          f"{' (SQLCipher encrypted)' if result['encrypted'] else ''}")


if __name__ == "__main__":
    main()
