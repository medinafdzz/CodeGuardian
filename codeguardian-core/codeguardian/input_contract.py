import json

from codeguardian.logging_utils import logger


def load_webhook_data(filepath: str) -> tuple[str, str, str, str]:
    with open(filepath, "r") as file:
        data = json.load(file)

    project_key = data.get("project_key")
    pr_id = data.get("pr_id")
    repo_slug = data.get("repo_slug")
    workspace = data.get("workspace", "medinafdzz")

    if not project_key:
        logger.error("Project key not found in the JSON file.")
        return "", "", "", ""

    if not pr_id:
        logger.error("Pull request ID not found in the JSON file.")
        return "", "", "", ""

    if not repo_slug:
        logger.error("Repository slug not found in the JSON file.")
        return "", "", "", ""

    return project_key, str(pr_id), str(repo_slug), str(workspace)
