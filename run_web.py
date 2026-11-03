"""Start the web page, runnable from an IDE (PyCharm: right-click > Run 'run_web').

Same as `uvicorn app.main:app --reload`. Then open http://localhost:8000.
Code changes reload automatically; restart after editing .env.
"""
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    app_dir = Path(__file__).resolve().parent / "app"
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True, reload_dirs=[str(app_dir)])
