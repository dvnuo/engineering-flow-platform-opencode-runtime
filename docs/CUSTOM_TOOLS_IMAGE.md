# Custom Tools Runtime Image

The opencode-runtime Dockerfile consumes prebuilt custom tool binaries from the
build context. Jenkins or another external pipeline must build
`engineering-flow-platform-tools` first and place the correct platform binaries
in `runtime-tools/` before `docker build`.

Required files before `docker build`:

- `runtime-tools/jira`
- `runtime-tools/confluence`
- `runtime-maven/settings.xml`

The pipeline must generate `runtime-maven/settings.xml` in the runtime build
context before Docker build. Do not commit the real settings file. It is ignored
by git; commit only `runtime-maven/settings.xml.example`.

Example for linux/amd64:

```bash
# Build tools repo externally
cd /path/to/engineering-flow-platform-tools
bash scripts/build.sh --snapshot

# Prepare runtime build context
cd /path/to/engineering-flow-platform-opencode-runtime
mkdir -p runtime-tools
cp /path/to/engineering-flow-platform-tools/dist/linux-amd64/jira runtime-tools/jira
cp /path/to/engineering-flow-platform-tools/dist/linux-amd64/confluence runtime-tools/confluence
mkdir -p runtime-maven
cp /secure/pipeline/generated/settings.xml runtime-maven/settings.xml

# Build runtime image
docker build --build-arg MAVEN_SETTINGS_DIR=runtime-maven -t engineering-flow-platform-opencode-runtime:local .
```

Example for linux/arm64:

```bash
mkdir -p runtime-tools
cp /path/to/engineering-flow-platform-tools/dist/linux-arm64/jira runtime-tools/jira
cp /path/to/engineering-flow-platform-tools/dist/linux-arm64/confluence runtime-tools/confluence
```

This keeps the final runtime image independent of the Go toolchain, prevents the
Docker build from cloning the tools repo, and lets CI or Jenkins control the
exact tools revision used for the image.
