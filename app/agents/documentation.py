"""Documentation review agent. Its system prompt lives in `prompts/documentation.txt`."""

from app.agents.base import ReviewAgent


class DocumentationAgent(ReviewAgent):
    name = "documentation"
    title = "Documentation"
    emoji = "📝"
    prompt_file = "documentation.txt"
