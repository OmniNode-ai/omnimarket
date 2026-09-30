# ruff: noqa: SIM115, T201
import yaml

src = "/app/config/delegation/routing_tiers.yaml"
d = yaml.safe_load(open(src))
d["tiers"] = [t for t in d["tiers"] if t["name"] == "local"]
print([t["name"] for t in d["tiers"]], [k for k in d if k != "tiers"])
yaml.safe_dump(
    d, open("/tmp/omn20032/routing_tiers_local_only.yaml", "w"), sort_keys=False
)
