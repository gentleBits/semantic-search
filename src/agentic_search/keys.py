"""The OpenAI client, made only when something is about to be sent."""

from __future__ import annotations

import os

from .errors import ResumesError


def openai_client(what: str):
    if not os.environ.get("OPENAI_API_KEY"):
        raise ResumesError("KEY_MISSING", f"{what} needs OPENAI_API_KEY in the environment")
    from openai import OpenAI

    return OpenAI()
