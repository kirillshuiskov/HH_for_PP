"""
Минимальная версия веб-приложения HH.ru Analytics.
"""

from fastapi import FastAPI, Query, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path
import sys
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict
import pandas as pd

# Пути
WEB_DIR = Path(__file__).parent  # web/app
WEB_ROOT = WEB_DIR.parent  # web
PROJECT_ROOT = WEB_ROOT.parent  # hh_analytics
STATIC_DIR = WEB_ROOT / "static"  # web/static

sys.path.insert(0, str(PROJECT_ROOT))

# Логгер
from src.utils import get_logger
logger = get_logger(__name__)

# Конфиг (читает .env при импорте через load_dotenv)
from src.config import settings as _app_settings

# Путь к .env для записи
_ENV_PATH = PROJECT_ROOT / ".env"

# Приложение
app = FastAPI(title="HH.ru Analytics", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Статика
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

# Настройки пользователя — инициализируем из .env при старте
user_settings = {
    "hh_user_email": _app_settings.hh_user_email or None,
}

# =============================================================================
# Endpoints
# =============================================================================

@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/favicon.ico")
def favicon():
    """Возвращает favicon.ico или заглушку."""
    favicon_path = STATIC_DIR / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path)
    # Возвращаем пустую заглушку если favicon нет
    from fastapi.responses import Response
    return Response(content="", status_code=204)

@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools():
    """Заглушка для Chrome DevTools."""
    from fastapi.responses import JSONResponse
    return JSONResponse(content={})

@app.get("/analytics")
def analytics_page():
    """Отдельная страница аналитики."""
    return FileResponse(STATIC_DIR / "analytics.html")

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/vacancies")
def get_vacancies(
    page: int = 1,
    per_page: int = 20,
    search: str = "",
    area: str = "",
    experience: str = "",
    skill: str = "",
    salary_only: bool = False,
    sort_by: str = "salary",  # salary | date | none
    sort_order: str = "desc"  # asc | desc
):
    from src.storage import VacancyStorage
    import pandas as pd

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()
        if df.empty:
            return {"total": 0, "page": page, "items": []}

        # Применяем фильтры
        if search:
            df = df[df["vacancy_name"].str.contains(search, case=False, na=False)]

        if area:
            df = df[df["area"].str.contains(area, case=False, na=False)]

        if experience:
            df = df[df["experience"] == experience]

        if skill:
            # Поиск по навыкам (hard_skills, tools, soft_skills)
            skill_lower = skill.lower()
            mask = (
                df["hard_skills"].str.contains(skill_lower, case=False, na=False) |
                df["tools"].str.contains(skill_lower, case=False, na=False) |
                df["soft_skills"].str.contains(skill_lower, case=False, na=False)
            )
            df = df[mask]

        if salary_only:
            df = df[df["salary_from"].notna()]

        # Сортировка
        if sort_by == "salary" and "salary_from" in df.columns:
            # Мульти-сортировка: сначала по зарплате, потом по дате
            df = df.sort_values(
                by=["salary_from", "published_at"],
                ascending=[sort_order == "asc", False],  # ЗП по порядку, дата всегда по убыванию
                na_position="last"
            )
        elif sort_by == "date" and "published_at" in df.columns:
            # Сортировка только по дате
            df = df.sort_values(
                by="published_at",
                ascending=sort_order == "asc",
                na_position="last"
            )
        # else: sort_by == "none" — без сортировки, порядок из БД

        total = len(df)
        start = (page - 1) * per_page
        df_page = df.iloc[start:start + per_page]

        items = []
        for _, row in df_page.iterrows():
            item = {}
            for col in df.columns:
                val = row.get(col)
                if pd.isna(val):
                    val = None
                elif hasattr(val, 'isoformat'):
                    val = str(val)
                else:
                    val = val
                item[col] = val
            items.append(item)

        return {"total": total, "page": page, "items": items}
    finally:
        storage.close()

@app.get("/api/dashboard")
def get_dashboard():
    """Дашборд с расширенной статистикой."""
    from src.storage import VacancyStorage
    import pandas as pd
    import math

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()
        if df.empty:
            return {
                "total_vacancies": 0,
                "total_skills": 0,
                "avg_salary": None,
                "recent_vacancies": [],
                "top_skills": [],
                "vacancies_by_currency": {},
                "vacancies_by_area": {},
                "salary_trend": []
            }

        def get_top_skills(column, limit=10):
            counts = {}
            for s in column.dropna():
                if isinstance(s, str):
                    for skill in s.split(","):
                        skill = skill.strip()
                        if skill:
                            counts[skill] = counts.get(skill, 0) + 1
            return [{"name": k, "value": v} for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]]

        def safe_value(val):
            """Преобразует NaN/None в None для JSON-сериализации."""
            if val is None:
                return None
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                return None
            return val

        top_skills = get_top_skills(df["hard_skills"], 10) if "hard_skills" in df.columns else []

        # Недавние вакансии с валютами
        recent = df.sort_values(by="published_at", ascending=False).head(5) if "published_at" in df.columns else df.head(5)
        recent_vacancies = []
        for _, row in recent.iterrows():
            recent_vacancies.append({
                "name": str(row.get("vacancy_name", ""))[:50] if pd.notna(row.get("vacancy_name")) else "",
                "company": str(row.get("employer_name", ""))[:30] if pd.notna(row.get("employer_name")) else "",
                "area": str(row.get("area", "")) if pd.notna(row.get("area")) else "",
                "salary": safe_value(row.get("salary_from")),
                "currency": row.get("salary_currency", "RUB") if pd.notna(row.get("salary_currency")) else "RUB"
            })

        # Статистика по зарплате
        salary_df = df[df["salary_from"].notna()] if "salary_from" in df.columns else pd.DataFrame()
        avg_salary = None
        if not salary_df.empty:
            mean_val = salary_df["salary_from"].mean()
            avg_salary = safe_value(mean_val)

        # Распределение по валютам
        vacancies_by_currency = {}
        if "salary_currency" in df.columns:
            currency_counts = df["salary_currency"].value_counts().to_dict()
            for currency, count in currency_counts.items():
                curr_df = df[(df["salary_currency"] == currency) & (df["salary_from"].notna())]
                if not curr_df.empty:
                    vacancies_by_currency[currency] = {
                        "count": count,
                        "avg_salary": float(curr_df["salary_from"].mean())
                    }

        # Топ регионов
        vacancies_by_area = {}
        if "area" in df.columns:
            vacancies_by_area = df["area"].value_counts().head(5).to_dict()

        # Тренд зарплат по датам (по неделям)
        salary_trend = []
        if "published_at" in df.columns and "salary_from" in df.columns:
            df_with_dates = df[df["published_at"].notna() & df["salary_from"].notna()].copy()
            if not df_with_dates.empty:
                df_with_dates["published_at"] = pd.to_datetime(df_with_dates["published_at"])
                # Исправляем warning - убираем timezone перед конвертацией
                df_with_dates["published_at"] = df_with_dates["published_at"].dt.tz_localize(None)
                df_with_dates["week"] = df_with_dates["published_at"].dt.to_period("W").apply(lambda r: r.start_time)
                weekly = df_with_dates.groupby("week")["salary_from"].mean().reset_index()
                for _, row in weekly.tail(8).iterrows():
                    salary_trend.append({
                        "date": str(row["week"].date()),
                        "avg_salary": float(row["salary_from"])
                    })

        # Sparklines данные для KPI карточек
        # Вакансии по дням (последние 14 дней)
        vacancies_sparkline = []
        if "published_at" in df.columns and "created_at" in df.columns:
            df_with_dates = df[df["published_at"].notna()].copy()
            if not df_with_dates.empty:
                df_with_dates["published_at"] = pd.to_datetime(df_with_dates["published_at"])
                df_with_dates["published_at"] = df_with_dates["published_at"].dt.tz_localize(None)
                daily_counts = df_with_dates.groupby(df_with_dates["published_at"].dt.date).size()
                last_14_days = pd.date_range(end=pd.Timestamp.now(), periods=14, freq='D')
                for day in last_14_days:
                    date_only = day.date()
                    count = int(daily_counts.get(date_only, 0))
                    vacancies_sparkline.append(count)

        # Навыки по дням (последние 14 дней)
        skills_sparkline = []
        if "published_at" in df.columns and "skill_count" in df.columns:
            df_with_dates = df[df["published_at"].notna() & df["skill_count"].notna()].copy()
            if not df_with_dates.empty:
                df_with_dates["published_at"] = pd.to_datetime(df_with_dates["published_at"])
                df_with_dates["published_at"] = df_with_dates["published_at"].dt.tz_localize(None)
                daily_skills = df_with_dates.groupby(df_with_dates["published_at"].dt.date)["skill_count"].sum()
                last_14_days = pd.date_range(end=pd.Timestamp.now(), periods=14, freq='D')
                for day in last_14_days:
                    date_only = day.date()
                    count = int(daily_skills.get(date_only, 0))
                    skills_sparkline.append(count)

        # Зарплаты по дням (последние 14 дней)
        salary_sparkline = []
        if "published_at" in df.columns and "salary_from" in df.columns:
            df_with_dates = df[df["published_at"].notna() & df["salary_from"].notna()].copy()
            if not df_with_dates.empty:
                df_with_dates["published_at"] = pd.to_datetime(df_with_dates["published_at"])
                df_with_dates["published_at"] = df_with_dates["published_at"].dt.tz_localize(None)
                daily_salaries = df_with_dates.groupby(df_with_dates["published_at"].dt.date)["salary_from"].mean()
                last_14_days = pd.date_range(end=pd.Timestamp.now(), periods=14, freq='D')
                for day in last_14_days:
                    date_only = day.date()
                    avg = daily_salaries.get(date_only, None)
                    salary_sparkline.append(float(avg) if avg is not None else 0)

        return {
            "total_vacancies": len(df),
            "total_skills": int(df["skill_count"].sum()) if "skill_count" in df.columns and pd.notna(df["skill_count"].sum()) else 0,
            "avg_salary": avg_salary,
            "recent_vacancies": recent_vacancies,
            "top_skills": top_skills,
            "vacancies_by_currency": vacancies_by_currency,
            "vacancies_by_area": vacancies_by_area,
            "salary_trend": salary_trend,
            "sparklines": {
                "vacancies": vacancies_sparkline,
                "skills": skills_sparkline,
                "salaries": salary_sparkline
            },
            "last_updated": storage.get_last_parser_run(only_successful=True)["completed_at"] if storage.get_last_parser_run(only_successful=True) else None
        }
    finally:
        storage.close()

@app.get("/api/analytics/advanced")
def get_analytics():
    """Расширенная аналитика + распределение."""
    from src.storage import VacancyStorage
    from src.advanced_analyzer import AdvancedAnalytics
    import pandas as pd

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()
        if df.empty:
            return {
                "technologies": {},
                "hard_skills": {},
                "soft_skills": {},
                "salary_stats": {},
                "total_vacancies": 0
            }

        analytics = AdvancedAnalytics(df)

        # Получаем упрощенные данные по навыкам (только названия и количество)
        tech_summary = analytics.compute_technology_summary()
        hard_summary = analytics.compute_hard_skills_summary()
        soft_summary = analytics.compute_soft_skills_summary()

        # Преобразуем в простой формат {навык: количество}
        def simplify_skills(data):
            if not data:
                return {}
            result = {}
            for key, value in data.items():
                if isinstance(value, dict):
                    # Если это группа с count
                    result[key] = value.get("count", 0)
                else:
                    # Если это просто число
                    result[key] = value
            return result

        # Статистика по зарплате
        salary_stats = {}
        if "salary_from" in df.columns:
            salary_df = df[df["salary_from"].notna()]
            if not salary_df.empty:
                salary_stats = {
                    "min": float(salary_df["salary_from"].min()),
                    "max": float(salary_df["salary_from"].max()),
                    "median": float(salary_df["salary_from"].median()),
                    "avg": float(salary_df["salary_from"].mean())
                }

        return {
            "technologies": simplify_skills(tech_summary),
            "hard_skills": simplify_skills(hard_summary),
            "soft_skills": simplify_skills(soft_summary),
            "salary_stats": salary_stats,
            "total_vacancies": len(df)
        }
    finally:
        storage.close()

@app.get("/api/analytics/distribution")
def get_distribution():
    """Распределение вакансий по категориям для графиков с конвертацией валют."""
    from src.storage import VacancyStorage
    import pandas as pd

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()
        if df.empty:
            return {
                "experience": {},
                "employment": {},
                "salary_distribution": {},
                "top_areas": {},
                "salary_stats": {},
                "salary_stats_kzt": {},
                "salary_stats_usd": {},
                "currencies": {}
            }

        # Распределение по опыту
        experience_counts = {}
        if "experience" in df.columns:
            experience_counts = df["experience"].value_counts().to_dict()

        # Распределение по занятости
        employment_counts = {}
        if "employment" in df.columns:
            employment_counts = df["employment"].value_counts().to_dict()

        # Курсы валют (примерные)
        EXCHANGE_RATES = {
            "RUB": 1.0,
            "KZT": 0.18,      # 1 KZT = 0.18 RUB
            "UZS": 0.0072,    # 1 UZS = 0.0072 RUB
            "BYN": 28.0,      # 1 BYN = 28 RUB
            "USD": 92.0,      # 1 USD = 92 RUB
            "EUR": 100.0,     # 1 EUR = 100 RUB
        }

        def convert_to_rub(row):
            salary = row.get("salary_from")
            currency = row.get("salary_currency", "RUB")
            if pd.isna(salary):
                return None
            rate = EXCHANGE_RATES.get(currency, 1.0)
            return salary * rate

        # Создаем копию с конвертированными зарплатами
        df_rub = df.copy()
        df_rub["salary_from_rub"] = df_rub.apply(convert_to_rub, axis=1)

        # Распределение по зарплате в RUB (конвертированное)
        salary_distribution = {"< 50k": 0, "50k-100k": 0, "100k-150k": 0, "150k-200k": 0, "200k+": 0}
        if "salary_from_rub" in df_rub.columns:
            salary_df = df_rub[df_rub["salary_from_rub"].notna()]
            for salary in salary_df["salary_from_rub"]:
                if salary < 50000:
                    salary_distribution["< 50k"] += 1
                elif salary < 100000:
                    salary_distribution["50k-100k"] += 1
                elif salary < 150000:
                    salary_distribution["100k-150k"] += 1
                elif salary < 200000:
                    salary_distribution["150k-200k"] += 1
                else:
                    salary_distribution["200k+"] += 1

        # Топ регионов
        top_areas = {}
        if "area" in df.columns:
            top_areas = df["area"].value_counts().head(10).to_dict()

        # Статистика по зарплате в RUB
        salary_stats = {}
        if "salary_from_rub" in df_rub.columns:
            salary_df = df_rub[df_rub["salary_from_rub"].notna()]
            if not salary_df.empty:
                salary_stats = {
                    "min": float(salary_df["salary_from_rub"].min()),
                    "max": float(salary_df["salary_from_rub"].max()),
                    "median": float(salary_df["salary_from_rub"].median()),
                    "avg": float(salary_df["salary_from_rub"].mean())
                }

        # Статистика по валютам
        salary_stats_by_currency = {}
        if "salary_currency" in df.columns:
            for currency in df["salary_currency"].unique():
                if pd.isna(currency):
                    continue
                df_curr = df[df["salary_currency"] == currency]
                if "salary_from" in df_curr.columns:
                    salary_df_curr = df_curr[df_curr["salary_from"].notna()]
                    if not salary_df_curr.empty:
                        salary_stats_by_currency[currency] = {
                            "min": float(salary_df_curr["salary_from"].min()),
                            "max": float(salary_df_curr["salary_from"].max()),
                            "median": float(salary_df_curr["salary_from"].median()),
                            "avg": float(salary_df_curr["salary_from"].mean()),
                            "count": len(salary_df_curr)
                        }

        # Распределение по валютам
        currencies = {}
        if "salary_currency" in df.columns:
            currencies = df["salary_currency"].value_counts().to_dict()

        return {
            "experience": experience_counts,
            "employment": employment_counts,
            "salary_distribution": salary_distribution,
            "top_areas": top_areas,
            "salary_stats": salary_stats,
            "salary_stats_by_currency": salary_stats_by_currency,
            "currencies": currencies
        }
    finally:
        storage.close()

# =============================================================================
# Сбор вакансий вынесен из веб-приложения.
# Официальный API hh.ru заблокирован — сбор выполняется веб-сборщиком
# (src/web_collector.py). Веб-приложение теперь только для аналитики.
# Эндпоинты /api/parser/start|stop|status|cache удалены.
# =============================================================================

# =============================================================================
# API для истории парсинга (только чтение) и настроек
# =============================================================================

@app.get("/api/parser/last-run")
def get_last_parser_run():
    """Получить данные о последнем запуске парсера."""
    from src.storage import VacancyStorage
    storage = VacancyStorage()
    try:
        last_run = storage.get_last_parser_run(only_successful=True)
        return {"last_run": last_run}
    finally:
        storage.close()

@app.get("/api/parser/history")
def get_parser_history(limit: int = 20):
    """Получить историю запусков парсера."""
    from src.storage import VacancyStorage
    storage = VacancyStorage()
    try:
        history = storage.get_parser_runs_history(limit=limit)
        return {"history": history}
    finally:
        storage.close()

@app.get("/api/app/settings")
def get_app_settings():
    """Получить настройки приложения."""
    from src.storage import VacancyStorage
    storage = VacancyStorage()
    try:
        app_settings = storage.get_app_settings()
        return {"settings": app_settings}
    finally:
        storage.close()

# =============================================================================
# API для настроек пользователя (email для HH API)
# =============================================================================

import re

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

@app.get("/api/user/email")
def get_user_email():
    """Получить сохранённый email пользователя."""
    return {
        "email": user_settings.get("hh_user_email"),
        "is_configured": user_settings.get("hh_user_email") is not None
    }

@app.post("/api/user/email")
def set_user_email(email_data: dict):
    """Сохранить email пользователя для HH API."""
    email = email_data.get("email", "").strip()

    if not email:
        raise HTTPException(400, "Email не может быть пустым")

    if not EMAIL_REGEX.match(email):
        raise HTTPException(400, "Неверный формат email")

    user_settings["hh_user_email"] = email

    # Сохраняем email в .env для сохранения между перезапусками сервера
    try:
        if _ENV_PATH.exists():
            content = _ENV_PATH.read_text(encoding="utf-8")
        else:
            content = ""

        if "HH_USER_EMAIL=" in content:
            content = re.sub(r'HH_USER_EMAIL=.*', f'HH_USER_EMAIL={email}', content)
        else:
            content += f"\nHH_USER_EMAIL={email}\n"

        _ENV_PATH.write_text(content, encoding="utf-8")
        logger.info(f"Email сохранён в .env: {email}")
    except Exception as e:
        logger.warning(f"Не удалось записать email в .env: {e}")

    logger.info(f"Email пользователя установлен: {email}")
    return {"message": "Email сохранён", "email": email}

# =============================================================================
# API для отчётов
# =============================================================================

@app.get("/api/reports/list")
def list_reports():
    from src.config import settings
    from datetime import datetime

    reports_dir = settings.reports_dir
    if not reports_dir.exists():
        return {"reports": []}

    reports = [{"name": f.name, "size": f.stat().st_size, "created_at": datetime.fromtimestamp(f.stat().st_ctime).isoformat()} for f in reports_dir.glob("*.xlsx")]
    return {"reports": sorted(reports, key=lambda x: x["created_at"], reverse=True)}

@app.get("/api/reports/download/{filename:path}")
def download_report(filename: str):
    from src.config import settings
    from fastapi.responses import FileResponse

    # Защита от path traversal: итоговый путь обязан лежать внутри reports_dir.
    # Отсекаем абсолютные пути и последовательности "..".
    reports_dir = settings.reports_dir.resolve()
    filepath = (reports_dir / filename).resolve()
    if not filepath.is_relative_to(reports_dir):
        raise HTTPException(400, "Некорректное имя файла")
    if not filepath.is_file():
        raise HTTPException(404, "Отчёт не найден")

    return FileResponse(path=filepath, filename=filepath.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.post("/api/reports/generate")
def generate_report(background_tasks: BackgroundTasks):
    def run():
        from src.storage import VacancyStorage
        from src.analyzer import VacancyAnalyzer
        from src.advanced_analyzer import AdvancedAnalytics

        storage = VacancyStorage()
        try:
            df = storage.get_all_vacancies()
            if not df.empty:
                VacancyAnalyzer(df).generate_excel_report()
                AdvancedAnalytics(df).generate_detailed_excel_report()
        finally:
            storage.close()

    background_tasks.add_task(run)
    return {"message": "Генерация запущена"}

@app.get("/api/export/vacancies")
def export_vacancies(format: str = "csv"):
    from src.storage import VacancyStorage
    from src.config import settings
    from datetime import datetime

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()
        if df.empty:
            raise HTTPException(404, "Нет данных")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if format == "csv":
            filepath = settings.reports_dir / f"vacancies_export_{timestamp}.csv"
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
            media_type = "text/csv"
        else:
            filepath = settings.reports_dir / f"vacancies_export_{timestamp}.xlsx"
            df.to_excel(filepath, index=False, engine="openpyxl")
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

        return FileResponse(path=filepath, filename=filepath.name, media_type=media_type)
    finally:
        storage.close()

# =============================================================================
# API для профессий
# =============================================================================

# Кэш для каталога профессий
_professions_catalog_cache = None

def _load_professions_catalog():
    """Загрузить каталог профессий из файла."""
    global _professions_catalog_cache

    if _professions_catalog_cache is not None:
        return _professions_catalog_cache

    catalog_path = PROJECT_ROOT / "data" / "professions_catalog.json"

    if catalog_path.exists():
        import json
        with open(catalog_path, "r", encoding="utf-8") as f:
            _professions_catalog_cache = json.load(f)
    else:
        _professions_catalog_cache = {"professions": {}, "total": 0}

    return _professions_catalog_cache

@app.get("/api/professions/list")
def get_professions_list(
    domain: Optional[str] = None,
    sphere: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100
):
    """Получить список профессий с фильтрами."""
    catalog = _load_professions_catalog()

    professions = list(catalog.get("professions", {}).values())

    # Фильтр по домену
    if domain:
        professions = [p for p in professions if p.get("domain") == domain]

    # Фильтр по сфере
    if sphere:
        professions = [p for p in professions if p.get("sphere") == sphere]

    # Поиск по названию
    if search:
        search_lower = search.lower()
        professions = [p for p in professions if search_lower in p.get("name", "").lower()]

    # Сортировка по количеству вакансий
    professions.sort(key=lambda x: x.get("vacancies_count", 0), reverse=True)

    # Ограничение
    professions = professions[:limit]

    # Форматируем ответ
    return {
        "professions": professions,
        "total": len(professions),
        "domains": catalog.get("domains", {}),
        "areas": catalog.get("areas", {})
    }

@app.get("/api/professions/detail/{profession_key:path}")
def get_profession_detail(profession_key: str):
    """Получить детальную информацию о профессии."""
    catalog = _load_professions_catalog()

    profession = catalog.get("professions", {}).get(profession_key)

    if not profession:
        raise HTTPException(status_code=404, detail="Профессия не найдена")

    return profession

@app.get("/api/autocomplete/vacancies")
def autocomplete_vacancies(q: str = Query(..., min_length=2)):
    """
    Автодополнение названий вакансий.

    Args:
        q: Поисковый запрос (минимум 2 символа)

    Returns:
        Список подходящих названий вакансий
    """
    from src.storage import VacancyStorage
    import pandas as pd

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()
        if df.empty:
            return {"suggestions": []}

        # Фильтрация по запросу (case-insensitive)
        mask = df['vacancy_name'].str.contains(q, case=False, na=False)
        matches = df[mask]

        # Уникальные названия + сортировка по частоте + топ-10
        unique = matches['vacancy_name'].value_counts().head(10).index.tolist()

        return {
            "suggestions": [
                {"label": name, "value": name, "count": int(count)}
                for name, count in zip(unique, matches['vacancy_name'].value_counts().head(10).values)
            ],
            "total": len(unique)
        }
    finally:
        storage.close()

@app.get("/api/autocomplete/areas")
def autocomplete_areas(q: str = Query(..., min_length=2)):
    """
    Автодополнение регионов.

    Args:
        q: Поисковый запрос (минимум 2 символа)

    Returns:
        Список подходящих регионов
    """
    from src.storage import VacancyStorage

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()
        if df.empty:
            return {"suggestions": []}

        # Фильтрация
        mask = df['area'].str.contains(q, case=False, na=False)
        matches = df[mask]

        # Уникальные регионы + сортировка по частоте
        unique = matches['area'].value_counts().head(10).index.tolist()

        return {
            "suggestions": [
                {"label": area, "value": area, "count": int(count)}
                for area, count in zip(unique, matches['area'].value_counts().head(10).values)
            ],
            "total": len(unique)
        }
    finally:
        storage.close()

@app.get("/api/autocomplete/skills")
def autocomplete_skills(q: str = Query(..., min_length=2)):
    """
    Автодополнение навыков (hard/soft skills, tools).

    Args:
        q: Поисковый запрос (минимум 2 символа)

    Returns:
        Список подходящих навыков
    """
    from src.storage import VacancyStorage
    from collections import Counter

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()
        if df.empty:
            return {"suggestions": []}

        # Собираем все навыки
        all_skills = []

        for col in ['hard_skills', 'soft_skills', 'tools']:
            if col in df.columns:
                for skills_str in df[col].dropna():
                    if isinstance(skills_str, str):
                        skills = [s.strip() for s in skills_str.split(',')]
                        all_skills.extend([s for s in skills if s])

        # Фильтруем по запросу
        q_lower = q.lower()
        matches = [s for s in all_skills if q_lower in s.lower()]

        # Подсчёт частоты + топ-15
        counter = Counter(matches)
        top_skills = counter.most_common(15)

        return {
            "suggestions": [
                {"label": skill, "value": skill, "count": count}
                for skill, count in top_skills
            ],
            "total": len(counter)
        }
    finally:
        storage.close()

# Кэш для autocomplete (в памяти)
_autocomplete_cache = {}
_cache_ttl = 300  # 5 минут

@app.get("/api/analytics/kpi")
def get_kpi_metrics(
    period: Optional[str] = Query("all_time", description="Период: today|week|month|quarter|year|all_time"),
    domain: Optional[str] = Query(None, description="Домен профессии"),
    regions: Optional[str] = Query(None, description="Регионы через запятую"),
    experience: Optional[str] = Query(None, description="Опыт работы"),
    salary_only: bool = Query(False, description="Только вакансии с зарплатой")
):
    """
    KPI метрики с трендами для дашборда с поддержкой фильтров.

    Возвращает 8 ключевых метрик с процентами роста/падения.
    """
    from src.storage import VacancyStorage
    from datetime import datetime, timedelta
    import pandas as pd

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()

        if df.empty:
            return {
                "metrics": {},
                "period": period,
                "last_updated": datetime.now().isoformat()
            }

        # === Применяем фильтры ===

        # Фильтр по периоду
        if period and period != "all_time":
            if 'published_at' in df.columns:
                df['published_at'] = pd.to_datetime(df['published_at'], errors='coerce')
                # Получаем часовой пояс из колонки published_at
                tz = df['published_at'].dt.tz
                now = pd.Timestamp.now(tz=tz) if tz else datetime.now()
                if period == "today":
                    df = df[df['published_at'] >= now.replace(hour=0, minute=0, second=0, microsecond=0)]
                elif period == "week":
                    df = df[df['published_at'] >= now - timedelta(days=7)]
                elif period == "month":
                    df = df[df['published_at'] >= now - timedelta(days=30)]
                elif period == "quarter":
                    df = df[df['published_at'] >= now - timedelta(days=90)]
                elif period == "year":
                    df = df[df['published_at'] >= now - timedelta(days=365)]

        # Фильтр по домену (если есть колонка domain/profession)
        if domain:
            if 'domain' in df.columns:
                df = df[df['domain'].str.contains(domain, case=False, na=False)]
            elif 'profession' in df.columns:
                df = df[df['profession'].str.contains(domain, case=False, na=False)]

        # Фильтр по регионам
        if regions:
            region_list = [r.strip() for r in regions.split(',')]
            if 'area' in df.columns:
                mask = df['area'].str.contains('|'.join(region_list), case=False, na=False)
                df = df[mask]

        # Фильтр по опыту
        if experience:
            if 'experience' in df.columns:
                df = df[df['experience'] == experience]

        # Фильтр только с зарплатой
        if salary_only:
            if 'salary_from' in df.columns:
                df = df[df['salary_from'].notna()]

        if df.empty:
            return {
                "metrics": {},
                "period": period,
                "filters": {
                    "period": period,
                    "domain": domain,
                    "regions": regions,
                    "experience": experience,
                    "salary_only": salary_only
                },
                "last_updated": datetime.now().isoformat()
            }

        # === Расчёт метрик ===

        # 1. Средняя зарплата
        salary_df = df[df['salary_from'].notna()]
        avg_salary = float(salary_df['salary_from'].mean()) if not salary_df.empty else 0

        # 2. Медианная зарплата
        median_salary = float(salary_df['salary_from'].median()) if not salary_df.empty else 0

        # 3. Всего вакансий
        total_vacancies = len(df)

        # 4. Навыков найдено (сумма всех навыков)
        total_skills = int(df['skill_count'].sum()) if 'skill_count' in df.columns else 0

        # 5. Уникальных компаний
        unique_companies = df['employer_name'].nunique() if 'employer_name' in df.columns else 0

        # 6. Уникальных регионов
        unique_regions = df['area'].nunique() if 'area' in df.columns else 0

        # 7. Hard Skills (подсчёт уникальных)
        hard_skills_set = set()
        if 'hard_skills' in df.columns:
            for skills_str in df['hard_skills'].dropna():
                if isinstance(skills_str, str):
                    skills = [s.strip() for s in skills_str.split(',')]
                    hard_skills_set.update([s for s in skills if s])
        hard_skills_count = len(hard_skills_set)

        # 8. Soft Skills (подсчёт уникальных)
        soft_skills_set = set()
        if 'soft_skills' in df.columns:
            for skills_str in df['soft_skills'].dropna():
                if isinstance(skills_str, str):
                    skills = [s.strip() for s in skills_str.split(',')]
                    soft_skills_set.update([s for s in skills if s])
        soft_skills_count = len(soft_skills_set)

        # === Расчёт трендов (сравнение с предыдущим периодом) ===
        # Для демо используем случайные значения (в продакшене — сравнение дат)
        import random
        def calc_trend(current_value, min_percent=-20, max_percent=30):
            """Генерация тренда для демо"""
            trend_percent = random.uniform(min_percent, max_percent)
            direction = "up" if trend_percent > 0 else "down" if trend_percent < 0 else "same"
            return {
                "trend_percent": round(abs(trend_percent), 1),
                "trend_direction": direction
            }

        return {
            "metrics": {
                "avg_salary": {
                    "value": round(avg_salary, 2),
                    "currency": "RUB",
                    **calc_trend(avg_salary, -10, 25)
                },
                "median_salary": {
                    "value": round(median_salary, 2),
                    "currency": "RUB",
                    **calc_trend(median_salary, -5, 20)
                },
                "total_vacancies": {
                    "value": total_vacancies,
                    **calc_trend(total_vacancies, -15, 40)
                },
                "total_skills": {
                    "value": total_skills,
                    **calc_trend(total_skills, 0, 50)
                },
                "unique_companies": {
                    "value": unique_companies,
                    **calc_trend(unique_companies, -10, 30)
                },
                "unique_regions": {
                    "value": unique_regions,
                    **calc_trend(unique_regions, 0, 15)
                },
                "hard_skills_count": {
                    "value": hard_skills_count,
                    **calc_trend(hard_skills_count, 0, 40)
                },
                "soft_skills_count": {
                    "value": soft_skills_count,
                    **calc_trend(soft_skills_count, 0, 30)
                }
            },
            "period": period,
            "filters": {
                "period": period,
                "domain": domain,
                "regions": regions,
                "experience": experience,
                "salary_only": salary_only
            },
            "last_updated": datetime.now().isoformat()
        }

    finally:
        storage.close()

@app.get("/api/analytics/top-skills")
def get_top_skills(
    type: str = Query("hard", pattern="^(hard|soft|tools)$"),
    limit: int = Query(20, ge=1, le=100),
    period: Optional[str] = Query("all_time", description="Период: today|week|month|quarter|year|all_time"),
    domain: Optional[str] = Query(None, description="Домен профессии"),
    regions: Optional[str] = Query(None, description="Регионы через запятую"),
    experience: Optional[str] = Query(None, description="Опыт работы")
):
    """
    Топ навыков по типу с поддержкой фильтров.

    Args:
        type: hard|soft|tools
        limit: 1-100 (default 20)
        period: Период фильтрации
        domain: Домен профессии
        regions: Регионы через запятую
        experience: Опыт работы

    Returns:
        Список навыков с количеством и процентами
    """
    from src.storage import VacancyStorage
    from datetime import timedelta
    from collections import Counter
    import pandas as pd

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()

        if df.empty:
            return {
                "type": type,
                "total_unique": 0,
                "skills": [],
                "filters": {
                    "type": type,
                    "limit": limit,
                    "period": period,
                    "domain": domain,
                    "regions": regions,
                    "experience": experience
                }
            }

        # === Применяем фильтры ===

        # Фильтр по периоду
        if period and period != "all_time":
            if 'published_at' in df.columns:
                df['published_at'] = pd.to_datetime(df['published_at'], errors='coerce')
                # Получаем часовой пояс из колонки published_at
                tz = df['published_at'].dt.tz
                now = pd.Timestamp.now(tz=tz) if tz else datetime.now()
                if period == "today":
                    df = df[df['published_at'] >= now.replace(hour=0, minute=0, second=0, microsecond=0)]
                elif period == "week":
                    df = df[df['published_at'] >= now - timedelta(days=7)]
                elif period == "month":
                    df = df[df['published_at'] >= now - timedelta(days=30)]
                elif period == "quarter":
                    df = df[df['published_at'] >= now - timedelta(days=90)]
                elif period == "year":
                    df = df[df['published_at'] >= now - timedelta(days=365)]

        # Фильтр по домену
        if domain:
            if 'domain' in df.columns:
                df = df[df['domain'].str.contains(domain, case=False, na=False)]
            elif 'profession' in df.columns:
                df = df[df['profession'].str.contains(domain, case=False, na=False)]

        # Фильтр по регионам
        if regions:
            region_list = [r.strip() for r in regions.split(',')]
            if 'area' in df.columns:
                mask = df['area'].str.contains('|'.join(region_list), case=False, na=False)
                df = df[mask]

        # Фильтр по опыту
        if experience:
            if 'experience' in df.columns:
                df = df[df['experience'] == experience]

        if df.empty:
            return {
                "type": type,
                "total_unique": 0,
                "skills": [],
                "filters": {
                    "type": type,
                    "limit": limit,
                    "period": period,
                    "domain": domain,
                    "regions": regions,
                    "experience": experience
                }
            }

        # Собираем все навыки
        all_skills = []
        column_map = {
            "hard": "hard_skills",
            "soft": "soft_skills",
            "tools": "tools"
        }

        column_name = column_map.get(type)
        if column_name and column_name in df.columns:
            for skills_str in df[column_name].dropna():
                if isinstance(skills_str, str):
                    skills = [s.strip() for s in skills_str.split(',')]
                    all_skills.extend([s for s in skills if s])

        # Подсчёт частоты
        counter = Counter(all_skills)
        total_unique = len(counter)

        # Топ-N с процентами
        total_vacancies = len(df)
        top_skills = counter.most_common(limit)

        return {
            "type": type,
            "total_unique": total_unique,
            "skills": [
                {
                    "name": skill,
                    "count": count,
                    "percentage": round((count / total_vacancies) * 100, 1) if total_vacancies > 0 else 0
                }
                for skill, count in top_skills
            ],
            "filters": {
                "type": type,
                "limit": limit,
                "period": period,
                "domain": domain,
                "regions": regions,
                "experience": experience
            }
        }

    finally:
        storage.close()

# Кэш для autocomplete (в памяти)
_autocomplete_cache = {}
_cache_ttl = 300  # 5 минут

@app.get("/api/autocomplete/cached")
def autocomplete_cached(
    type: str = Query(..., pattern="^(vacancies|areas|skills)$"),
    q: str = Query(..., min_length=2)
):
    """
    Универсальный endpoint с кэшированием.

    Args:
        type: Тип autocomplete (vacancies|areas|skills)
        q: Поисковый запрос

    Returns:
        Кэшированный результат или новый запрос
    """
    import time
    from datetime import datetime

    cache_key = f"{type}:{q}"

    # Проверка кэша
    if cache_key in _autocomplete_cache:
        cached_data, cached_time = _autocomplete_cache[cache_key]
        if time.time() - cached_time < _cache_ttl:
            return {**cached_data, "cached": True}

    # Новый запрос
    if type == "vacancies":
        result = autocomplete_vacancies(q)
    elif type == "areas":
        result = autocomplete_areas(q)
    else:
        result = autocomplete_skills(q)

    # Сохранение в кэш
    _autocomplete_cache[cache_key] = (result, time.time())

    # Очистка старого кэша (раз в 100 запросов)
    if len(_autocomplete_cache) > 100:
        now = time.time()
        _autocomplete_cache = {
            k: v for k, v in _autocomplete_cache.items()
            if now - v[1] < _cache_ttl
        }

    return {**result, "cached": False}

@app.get("/api/professions/vacancies")
def get_profession_vacancies(name: str):
    """Получить вакансии для профессии."""
    catalog = _load_professions_catalog()

    # Ищем профессию по названию
    profession = None
    for prof in catalog.get("professions", {}).values():
        if name.lower() in prof.get("name", "").lower():
            profession = prof
            break

    if not profession:
        return {"vacancies": []}

    # Возвращаем примеры вакансий
    return {
        "vacancies": profession.get("sample_vacancies", [])[:10]
    }

# =============================================================================
# Экспорт аналитики (PDF, Excel, CSV)
# =============================================================================

@app.post("/api/export/analytics")
def export_analytics(
    format: str = Query("xlsx", pattern="^(pdf|xlsx|csv)$"),
    period: Optional[str] = Query("all_time"),
    domain: Optional[str] = Query(None),
    regions: Optional[str] = Query(None),
    experience: Optional[str] = Query(None),
    salary_only: bool = Query(False)
):
    """
    Экспорт аналитического отчёта в формате PDF, Excel или CSV.

    Args:
        format: Формат экспорта (pdf|xlsx|csv)
        period: Период фильтрации
        domain: Домен профессии
        regions: Регионы через запятую
        experience: Опыт работы
        salary_only: Только вакансии с зарплатой

    Returns:
        Файл отчёта
    """
    from src.storage import VacancyStorage
    from src.config import settings
    from datetime import datetime
    import pandas as pd
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    storage = VacancyStorage()
    try:
        df = storage.get_all_vacancies()

        if df.empty:
            raise HTTPException(status_code=404, detail="Нет данных для экспорта")

        # Применяем фильтры
        if period and period != "all_time":
            if 'published_at' in df.columns:
                df['published_at'] = pd.to_datetime(df['published_at'], errors='coerce')
                # Получаем часовой пояс из колонки published_at
                tz = df['published_at'].dt.tz
                now = pd.Timestamp.now(tz=tz) if tz else datetime.now()
                if period == "today":
                    df = df[df['published_at'] >= now.replace(hour=0, minute=0, second=0, microsecond=0)]
                elif period == "week":
                    df = df[df['published_at'] >= now - timedelta(days=7)]
                elif period == "month":
                    df = df[df['published_at'] >= now - timedelta(days=30)]
                elif period == "quarter":
                    df = df[df['published_at'] >= now - timedelta(days=90)]
                elif period == "year":
                    df = df[df['published_at'] >= now - timedelta(days=365)]

        if domain:
            if 'domain' in df.columns:
                df = df[df['domain'].str.contains(domain, case=False, na=False)]
            elif 'profession' in df.columns:
                df = df[df['profession'].str.contains(domain, case=False, na=False)]

        if regions:
            region_list = [r.strip() for r in regions.split(',')]
            if 'area' in df.columns:
                mask = df['area'].str.contains('|'.join(region_list), case=False, na=False)
                df = df[mask]

        if experience:
            if 'experience' in df.columns:
                df = df[df['experience'] == experience]

        if salary_only:
            if 'salary_from' in df.columns:
                df = df[df['salary_from'].notna()]

        if df.empty:
            raise HTTPException(status_code=404, detail="Нет данных после применения фильтров")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"hh_analytics_{timestamp}"

        if format == "csv":
            # CSV экспорт
            output = BytesIO()
            df.to_csv(output, index=False, encoding="utf-8-sig")
            output.seek(0)

            return StreamingResponse(
                output,
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
            )

        elif format == "xlsx":
            # Excel экспорт с несколькими листами
            output = BytesIO()

            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                # Лист 1: KPI метрики
                kpi_data = {
                    'Метрика': [
                        'Всего вакансий',
                        'Средняя зарплата',
                        'Медианная зарплата',
                        'Уникальных компаний',
                        'Уникальных регионов',
                        'Всего навыков',
                        'Hard Skills (уникальных)',
                        'Soft Skills (уникальных)'
                    ],
                    'Значение': [
                        len(df),
                        round(df[df['salary_from'].notna()]['salary_from'].mean(), 2) if not df[df['salary_from'].notna()].empty else 0,
                        round(df[df['salary_from'].notna()]['salary_from'].median(), 2) if not df[df['salary_from'].notna()].empty else 0,
                        df['employer_name'].nunique() if 'employer_name' in df.columns else 0,
                        df['area'].nunique() if 'area' in df.columns else 0,
                        int(df['skill_count'].sum()) if 'skill_count' in df.columns else 0,
                        len(set(s for skills_str in df['hard_skills'].dropna() if isinstance(skills_str, str) for s in skills_str.split(','))),
                        len(set(s for skills_str in df['soft_skills'].dropna() if isinstance(skills_str, str) for s in skills_str.split(',')))
                    ]
                }
                kpi_df = pd.DataFrame(kpi_data)
                kpi_df.to_excel(writer, sheet_name='KPI', index=False)

                # Лист 2: Hard Skills топ
                if 'hard_skills' in df.columns:
                    hard_skills = []
                    for skills_str in df['hard_skills'].dropna():
                        if isinstance(skills_str, str):
                            hard_skills.extend([s.strip() for s in skills_str.split(',') if s.strip()])
                    from collections import Counter
                    hard_counts = Counter(hard_skills)
                    hard_df = pd.DataFrame([
                        {'Навык': skill, 'Количество': count}
                        for skill, count in hard_counts.most_common(50)
                    ])
                    hard_df.to_excel(writer, sheet_name='Hard Skills', index=False)

                # Лист 3: Soft Skills топ
                if 'soft_skills' in df.columns:
                    soft_skills = []
                    for skills_str in df['soft_skills'].dropna():
                        if isinstance(skills_str, str):
                            soft_skills.extend([s.strip() for s in skills_str.split(',') if s.strip()])
                    soft_counts = Counter(soft_skills)
                    soft_df = pd.DataFrame([
                        {'Навык': skill, 'Количество': count}
                        for skill, count in soft_counts.most_common(50)
                    ])
                    soft_df.to_excel(writer, sheet_name='Soft Skills', index=False)

                # Лист 4: Tools топ
                if 'tools' in df.columns:
                    tools = []
                    for skills_str in df['tools'].dropna():
                        if isinstance(skills_str, str):
                            tools.extend([s.strip() for s in skills_str.split(',') if s.strip()])
                    tools_counts = Counter(tools)
                    tools_df = pd.DataFrame([
                        {'Инструмент': skill, 'Количество': count}
                        for skill, count in tools_counts.most_common(50)
                    ])
                    tools_df.to_excel(writer, sheet_name='Tools', index=False)

                # Лист 5: Сырые данные (первые 1000 записей)
                raw_df = df.head(1000).copy()
                raw_df.to_excel(writer, sheet_name='Данные', index=False)

                # Форматирование
                workbook = writer.book
                header_format = workbook.add_format({
                    'bold': True,
                    'bg_color': '#4472C4',
                    'font_color': 'white',
                    'border': 1
                })

                for worksheet in writer.sheets.values():
                    for col_num in range(len(raw_df.columns) if worksheet.name == 'Данные' else 2):
                        worksheet.set_column(col_num, col_num, 20)

            output.seek(0)

            return StreamingResponse(
                output,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={filename}.xlsx"}
            )

        elif format == "pdf":
            # PDF экспорт
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import inch
            from reportlab.lib.enums import TA_CENTER, TA_LEFT
            from collections import Counter

            output = BytesIO()
            doc = SimpleDocTemplate(
                output,
                pagesize=landscape(A4),
                rightMargin=0.5*inch,
                leftMargin=0.5*inch,
                topMargin=0.5*inch,
                bottomMargin=0.5*inch
            )

            elements = []
            styles = getSampleStyleSheet()

            # Заголовок
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=18,
                textColor=colors.HexColor('#1e40af'),
                spaceAfter=12,
                alignment=TA_CENTER
            )

            elements.append(Paragraph("HH.ru Analytics — Отчёт", title_style))

            # Информация о фильтрах
            filter_info = f"Период: {period} | Вакансий: {len(df)} | Дата генерации: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            elements.append(Paragraph(filter_info, styles['Normal']))
            elements.append(Spacer(1, 0.3*inch))

            # KPI таблица
            kpi_data = [
                ['Метрика', 'Значение'],
                ['Всего вакансий', str(len(df))],
                ['Средняя зарплата', f"{round(df[df['salary_from'].notna()]['salary_from'].mean(), 2) if not df[df['salary_from'].notna()].empty else 'N/A'} ₽"],
                ['Медианная зарплата', f"{round(df[df['salary_from'].notna()]['salary_from'].median(), 2) if not df[df['salary_from'].notna()].empty else 'N/A'} ₽"],
                ['Hard Skills (уникальных)', str(len(set(s for skills_str in df['hard_skills'].dropna() if isinstance(skills_str, str) for s in skills_str.split(','))))],
                ['Soft Skills (уникальных)', str(len(set(s for skills_str in df['soft_skills'].dropna() if isinstance(skills_str, str) for s in skills_str.split(','))))]
            ]

            kpi_table = Table(kpi_data, colWidths=[3*inch, 2*inch])
            kpi_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f9ff')])
            ]))
            elements.append(kpi_table)
            elements.append(Spacer(1, 0.3*inch))

            # Топ Hard Skills
            elements.append(Paragraph("Топ-20 Hard Skills", styles['Heading2']))
            elements.append(Spacer(1, 0.2*inch))

            if 'hard_skills' in df.columns:
                hard_skills = []
                for skills_str in df['hard_skills'].dropna():
                    if isinstance(skills_str, str):
                        hard_skills.extend([s.strip() for s in skills_str.split(',') if s.strip()])
                hard_counts = Counter(hard_skills)

                hard_data = [['Навык', 'Количество', '%']]
                total = len(df)
                for skill, count in hard_counts.most_common(20):
                    hard_data.append([skill, str(count), f"{round(count/total*100, 1)}%"])

                hard_table = Table(hard_data, colWidths=[2.5*inch, 1.5*inch, 1*inch])
                hard_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#059669')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#ecfdf5')])
                ]))
                elements.append(hard_table)

            # Топ Soft Skills
            elements.append(Spacer(1, 0.3*inch))
            elements.append(Paragraph("Топ-20 Soft Skills", styles['Heading2']))
            elements.append(Spacer(1, 0.2*inch))

            if 'soft_skills' in df.columns:
                soft_skills = []
                for skills_str in df['soft_skills'].dropna():
                    if isinstance(skills_str, str):
                        soft_skills.extend([s.strip() for s in skills_str.split(',') if s.strip()])
                soft_counts = Counter(soft_skills)

                soft_data = [['Навык', 'Количество', '%']]
                total = len(df)
                for skill, count in soft_counts.most_common(20):
                    soft_data.append([skill, str(count), f"{round(count/total*100, 1)}%"])

                soft_table = Table(soft_data, colWidths=[2.5*inch, 1.5*inch, 1*inch])
                soft_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#7c3aed')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f5f3ff')])
                ]))
                elements.append(soft_table)

            doc.build(elements)
            output.seek(0)

            return StreamingResponse(
                output,
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={filename}.pdf"}
            )

    finally:
        storage.close()

# =============================================================================
# Запуск
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("🚀 HH.ru Analytics — Веб-приложение")
    print("=" * 60)
    print(f"📁 Project: {PROJECT_ROOT}")
    print(f"📁 Static: {STATIC_DIR}")
    print(f"✅ Static exists: {STATIC_DIR.exists()}")
    print()
    print("🌐 Откройте в браузере:")
    print("   http://localhost:8000")
    print("   http://localhost:8000/docs")
    print("=" * 60)

    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
