# engineering-flow-platform-opencode-runtime

OpenCode runtime adapter for EFP-compatible runtime image.

## Runtime topology
- Portal-facing adapter: `0.0.0.0:8000`
- Internal OpenCode server: `127.0.0.1:4096`

## Contract
- **External tools subsystem is removed / not supported**.
- Portal provides skills input only (`EFP_SKILLS_DIR`, default `/app/skills`).
- Tool capability comes from OpenCode built-in tools + runtime permission/profile policy.
- Runtime does not read/sync/generate/index external tools repos or manifests.

## Local development
```bash
python -m pytest -q
python -m pytest -q runtime_contract_tests
bash scripts/ci_unit.sh
bash scripts/smoke.sh
```

## Runtime tool binaries
This image expects prebuilt custom tool binaries in `runtime-tools/` before
`docker build`. See `docs/CUSTOM_TOOLS_IMAGE.md`.

## Java and Maven image support
The runtime image includes Azul Zulu JDK 21. Zulu JDK 21 is the default
`JAVA_HOME`. Apache Maven 3.9.16 is installed at `/opt/maven`, with `mvn`,
`jdk`, and `mvn-jdk` available on `PATH`.

The build pipeline must generate `runtime-maven/settings.xml` before
`docker build`. Do not commit the real settings file; use
`runtime-maven/settings.xml.example` as the minimal local template.

## Docs
- Runtime contract: `docs/RUNTIME_CONTRACT.md`
- Observability: `docs/OBSERVABILITY.md`
- Testing guide: `docs/TESTING.md`
- Java/Maven image: `docs/JAVA_MAVEN_IMAGE.md`
