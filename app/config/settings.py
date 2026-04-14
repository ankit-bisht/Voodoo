from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Jira
    jira_url: str
    jira_user: str
    jira_token: str
    jira_webhook_secret: str = ""

    # GitHub
    github_token: str
    github_repo: str  # e.g. "org/repo-name"
    repo_local_path: str  # absolute path to local clone

    # GitHub Copilot (OAuth token from get_copilot_token.py)
    github_token: str = ""
    copilot_oauth_token: str = ""
    copilot_model: str = "claude-sonnet-4.6"
    copilot_base_url: str = "https://api.githubcopilot.com"

    # App behaviour
    base_branch: str = "main"
    max_context_files: int = 10
    max_file_chars: int = 8000  # chars per file sent to AI
    max_readme_chars: int = 4000

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
