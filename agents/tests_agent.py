from agents.base_agent import BaseAgent


class TestsAgent(BaseAgent):
    name = "tests"
    prompt_file = "tests.txt"

    async def review(self, diff: str, context: dict) -> dict:
        return await super().review(diff, context)
