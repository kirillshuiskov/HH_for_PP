#!/usr/bin/env python3
"""
Сбор и аналитика по Sales / BDM вакансиям (приоритет пользователя).

Изолированный прогон: сырьё в data/raw_sales/, обработка в data/processed_sales/,
отчёты в data/reports_sales/. Не трогает существующие IT-данные в data/raw.

Запуск:
    python collect_sales_analytics.py            # собрать + обработать + аналитика
    python collect_sales_analytics.py --collect  # только сбор
    python collect_sales_analytics.py --analyze  # только аналитика по уже собранному
"""

import argparse
import sys
import json
from pathlib import Path
from collections import Counter

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings
from src.utils import get_logger
from src.web_collector import HHWebCollector, SALES_ROLE_HINTS
from src.processor import VacancyProcessor

logger = get_logger(__name__)

RAW_DIR = PROJECT_ROOT / "data" / "raw_sales"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed_sales"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports_sales"

# Sales / BDM — ключевые запросы. BDM идёт первым (приоритет).
# Расширенный BDM-набор (дособираем поверх уже собранных 493 в data/raw_sales/).
# Senior-роли (Head/Director/VP) исключены — не целевая роль пользователя «пока что».
# Проверка написания на hh.ru: правильное "business" даёт 495, опечатка "bussiness" — ~1.
KEYWORDS = [
    "B2B sales manager",
    "development manager",
    "менеджер по партнёрствам",
    "партнёрский менеджер",
    "channel manager",
    "менеджер по развитию",
    "buisness development manager",         # реальная опечатка на hh.ru (2 вакансии)
]

# Подстроки в названии вакансии, которые считаем нерелевантными и выкидываем
# из аналитики целиком (даже если попали в сбор).
# Senior-роли (head/director/VP/руководитель отдела) — не целевой уровень пользователя.
IRRELEVANT_HINTS = [
    "торговый представител", "мерчендайз", "мерчандайз",
    # senior / leadership
    "head of", "director of", "vp of sales", "vp sales", "sales director",
    "директор по продажам", "директор по продаж", "директор по развитию",
    "руководитель отдела продаж", "начальник отдела продаж", "head of sales",
    "head of business", "директор по бизнес",
]

MAX_PER_KEYWORD = 50


def run_collect(schedule: str = "remote", area=None, max_per_keyword=MAX_PER_KEYWORD):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Сбор Sales/BDM вакансий ===")
    print(f"Ключевых запросов: {len(KEYWORDS)}, по {max_per_keyword} на запрос")
    print(f"schedule={schedule}, area={area}, output={RAW_DIR}\n")

    with HHWebCollector(
        schedule=schedule,
        area=area,
        delay=1.5,
        output_dir=RAW_DIR,
    ) as collector:
        vacs = collector.collect(
            keywords=KEYWORDS,
            max_per_keyword=max_per_keyword,
            per_page=50,
            fetch_details=True,
            save=True,
        )
    print(f"\nСобрано уникальных вакансий: {len(vacs)}")
    return vacs


def run_process():
    print(f"\n=== Обработка (Transform) ===")
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    processor = VacancyProcessor(output_dir=PROCESSED_DIR)
    df = processor.process_all(input_dir=RAW_DIR, save_csv=True, save_parquet=True)
    if df.empty:
        print("Нет данных для обработки.")
        return None
    print(f"Обработано вакансий: {len(df)}")
    return df


def _money(row):
    """Оценка зарплаты: берём 'to', если нет — 'from' (gross → net ×0.87)."""
    s_from = row.get("salary_from")
    s_to = row.get("salary_to")
    gross = row.get("salary_gross", False)
    cur = row.get("salary_currency", "RUB")
    val = s_to if s_to else s_from
    if val is None:
        return None
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    if gross:
        val = val * 0.87
    return val, cur


def run_analyze(df=None):
    import pandas as pd
    csv_path = PROCESSED_DIR / "vacancies_processed.csv"
    if df is None:
        if not csv_path.exists():
            print(f"CSV не найден: {csv_path}. Запусти --collect сначала.")
            return
        df = pd.read_csv(csv_path)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Выкидываем нерелевантные роли (напр. «торговый представитель») целиком
    def _is_irrelevant(name: str) -> bool:
        s = (name or "").lower()
        return any(h in s for h in IRRELEVANT_HINTS)

    mask_irr = df["vacancy_name"].fillna("").apply(_is_irrelevant)
    dropped = int(mask_irr.sum())
    if dropped:
        df = df[~mask_irr].reset_index(drop=True)
        print(f"\n(отброшено нерелевантных вакансий: {dropped})")

    n = len(df)
    print(f"\n{'='*70}")
    print(f"📊 АНАЛИТИКА ПО SALES / BDM  (вакансий: {n})")
    print(f"{'='*70}")

    # --- 1. Классификация: BDM vs Sales vs KAM/AM по названию ---
    def classify(name: str) -> str:
        s = name.lower()
        if any(h in s for h in ["business development", "развитие бизнеса", "bdm", "по развитию"]):
            return "BDM"
        if any(h in s for h in ["key account", "kam", "ключев"]):
            return "KAM"
        if "account manager" in s or "аккаунт" in s:
            return "Account Manager"
        if any(h in s for h in ["enterprise", "saas", "it sales", "продажи it", "digital"]):
            return "Enterprise/SaaS Sales"
        if any(h in s for h in ["торговый представител", "field", "разъездн"]):
            return "Field Sales"
        if "продаж" in s or "sales" in s:
            return "Sales (общее)"
        return "Прочее"

    df = df.copy()
    df["role_group"] = df["vacancy_name"].fillna("").apply(classify)
    print("\n— Распределение по группам ролей:")
    rg = df["role_group"].value_counts()
    for role, cnt in rg.items():
        print(f"  • {role:<24} {cnt:>4}  ({cnt/n*100:5.1f}%)")

    # --- 2. Зарплаты (только RUB) ---
    rub = df[df["salary_currency"].fillna("RUB") == "RUB"].copy()
    rub["salary_val"] = rub.apply(lambda r: (_money(r)[0] if _money(r) else None), axis=1)
    rub_with = rub.dropna(subset=["salary_val"])
    print(f"\n— Зарплата (RUB, на руки, указана у {len(rub_with)}/{len(rub)} RUB-вакансий):")
    if len(rub_with) >= 3:
        print(f"  медиана: {rub_with['salary_val'].median():,.0f}  "
              f"среднее: {rub_with['salary_val'].mean():,.0f}  "
              f"P25: {rub_with['salary_val'].quantile(0.25):,.0f}  "
              f"P75: {rub_with['salary_val'].quantile(0.75):,.0f}")
        print("  по группам ролей (медиана, n):")
        for role, grp in rub_with.groupby("role_group"):
            if len(grp) >= 2:
                print(f"    {role:<24} мед={grp['salary_val'].median():>9,.0f}  n={len(grp)}")

    # --- 3. Опыт ---
    print("\n— Требуемый опыт:")
    for exp, cnt in df["experience"].fillna("не указан").value_counts().items():
        print(f"  • {exp:<28} {cnt:>4}  ({cnt/n*100:5.1f}%)")

    # --- 4. Формат работы ---
    print("\n— Формат работы (по schedule):")
    sched = df["schedule"].fillna("").apply(
        lambda s: "Удалённо" if "Удалённ" in s else ("Гибрид" if "Гибрид" in s else ("Офис" if "На месте" in s else "иное/не указан"))
    )
    for fmt, cnt in sched.value_counts().items():
        print(f"  • {fmt:<24} {cnt:>4}  ({cnt/n*100:5.1f}%)")

    # --- 5. Регионы ---
    print("\n— Топ-10 регионов:")
    for area, cnt in df["area"].fillna("не указан").value_counts().head(10).items():
        print(f"  • {area:<28} {cnt:>4}")

    # --- 6. Топ работодатели ---
    print("\n— Топ-15 работодателей:")
    for emp, cnt in df["employer_name"].fillna("не указан").value_counts().head(15).items():
        print(f"  • {emp:<40} {cnt:>3}")

    # --- 7. Навыки ---
    def top_skills(column, top=20):
        c = Counter()
        for s in df[column].dropna():
            if isinstance(s, str):
                for sk in s.split(","):
                    sk = sk.strip()
                    if sk:
                        c[sk] += 1
        return c.most_common(top)

    print("\n— Топ-20 Hard Skills:")
    for sk, cnt in top_skills("hard_skills"):
        print(f"  • {sk:<32} {cnt:>4}  ({cnt/n*100:4.1f}%)")

    print("\n— Топ-15 Soft Skills:")
    for sk, cnt in top_skills("soft_skills", 15):
        print(f"  • {sk:<32} {cnt:>4}  ({cnt/n*100:4.1f}%)")

    print("\n— Топ-15 Tools:")
    for sk, cnt in top_skills("tools", 15):
        print(f"  • {sk:<32} {cnt:>4}  ({cnt/n*100:4.1f}%)")

    # --- 8. BDM-фокус (приоритет) ---
    bdm = df[df["role_group"] == "BDM"]
    if len(bdm) >= 3:
        print(f"\n{'-'*70}")
        print(f"🎯 BDM-фокус  (n={len(bdm)})")
        print(f"{'-'*70}")
        bdm_rub = bdm[bdm["salary_currency"].fillna("RUB") == "RUB"].copy()
        bdm_rub["salary_val"] = bdm_rub.apply(lambda r: (_money(r)[0] if _money(r) else None), axis=1)
        bdm_rub = bdm_rub.dropna(subset=["salary_val"])
        if len(bdm_rub) >= 2:
            print(f"  зарплата (RUB на руки): медиана={bdm_rub['salary_val'].median():,.0f}, "
                  f"среднее={bdm_rub['salary_val'].mean():,.0f}  (n={len(bdm_rub)})")
        print("  опыт:")
        for exp, cnt in bdm["experience"].fillna("не указан").value_counts().items():
            print(f"    • {exp:<28} {cnt}")
        print("  топ-15 hard skills:")
        c = Counter()
        for s in bdm["hard_skills"].dropna():
            if isinstance(s, str):
                for sk in s.split(","):
                    sk = sk.strip()
                    if sk:
                        c[sk] += 1
        for sk, cnt in c.most_common(15):
            print(f"    • {sk:<32} {cnt:>3}  ({cnt/len(bdm)*100:4.1f}%)")
        print("  топ-10 компаний:")
        for emp, cnt in bdm["employer_name"].fillna("—").value_counts().head(10).items():
            print(f"    • {emp:<40} {cnt}")

    # --- Сохранить сводный CSV ---
    summary_path = REPORTS_DIR / "sales_bdm_summary.csv"
    df[["vacancy_id", "vacancy_name", "role_group", "employer_name", "area",
        "experience", "schedule", "salary_from", "salary_to", "salary_currency",
        "hard_skills", "soft_skills", "tools", "vacancy_url"]].to_csv(
        summary_path, index=False, encoding="utf-8-sig")
    print(f"\n💾 Сводная таблица: {summary_path}")
    print(f"💾 Обработанный CSV: {PROCESSED_DIR / 'vacancies_processed.csv'}")
    print(f"{'='*70}\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--collect", action="store_true")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--schedule", default="remote")
    p.add_argument("--max", type=int, default=MAX_PER_KEYWORD)
    args = p.parse_args()

    df = None
    do_collect = args.collect or (not args.collect and not args.analyze)
    do_analyze = args.analyze or (not args.collect and not args.analyze)

    if do_collect:
        run_collect(schedule=args.schedule, max_per_keyword=args.max)
        df = run_process()
    if do_analyze:
        run_analyze(df)


if __name__ == "__main__":
    main()