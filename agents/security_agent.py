from agents.base_agent import BaseAgent


class SecurityAgent(BaseAgent):
    name = "security"
    prompt_file = "security.txt"

    async def review(self, diff: str, context: dict) -> dict:
        return await super().review(diff, context)
