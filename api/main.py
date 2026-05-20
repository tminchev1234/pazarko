"""
Pazarko API — Bulgarian supermarket price comparison
FastAPI backend with AI chat, product matching, inflation tracking
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from api.routes import prices, search, chat, inflation, users, kaufland, alex

app = FastAPI(
    title="Pazarko API",
    description="Bulgarian supermarket price comparison with AI",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(prices.router,    prefix="/api")
app.include_router(search.router,    prefix="/api")
app.include_router(chat.router,      prefix="/api")
app.include_router(inflation.router, prefix="/api")
app.include_router(users.router,     prefix="/api")
app.include_router(kaufland.router,  prefix="/api")
app.include_router(alex.router,      prefix="/api")

@app.get("/alex")
async def alex_redirect():
    return RedirectResponse(url="/alex/")

@app.get("/alex/")
async def serve_alex_home():
    return FileResponse("alex/frontend/index.html")

@app.get("/alex/{full_path:path}")
async def serve_alex_spa(full_path: str):
    """SPA catch-all — serve static assets if they exist, otherwise index.html."""
    static = Path("alex/frontend") / full_path
    if static.exists() and static.is_file():
        return FileResponse(static)
    return FileResponse("alex/frontend/index.html")

# Serve frontends
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "pazarko"}


if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
