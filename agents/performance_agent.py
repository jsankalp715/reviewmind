from agents.base_agent import BaseAgent


class PerformanceAgent(BaseAgent):
    name = "performance"
    prompt_file = "performance.txt"

    async def review(self, diff: str, context: dict) -> dict:
        return await super().review(diff, context)
