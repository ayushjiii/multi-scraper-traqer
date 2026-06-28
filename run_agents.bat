@echo off
echo [SYSTEM] Booting Traqer Microservices...

:: Open ChatGPT Agent in a new terminal
start cmd /k "title ChatGPT Agent && call .venv\Scripts\activate && python chatgpt_agent.py"

:: Open Perplexity Agent in a new terminal
start cmd /k "title Perplexity Agent && call .venv\Scripts\activate && python perplexity_agent.py"

:: Open Gemini Agent in a new terminal
start cmd /k "title Gemini Agent && call .venv\Scripts\activate && python gemini_agent.py"

:: Open Google AI Overviews Agent in a new terminal
start cmd /k "title AIO Agent && call .venv\Scripts\activate && python aio_agent.py"

echo [SYSTEM] All agents deployed! Check the new windows.