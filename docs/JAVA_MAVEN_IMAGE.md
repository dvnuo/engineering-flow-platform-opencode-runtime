# Java and Maven Runtime Image

The runtime Docker image installs Java and Maven directly in the image. Agents
use these tools through OpenCode Bash; no Portal change, tools repo loader, Go
CLI, MCP tool, custom tool, RuntimeProfile schema change, or capabilities API
change is required.

Installed Java runtimes:

- Azul Zulu JDK 21 at `/opt/jdks/zulu21`

Zulu JDK 21 is the default:

```bash
java -version
javac -version
echo "$JAVA_HOME"
```

Maven 3.9.16 is installed at `/opt/maven`. Maven settings are copied from the
build context into `/root/.m2/settings.xml`, and Docker build generates
`/root/.m2/toolchains.xml` with only the JDK 21 toolchain. Both files are
installed with mode `0600`.

CI runs a Docker smoke job that builds the image, starts the runtime, verifies
Java and Maven commands, and audits that both Maven settings and toolchains
files keep mode `0600`.

The expected build context path is `runtime-maven/settings.xml`. The Dockerfile
also supports:

```bash
docker build --build-arg MAVEN_SETTINGS_DIR=runtime-maven -t opencode-runtime .
```

Do not commit a real `runtime-maven/settings.xml`. It may contain credentials or
internal repository URLs. Commit only `runtime-maven/settings.xml.example` and
let the pipeline create the real file before Docker build.

Wrapper commands:

```bash
jdk list
jdk current
jdk 21 java -version
mvn-jdk -B -ntp test
mvn-jdk 21 -B -ntp verify
```

Direct JDK tools from the default JDK 21 are also on `PATH`:

```bash
jdeps --version
jlink --version
jcmd -h
```

Prefer `mvn -B -ntp` for Maven commands in automated agent work.
