"""Fetch -> filter -> score, runnable from an IDE (PyCharm: right-click > Run 'run_pipeline').

Same as `python -m app.pipeline`. Options can go in the run configuration's "Parameters":
    --query "data engineer" --query "ml engineer"   keywords instead of config.yaml's
    --posted past_week                               past_day | past_week | past_month | any
    --no-fetch                                       only score jobs already in the database
"""
from app.pipeline import main

if __name__ == "__main__":
    main()
