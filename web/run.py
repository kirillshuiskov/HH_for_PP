#!/usr/bin/env python3
"""
Скрипт для запуска веб-приложения HH.ru Analytics.

Использование:
    python web/run.py
    
Или через uvicorn напрямую:
    uvicorn web.app.main:app --host 0.0.0.0 --port 8000 --reload
"""

import sys
from pathlib import Path

# Добавляем корень проекта в path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import uvicorn

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Запуск веб-приложения HH.ru Analytics")
    print("=" * 60)
    print()
    print("📍 Адрес: http://localhost:8000")
    print("📚 API Docs: http://localhost:8000/docs")
    print("🏠 Frontend: http://localhost:8000/static/index.html")
    print()
    print("Нажмите Ctrl+C для остановки")
    print("=" * 60)
    
    uvicorn.run(
        "web.app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="info"
    )
