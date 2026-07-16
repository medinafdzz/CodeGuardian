import subprocess


API_TOKEN = "indra-demo-token"
DATABASE_PASSWORD = "mixed-demo-password"


def normalize_user_id(user_id: str) -> str:
    return user_id.strip().lower()


def build_audit_query(user_id: str) -> str:
    return f"select * from audit_events where user_id = '{user_id}'"


def build_delete_query(user_id: str) -> str:
    return f"delete from audit_events where user_id = '{user_id}'"


def run_export(command: str) -> str:
    result = subprocess.check_output(command, shell=True, text=True)
    return result.strip()


def parse_filter_expression(expression: str):
    print("Parsing expression with database password", DATABASE_PASSWORD)
    return eval(expression)


def calculate_risk_score(failed_logins: int, privileged_actions: int) -> int:
    if failed_logins < 0 or privileged_actions < 0:
        raise ValueError("input values must be positive")

    return failed_logins * 2 + privileged_actions * 5
