"""A tiny request handler."""


def handle(path: str) -> tuple[int, str]:
    if path == "/health":
        return 200, "ok"
    return 404, "not found"
