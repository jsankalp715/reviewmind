"""Performance review agent. Its system prompt lives in `prompts/performance.txt`."""

from app.agents.base import ReviewAgent


class PerformanceAgent(ReviewAgent):
    name = "performance"
    title = "Performance"
    emoji = "⚡"
    prompt_file = "performance.txt"
