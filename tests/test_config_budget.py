from agentpitbench import config


def test_partial_opai_task_budget_keeps_defaults(tmp_path):
    p = tmp_path / "bench.toml"
    p.write_text("[opai_task_budget]\ntranslate = 80\n")
    s = config.load(p)
    assert s.opai_task_budget["translate"] == 80
    assert s.opai_task_budget["motd_script"] == config.Settings().opai_task_budget["motd_script"] > 0
