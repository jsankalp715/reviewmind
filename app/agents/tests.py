"""Tests review agent. Its system prompt lives in `prompts/tests.txt`."""

from app.agents.base import ReviewAgent


class TestsAgent(ReviewAgent):
    name = "tests"
    title = "Tests"
    emoji = "🧪"
    prompt_file = "tests.txt"
    __test__ = False  # stop pytest from trying to collect this class
