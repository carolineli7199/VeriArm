"""Validator checks from the spec's verification checklist. No robot, no network.

  python -m art_curator.test_curator
"""
import copy

from art_curator.curator import CurationError, curate, load_catalog, validate

CATALOG = load_catalog()
GOOD = {
    "exhibition_title": "Finding Light in Darkness",
    "preview": "From the depths of fear, through the storm, toward a sky full of light.",
    "curatorial_statement": "An exploration of how humanity finds hope amid uncertainty.",
    "closing": "Look up tonight; the stars are still there.",
    "selected_artworks": [
        {"id": 4, "position": 1, "reason": "The Scream represents our confrontation with fear."},
        {"id": 2, "position": 2, "reason": "The Great Wave captures the struggle against overwhelming forces."},
        {"id": 1, "position": 3, "reason": "The Starry Night represents the possibility of wonder and hope."},
    ],
}


def broken(change) -> dict:
    a = copy.deepcopy(GOOD)
    change(a)
    return a


BAD = {
    "not an object": [],
    "missing title": broken(lambda a: a.pop("exhibition_title")),
    "extra field": broken(lambda a: a.update(robot_command="go")),
    "blank statement": broken(lambda a: a.update(curatorial_statement="   ")),
    "missing preview": broken(lambda a: a.pop("preview")),
    "blank preview": broken(lambda a: a.update(preview="")),
    "missing closing": broken(lambda a: a.pop("closing")),
    "blank closing": broken(lambda a: a.update(closing=" ")),
    "two artworks": broken(lambda a: a["selected_artworks"].pop()),
    "four artworks": broken(lambda a: a["selected_artworks"].append({"id": 5, "position": 3, "reason": "x"})),
    "duplicate id": broken(lambda a: a["selected_artworks"][1].update(id=4)),
    "id out of range": broken(lambda a: a["selected_artworks"][0].update(id=9)),
    "id as string": broken(lambda a: a["selected_artworks"][0].update(id="4")),
    "id as bool": broken(lambda a: a["selected_artworks"][0].update(id=True)),
    "duplicate position": broken(lambda a: a["selected_artworks"][1].update(position=1)),
    "position 0": broken(lambda a: a["selected_artworks"][0].update(position=0)),
    "blank reason": broken(lambda a: a["selected_artworks"][2].update(reason="")),
    "extra artwork field": broken(lambda a: a["selected_artworks"][0].update(x=100)),
}


def main():
    for name, answer in BAD.items():
        try:
            validate(answer, CATALOG)
        except CurationError as e:
            print(f"  ok  rejected {name:22s} ({e})")
        else:
            raise AssertionError(f"accepted a bad answer: {name}")

    shuffled = broken(lambda a: a["selected_artworks"].reverse())
    ex = validate(shuffled, CATALOG)
    assert [p["position"] for p in ex["selected_artworks"]] == [1, 2, 3]
    assert [p["id"] for p in ex["selected_artworks"]] == [4, 2, 1]
    print("  ok  out-of-order answer comes back as positions 1, 2, 3")

    for theme in ("finding hope in uncertain times", "joy and freedom", "x"):
        ex = curate(theme, offline=True)
        assert len({p["id"] for p in ex["selected_artworks"]}) == 3
    print("  ok  offline curator gives 3 distinct valid picks")
    print("all validator checks passed")


if __name__ == "__main__":
    main()
