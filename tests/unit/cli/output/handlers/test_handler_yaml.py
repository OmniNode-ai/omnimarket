import yaml

from omnimarket.cli.output.handlers.handler_yaml import HandlerCliOutputYaml
from tests.unit.cli._fixtures import make_sample_report


def test_yaml_parses_back() -> None:
    out = HandlerCliOutputYaml().render(make_sample_report())
    parsed = yaml.safe_load(out)
    assert parsed["skill_name"] == "ticket_pipeline"
    assert isinstance(parsed["steps"], list)
    assert isinstance(parsed["result_summary"], dict)


def test_yaml_no_python_object_tags() -> None:
    out = HandlerCliOutputYaml().render(make_sample_report())
    assert "!!python/" not in out
    assert "!!set" not in out
