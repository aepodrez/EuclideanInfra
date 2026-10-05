"""Production infrastructure pushes must declare their blast radius."""

from pathlib import Path


def test_full_infra_deploy_is_explicit_opt_in():
    workflow = (Path(__file__).parents[1] / ".github/workflows/deploy-infra.yml").read_text()
    assert "Require explicit deployment scope" in workflow
    assert "Conflicting deployment scopes" in workflow
    assert "Infra deployment requires an explicit scoped or full release marker" in workflow
    assert "if: ${{ contains(github.event.head_commit.message, '[full-infra-deploy]') }}" in workflow
    assert "if: ${{ contains(github.event.head_commit.message, '[scoped-monthly-readiness]') }}" in workflow
