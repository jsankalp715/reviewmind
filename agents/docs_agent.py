from agents.base_agent import BaseAgent


class DocsAgent(BaseAgent):
    name = "docs"
    prompt_file = "docs.txt"

    async def review(self, diff: str, context: dict) -> dict:
        return await super().review(diff, context)
