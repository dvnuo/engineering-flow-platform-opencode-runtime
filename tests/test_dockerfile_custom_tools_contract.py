from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _join(*parts: str) -> str:
    return "".join(parts)


def test_dockerfile_consumes_prebuilt_custom_tools_without_builder_stage():
    text = _text("Dockerfile")
    forbidden = [
        "FROM golang:",
        "AS " + _join("atlassian", "-", "tools"),
        _join("ATLASSIAN", "_", "TOOLS_REPO"),
        _join("ATLASSIAN", "_", "TOOLS_REF"),
        "git clone",
        "go build -o /out/jira",
    ]
    for token in forbidden:
        assert token not in text

    required = [
        "ARG CUSTOM_TOOLS_DIR=runtime-tools",
        "COPY ${CUSTOM_TOOLS_DIR}/jira /usr/local/bin/jira",
        "COPY ${CUSTOM_TOOLS_DIR}/confluence /usr/local/bin/confluence",
        "jira schema issue.map-csv --json",
        "jira schema issue.bulk-create --json",
    ]
    for token in required:
        assert token in text


def test_runtime_tools_context_contract_is_documented_and_ignored():
    docs = _text("docs/CUSTOM_TOOLS_IMAGE.md")
    for token in [
        "runtime-tools/jira",
        "runtime-tools/confluence",
        "engineering-flow-platform-tools",
        "scripts/build.sh --snapshot",
    ]:
        assert token in docs

    gitignore = _text(".gitignore")
    assert "runtime-tools/*" in gitignore
    assert "!runtime-tools/.gitkeep" in gitignore
    assert (ROOT / "runtime-tools" / ".gitkeep").exists()


def test_smoke_requires_runtime_tools_without_preparing_them():
    script = _text("scripts/smoke.sh")
    for token in [
        "require_runtime_tool jira",
        "require_runtime_tool confluence",
        "Missing runtime-tools/${tool}",
        "docs/CUSTOM_TOOLS_IMAGE.md",
        "jira schema issue.map-csv --json",
        "jira schema issue.bulk-create --json",
    ]:
        assert token in script
    assert "git clone" not in script
    assert "golang" not in script
