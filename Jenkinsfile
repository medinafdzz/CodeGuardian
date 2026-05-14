pipeline {
    agent any

    environment {
        SONARQUBE_HOST_URL   = 'http://sonarqube-server:9000'
        AGENT_REPO_URL       = 'https://bitbucket.org/medinafdzz/codeguardian-core.git'
        AGENT_REPO_REF       = 'feature'
        BITBUCKET_WORKSPACE  = 'medinafdzz'
        CACHE_MODE           = 'explicit'
        CACHE_TTL            = '3600s'
        CODEGUARDIAN_ENABLE_IMPROVEMENTS = 'true'
        CODEGUARDIAN_MAX_IMPROVEMENTS    = '3'
    }

    options {
        skipDefaultCheckout()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    stages {
        stage('Analyze PR') {
            when {
                changeRequest()
            }
            steps {
                checkout scm

                script {
                    def commonExclusions = '**/node_modules/**,**/dist/**,**/build/**,**/target/**,**/.git/**'
                    def analysisExclusions = "${commonExclusions},**/__pycache__/**,**/.venv/**,**/venv/**,**/.codeguardian-venv/**,**/.pytest_cache/**,**/coverage/**"

                    env.REPO_NAME = sh(
                        script: 'basename "$(git config --get remote.origin.url)" .git',
                        returnStdout: true
                    ).trim()

                    env.SONARQUBE_PROJECT_KEY = env.REPO_NAME.replace('/', '_')

                    def findFiles = { String pattern ->
                        sh(
                            script: "find . -path '*/.git' -prune -o -path '*/node_modules' -prune -o -path '*/target' -prune -o -path '*/build' -prune -o -path '*/.venv' -prune -o -path '*/venv' -prune -o -path '*/.codeguardian-venv' -prune -o -name '${pattern}' -print | sed 's#^./##'",
                            returnStdout: true
                        ).trim().split('\n').findAll { it }
                    }

                    def mavenPoms = fileExists('pom.xml') ? ['pom.xml'] : findFiles('pom.xml')
                    def gradleFiles = fileExists('build.gradle') || fileExists('build.gradle.kts') || fileExists('gradlew') ? ['.'] : findFiles('build.gradle') + findFiles('build.gradle.kts') + findFiles('gradlew')
                    def nodePackages = fileExists('package.json') ? ['package.json'] : findFiles('package.json')
                    def pythonFiles = findFiles('requirements.txt') + findFiles('pyproject.toml') + findFiles('setup.py')
                    def cfamilyCompileDbs = fileExists('compile_commands.json') ? ['compile_commands.json'] : findFiles('compile_commands.json')
                    def cfamilyBuildFiles = findFiles('CMakeLists.txt') + findFiles('Makefile')

                    def hasMaven = !mavenPoms.isEmpty()
                    def hasGradle = !gradleFiles.isEmpty()
                    def hasNode = !nodePackages.isEmpty()
                    def hasPython = !pythonFiles.isEmpty()
                    def hasCfamilyCompileDb = !cfamilyCompileDbs.isEmpty()
                    def hasCfamilyBuildFiles = !cfamilyBuildFiles.isEmpty()
                    def detectedStacks = []

                    def runQuiet = { String cmd, String logFile, String label ->
                        sh """
                        #!/bin/bash
                        set +x
                        ${cmd} > ${logFile} 2>&1 || {
                        code=\$?
                        echo "${label} failed. Last 200 lines:"
                        tail -n 200 ${logFile}
                        exit \$code
                        }
                        """
                    }

                    def runOptional = { String cmd, String logFile, String label ->
                        sh """
                        #!/bin/bash
                        set +x
                        ${cmd} > ${logFile} 2>&1 || {
                        code=\$?
                        echo "${label} failed. Last 200 lines:"
                        tail -n 200 ${logFile}
                        exit \$code
                        }
                        """
                    }

                    withSonarQubeEnv('SonarQube-Server') {
                        def runScanner = { String extraArgs ->
                            runQuiet(
                                """
                                sonar-scanner \
                                  -Dsonar.projectKey=${env.SONARQUBE_PROJECT_KEY} \
                                  -Dsonar.projectName=${env.REPO_NAME} \
                                  -Dsonar.host.url=${env.SONARQUBE_HOST_URL} \
                                  -Dsonar.scm.exclusions.disabled=false \
                                  -Dsonar.qualitygate.wait=false \
                                  -Dsonar.log.level=WARN \
                                  ${extraArgs}
                                """.stripIndent().trim(),
                                'sonar-scanner.log',
                                'SonarScanner'
                            )
                        }

                        if (hasMaven) {
                            detectedStacks.add('maven')
                            mavenPoms.eachWithIndex { pomFile, index ->
                                runQuiet(
                                    "mvn -B -q -ntp -f \"${pomFile}\" clean test",
                                    "maven-build-${index}.log",
                                    "Maven build and tests (${pomFile})"
                                )
                            }
                        }

                        if (hasGradle) {
                            detectedStacks.add('gradle')
                            gradleFiles.collect { it == '.' ? '.' : it.substring(0, it.lastIndexOf('/')) }.unique().eachWithIndex { gradleDir, index ->
                                if (fileExists("${gradleDir}/gradlew")) {
                                    runQuiet(
                                        "cd \"${gradleDir}\" && chmod +x ./gradlew && ./gradlew --no-daemon -q test",
                                        "gradle-build-${index}.log",
                                        "Gradle build and tests (${gradleDir})"
                                    )
                                } else {
                                    runQuiet(
                                        "gradle -p \"${gradleDir}\" --no-daemon -q test",
                                        "gradle-build-${index}.log",
                                        "Gradle build and tests (${gradleDir})"
                                    )
                                }
                            }
                        }

                        if (hasNode) {
                            detectedStacks.add('node')
                            nodePackages.eachWithIndex { packageFile, index ->
                                def nodeDir = packageFile.contains('/') ? packageFile.substring(0, packageFile.lastIndexOf('/')) : '.'
                                if (fileExists("${nodeDir}/package-lock.json")) {
                                    runOptional(
                                        "cd \"${nodeDir}\" && npm ci",
                                        "node-build-${index}.log",
                                        "NPM dependency installation (${nodeDir})"
                                    )
                                } else {
                                    runOptional(
                                        "cd \"${nodeDir}\" && npm install",
                                        "node-build-${index}.log",
                                        "NPM dependency installation (${nodeDir})"
                                    )
                                }

                                def hasNpmTest = sh(
                                    script: "cd \"${nodeDir}\" && node -e \"const p=require('./package.json'); process.exit(p.scripts && p.scripts.test ? 0 : 1)\"",
                                    returnStatus: true
                                ) == 0

                                if (hasNpmTest) {
                                    runOptional(
                                        "cd \"${nodeDir}\" && npm test",
                                        "node-test-${index}.log",
                                        "NPM tests (${nodeDir})"
                                    )
                                } else {
                                    echo "No npm test script found in ${nodeDir}. Skipping Node.js tests."
                                }
                            }
                        }

                        if (hasPython) {
                            detectedStacks.add('python')
                            def pythonCommand = 'python3'
                            def requirementFiles = findFiles('requirements.txt')
                            if (!requirementFiles.isEmpty()) {
                                runOptional(
                                    'python3 -m venv .codeguardian-venv',
                                    'python-build-venv.log',
                                    'Python virtual environment creation'
                                )
                                pythonCommand = './.codeguardian-venv/bin/python'
                                requirementFiles.eachWithIndex { requirementsFile, index ->
                                    runOptional(
                                        "${pythonCommand} -m pip install -q -r \"${requirementsFile}\"",
                                        "python-build-${index}.log",
                                        "Python dependency installation (${requirementsFile})"
                                    )
                                }
                            }

                            runOptional(
                                "${pythonCommand} -m compileall -q .",
                                'python-build-compile.log',
                                'Python syntax compilation'
                            )

                            def hasPythonTests = sh(
                                script: "find . -path '*/.venv' -prune -o -path '*/venv' -prune -o -path '*/.codeguardian-venv' -prune -o -path '*/__pycache__' -prune -o -type f \\( -name 'test_*.py' -o -name '*_test.py' \\) -print | grep -q .",
                                returnStatus: true
                            ) == 0

                            if (hasPythonTests) {
                                runOptional(
                                    "${pythonCommand} -m pip install -q pytest",
                                    'python-build-pytest.log',
                                    'Python pytest installation'
                                )
                                runOptional(
                                    "${pythonCommand} -m pytest -q",
                                    'python-test-0.log',
                                    'Python tests'
                                )
                            } else {
                                echo 'No Python tests found. Skipping Python tests.'
                            }
                        }

                        if (hasCfamilyCompileDb) {
                            detectedStacks.add('cfamily')
                        } else if (hasCfamilyBuildFiles) {
                            error('C/C++ project detected but compile_commands.json is missing.')
                        }

                        env.PROJECT_TYPE = detectedStacks ? detectedStacks.join(',') : 'generic'

                        def javaBinaries = sh(
                            script: "find . -type d \\( -path '*/target/classes' -o -path '*/build/classes' -o -path '*/build/classes/java/main' \\) | sed 's#^./##' | paste -sd, -",
                            returnStdout: true
                        ).trim()

                        def scannerArgs = "-Dsonar.sources=. -Dsonar.exclusions=${analysisExclusions}"
                        if (javaBinaries) {
                            scannerArgs = "${scannerArgs} -Dsonar.java.binaries=${javaBinaries}"
                        }
                        if (hasCfamilyCompileDb) {
                            scannerArgs = "${scannerArgs} -Dsonar.cfamily.compile-commands=${cfamilyCompileDbs[0]}"
                        }

                        runScanner(scannerArgs)
                    }

                    echo "Analysis completed for ${env.REPO_NAME} (${env.PROJECT_TYPE})"
                }
            }
        }

        stage('Run AI agent') {
            when {
                changeRequest()
            }
            steps {
                withCredentials([
                    string(credentialsId: 'sonarqube-token', variable: 'SONARQUBE_AUTH_TOKEN'),
                    string(credentialsId: 'LLM-token', variable: 'LLM_AUTH_TOKEN'),
                    string(credentialsId: 'bitbucket_email', variable: 'BITBUCKET_EMAIL'),
                    string(credentialsId: 'bitbucket-token', variable: 'BITBUCKET_API_TOKEN'),
                    string(credentialsId: 'atlassian-mcp-auth-header', variable: 'ATLASSIAN_MCP_AUTH_HEADER')
                ]) {
                    script {
                        def data = groovy.json.JsonOutput.prettyPrint(
                            groovy.json.JsonOutput.toJson([
                                pr_id      : env.CHANGE_ID ?: '',
                                project_key: env.SONARQUBE_PROJECT_KEY,
                                repo_slug  : env.REPO_NAME,
                                workspace  : env.BITBUCKET_WORKSPACE
                            ])
                        )

                        def agentRepoHostPath = env.AGENT_REPO_URL.replaceFirst(/^https?:\\/\\//, '')

                        writeFile file: 'data.json', text: data

                        withEnv([
                            "AGENT_REPO_HOST_PATH=${agentRepoHostPath}",
                            'ATLASSIAN_MCP_URL=https://mcp.atlassian.com/v1/mcp',
                            'CODEGUARDIAN_ENABLE_PERFORMANCE_REVIEW=true',
                            'CODEGUARDIAN_PERFORMANCE_MAX_SCOPES=30',
                            'CODEGUARDIAN_PERFORMANCE_MIN_COMPLEXITY_GAIN=true',
                            'CODEGUARDIAN_PERFORMANCE_CONTEXT_WINDOW=20',
                            "CODEGUARDIAN_RESULTS_PATH=${env.WORKSPACE}/codeguardian-results.json"
                        ]) {
                            sh '''
                            #!/bin/bash

                            set +x
                            git clone --quiet --depth 1 --single-branch -b "$AGENT_REPO_REF" \
                            "https://x-bitbucket-api-token-auth:${BITBUCKET_API_TOKEN}@${AGENT_REPO_HOST_PATH}" AIagent > /dev/null 2>&1

                            git fetch --quiet origin "${CHANGE_TARGET:-main}:refs/remotes/origin/${CHANGE_TARGET:-main}" || true

                            python3 -u AIagent/agent.py --file data.json
                            '''
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'codeguardian-results.json', allowEmptyArchive: true
            sh 'rm -rf AIagent data.json build.log test.log *-build-*.log *-test-*.log sonar-scanner.log || true'
        }
    }
}
