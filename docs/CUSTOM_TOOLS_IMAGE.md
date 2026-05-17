# Custom Tools Runtime Image

The opencode-runtime Dockerfile consumes prebuilt custom tool binaries from the
build context. Jenkins or another external pipeline must build
`engineering-flow-platform-tools` first and place the correct platform binaries
in `runtime-tools/` before `docker build`.

Required files before `docker build`:

- `runtime-tools/jira`
- `runtime-tools/confluence`

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

# Build runtime image
docker build -t engineering-flow-platform-opencode-runtime:local .
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
