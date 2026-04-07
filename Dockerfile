FROM jenkins/jenkins:2.541.2-jdk21
USER root
RUN apt-get update && apt-get install -y lsb-release
RUN curl -fsSLo /usr/share/keyrings/docker-archive-keyring.asc \
  https://download.docker.com/linux/debian/gpg
RUN echo "deb [arch=$(dpkg --print-architecture) \
  signed-by=/usr/share/keyrings/docker-archive-keyring.asc] \
  https://download.docker.com/linux/debian \
  $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list

  RUN echo "Acquire::http::Pipeline-Depth 0;" > /etc/apt/apt.conf.d/99fixbadproxy && \
    echo "Acquire::http::No-Cache true;" >> /etc/apt/apt.conf.d/99fixbadproxy && \
    echo "Acquire::BrokenProxy true;" >> /etc/apt/apt.conf.d/99fixbadproxy
    
# Installation of Docker CLI, Node.js/NPM, and C++ Build Tools (NUEVO)
RUN apt-get clean && rm -rf /var/lib/apt/lists/* && \
    apt-get update --fix-missing && apt-get install -y --fix-missing \
    docker-ce-cli \
    git \
    nodejs \
    npm \
    build-essential \
    cmake \
    cppcheck

# Installation of Python and pip to use it in Jenkins pipelines
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip

# Installation of Python packages for the Jenkins pipelines, including MCP and the Google GenAI client library
RUN python3 -m pip install -q google-genai mcp prometheus-client pydantic --break-system-packages

# Add the Jenkins user to the Docker group to allow it to run Docker commands
RUN groupadd -g 999 docker && usermod -aG docker jenkins

# Installation of SonarScanner and MCP Servers for GitHub and SonarQube
RUN npm install -g \
    @sonar/scan \
    mcp-sonarqube \
    bitbucket-mcp

# Installation of Jenkins plugins for the pipeline
RUN jenkins-plugin-cli --plugins "blueocean docker-workflow json-path-api"

# Jenkins needs to run as the jenkins user
USER jenkins