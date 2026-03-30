import uvicorn

from src.screenmonitor.app import app


if __name__ == "__main__":
    uvicorn.run("src.screenmonitor.app:app", host="127.0.0.1", port=8000)
