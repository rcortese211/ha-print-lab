import json, os, sys
ROOT = os.environ.get("ANYCUBIC", "../orca/resources/profiles/Anycubic")
idx = {}
for kind in ("machine", "process", "filament"):
    for f in os.listdir(os.path.join(ROOT, kind)):
        if f.endswith(".json"):
            d = json.load(open(os.path.join(ROOT, kind, f)))
            idx[(kind, d["name"])] = d
def flat(kind, name):
    d = idx[(kind, name)]
    base = flat(kind, d["inherits"]) if d.get("inherits") else {}
    out = dict(base); out.update(d); out.pop("inherits", None)
    return out
if __name__ == "__main__":
    kind, name = sys.argv[1], sys.argv[2]
    print(json.dumps(flat(kind, name), indent=1, sort_keys=True))
