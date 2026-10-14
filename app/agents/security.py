"""Security review agent. Its system prompt lives in `prompts/security.txt`."""

from app.agents.base import ReviewAgent


class SecurityAgent(ReviewAgent):
    name = "security"
    title = "Security"
    emoji = "🔒"
    prompt_file = "security.txt"
