FROM jenkins/jenkins:2.541.3-jdk21

USER root

# Base utilities + Docker repo
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gnupg \
    lsb-release \
    unzip \
    zip \
    jq \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /usr/share/keyrings/docker-archive-keyring.asc

RUN echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.asc] \
    https://download.docker.com/linux/debian \
    $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list

# Avoid flaky proxy/cache issues during apt
RUN echo "Acquire::http::Pipeline-Depth 0;" > /etc/apt/apt.conf.d/99fixbadproxy && \
    echo "Acquire::http::No-Cache true;" >> /etc/apt/apt.conf.d/99fixbadproxy && \
    echo "Acquire::BrokenProxy true;" >> /etc/apt/apt.conf.d/99fixbadproxy

# Main toolchain: Docker CLI + Java build tools + Python + C/C++ + Node
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-ce-cli \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    python-is-python3 \
    maven \
    gradle \
    nodejs \
    npm \
    build-essential \
    gcc \
    g++ \
    make \
    cmake \
    ninja-build \
    cppcheck \
    clang \
    clang-tidy \
    gdb \
    valgrind \
    shellcheck \
    && rm -rf /var/lib/apt/lists/*

# Python packages used by your pipelines / agent
RUN python3 -m pip install --break-system-packages --no-cache-dir \
    google-genai \
    mcp \
    prometheus-client \
    pydantic

# Global npm tools
# Keep your current package names if they already work in your environment
RUN npm install -g \
    @sonar/scan \
    mcp-sonarqube \
    bitbucket-mcp

# Jenkins plugins
RUN jenkins-plugin-cli --plugins \
    "blueocean docker-workflow json-path-api"

# Docker group access for Jenkins user
RUN groupadd -f docker && usermod -aG docker jenkins

USER jenkins