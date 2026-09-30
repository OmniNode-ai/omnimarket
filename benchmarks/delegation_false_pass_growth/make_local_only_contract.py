# ruff: noqa: SIM115, E402, T201

import yaml

src = "/app/.venv/lib/python3.12/site-packages/omnimarket/configs/task_class_contracts.v1.yaml"
d = yaml.safe_load(open(src))
n = 0
for name, c in d["task_classes"].items():
    ep = c.get("escalation_policy")
    if ep is not None and "tier_order" in ep:
        print(name, ep["tier_order"], ep.get("max_escalations"))
        ep["tier_order"] = ["local"]
        n += 1
    else:
        print(name, "NO tier_order", ep)
import os

os.makedirs("/tmp/omn20032", exist_ok=True)
yaml.safe_dump(
    d, open("/tmp/omn20032/task_class_contracts.v1.yaml", "w"), sort_keys=False
)
print("modified", n)
