# CodeGuardian Mixed Sample

This repository is a small multi-language project used to test CodeGuardian.

It contains two independent parts:

- `java-service`: a Maven Java service.
- `python-tools`: a Python utility module.

The objective is not to be a real business application. The objective is to check that the generic Jenkinsfile can detect more than one technology in the same repository, run the available validations, send the complete repository to SonarQube and then execute CodeGuardian.

## Expected Pipeline Behaviour

When this repository is analysed by Jenkins:

1. Maven must compile and run the Java tests.
2. Python files must be compiled with `compileall`.
3. Python tests must be executed with `pytest`.
4. SonarQube must analyse the complete repository.
5. CodeGuardian must read the SonarQube issues and publish comments in the pull request.

## Demo Intention

The code includes simple issues on purpose, for example hardcoded values, unsafe string handling and weak validation. They are useful to show that CodeGuardian can work with different languages in the same repository.

## Local Commands

From the repository root:

```bash
cd java-service
mvn test
```

```bash
python -m pytest -q
```
