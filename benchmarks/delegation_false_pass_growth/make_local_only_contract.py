# ruff: noqa: SIM115, T201

import os

import yaml

src = "/app/.venv/lib/python3.12/site-packages/omnimarket/configs/task_class_contracts.v1.yaml"
d = yaml.safe_load(open(src))
n, WORK_DIR = 0, os.environ["OMN20032_WORK_DIR"]
for name, c in d["task_classes"].items():
    ep = c.get("escalation_policy")
    if ep is not None and "tier_order" in ep:
        print(name, ep["tier_order"], ep.get("max_escalations"))
        ep["tier_order"] = ["local"]
        n += 1
    else:
        print(name, "NO tier_order", ep)
os.makedirs(WORK_DIR, exist_ok=True)
yaml.safe_dump(
    d, open(f"{WORK_DIR}/task_class_contracts.v1.yaml", "w"), sort_keys=False
)
print("modified", n)
