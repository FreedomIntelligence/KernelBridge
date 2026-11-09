"""Source formatting only; no model clients, device imports or global configuration."""
import re


def extract_first_code(text: str) -> str:
    match = re.search(r"```(?:python|cpp)?\s*\n?(.*?)```", text.strip(), re.DOTALL)
    return match.group(1).strip() if match else text
