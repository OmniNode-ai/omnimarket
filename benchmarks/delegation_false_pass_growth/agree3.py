# ruff: noqa: SIM115, E741, T201
"""OMN-20032: two-rater agreement (all labelled items) and three-way agreement on the stratified third-rater sample.
usage: python3 agree3.py <dir> <labels_opus.jsonl> <labels_sonnet.jsonl> [<third.json> <sample.json>]  -> agreement.json"""

import itertools
import json
import sys
from collections import Counter, defaultdict

D = sys.argv[1]


def load(p):
    return {json.loads(l)["correlation_id"]: json.loads(l) for l in open(p)}


O, N = load(sys.argv[2]), load(sys.argv[3])


def cohen(pairs):
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa = Counter(a for a, _ in pairs)
    pb = Counter(b for _, b in pairs)
    pe = sum(pa[c] * pb[c] for c in ("adequate", "inadequate")) / n / n
    return {
        "n": n,
        "agree": sum(a == b for a, b in pairs),
        "po": po,
        "kappa": (po - pe) / (1 - pe) if pe < 1 else None,
    }


two = [
    (O[c]["label"], N[c]["label"])
    for c in O
    if c in N and O[c]["label"] != "unlabelable" and N[c]["label"] != "unlabelable"
]
out = {
    "two_rater_all": cohen(two),
    "two_rater_items_total": len([c for c in O if c in N]),
}
acc = {c for c in O if "/accepted" in O[c]["stratum"]}
out["two_rater_accepted"] = cohen(
    [
        (O[c]["label"], N[c]["label"])
        for c in acc
        if c in N and O[c]["label"] != "unlabelable" and N[c]["label"] != "unlabelable"
    ]
)
byc = defaultdict(list)
for c in acc:
    if c in N and O[c]["label"] != "unlabelable" and N[c]["label"] != "unlabelable":
        byc[O[c]["stratum"].split("/")[0]].append((O[c]["label"], N[c]["label"]))
out["two_rater_accepted_by_class"] = {k: cohen(v) for k, v in sorted(byc.items())}
out["unlabelable"] = {
    "opus": sum(1 for r in O.values() if r["label"] == "unlabelable"),
    "sonnet": sum(1 for r in N.values() if r["label"] == "unlabelable"),
    "sonnet_missing": len([c for c in O if c not in N]),
}
if len(sys.argv) > 4:
    T = json.load(open(sys.argv[4]))
    sample = json.load(open(sys.argv[5]))
    idmap = {s["id"]: s for s in sample}
    trip, dropped = [], Counter()
    for bid, s in idmap.items():
        c = s["correlation_id"]
        t = T.get(bid, {}).get("label")
        lab = (O.get(c, {}).get("label"), N.get(c, {}).get("label"), t)
        if None in lab or "unlabelable" in lab or t not in ("adequate", "inadequate"):
            dropped["some rater undecided or missing"] += 1
            continue
        trip.append((c, s["task_class"], *lab))
    n = len(trip)
    unanimous = sum(len({a, b, t}) == 1 for _, _, a, b, t in trip)
    # Fleiss kappa, 3 raters, 2 categories
    P_i = []
    tot = Counter()
    for _, _, *r in trip:
        cnt = Counter(r)
        tot.update(cnt)
        P_i.append((sum(v * v for v in cnt.values()) - 3) / (3 * 2))
    Pbar = sum(P_i) / n
    pj = {k: v / (3 * n) for k, v in tot.items()}
    Pe = sum(v * v for v in pj.values())
    out["three_way"] = {
        "sample_items": len(sample),
        "decided_by_all_three": n,
        "dropped": dict(dropped),
        "unanimous": unanimous,
        "unanimous_share": unanimous / n if n else None,
        "fleiss_kappa": (Pbar - Pe) / (1 - Pe) if Pe < 1 else None,
        "pairwise": {
            f"{x}-{y}": cohen([(r[i], r[j]) for _, _, *r in trip])
            for (x, i), (y, j) in itertools.combinations(
                [("opus", 0), ("sonnet", 1), ("gpt-oss-120b", 2)], 2
            )
        },
        "by_class": {
            cl: {
                "n": sum(1 for x in trip if x[1] == cl),
                "unanimous": sum(
                    1 for x in trip if x[1] == cl and len({x[2], x[3], x[4]}) == 1
                ),
                "inadequate_votes": dict(
                    Counter(
                        ("opus" if x[2] == "inadequate" else None)
                        for x in trip
                        if x[1] == cl
                    )
                ),
            }
            for cl in sorted({x[1] for x in trip})
        },
        "false_pass_on_sample": {
            "opus": sum(x[2] == "inadequate" for x in trip),
            "sonnet": sum(x[3] == "inadequate" for x in trip),
            "gpt-oss-120b": sum(x[4] == "inadequate" for x in trip),
            "n": n,
        },
    }
    out["three_way_rows"] = [
        {
            "correlation_id": c,
            "task_class": cl,
            "opus": a,
            "sonnet": b,
            "gpt-oss-120b": t,
        }
        for c, cl, a, b, t in trip
    ]
json.dump(out, open(f"{D}/agreement.json", "w"), indent=1)
print(
    json.dumps({k: v for k, v in out.items() if k != "three_way_rows"}, indent=1)[:3500]
)
