import uvicorn
from fastapi import FastAPI

from app.jira.webhook_handler import router as jira_router

app = FastAPI(title="Jira AI Agent", version="1.0.0")

app.include_router(jira_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
