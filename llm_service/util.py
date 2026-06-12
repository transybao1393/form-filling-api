import re


def clean_json_output(raw: str) -> dict:
    """Strip markdown code fences (```json ... ``` or ``` ... ```) that some
    LLMs (e.g. Ollama models) wrap around JSON output, then parse to dict.

    Raises:
        json.JSONDecodeError: if the cleaned text is still not valid JSON.
    """
    text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw.strip()).strip()
    return text