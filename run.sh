#!/bin/bash

source .venv/bin/activate

# Use PORT environment variable if set (for Render), otherwise default to 8000
PORT=${PORT:-8000}

uvicorn main:app --host 0.0.0.0 --port $PORT 

