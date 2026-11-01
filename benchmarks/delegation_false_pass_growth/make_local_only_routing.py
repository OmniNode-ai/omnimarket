# ruff: noqa: SIM115, T201
import os

import yaml

d = yaml.safe_load(open("/app/config/delegation/routing_tiers.yaml"))
d["tiers"] = [t for t in d["tiers"] if t["name"] == "local"]
print([t["name"] for t in d["tiers"]], [k for k in d if k != "tiers"])
out = f"{os.environ['OMN20032_WORK_DIR']}/routing_tiers_local_only.yaml"
yaml.safe_dump(d, open(out, "w"), sort_keys=False)
