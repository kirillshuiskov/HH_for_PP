#!/usr/bin/env python3
"""
HH.ru Analytics — Главная точка входа (ETL Pipeline).

Обрабатывает уже собранные данные:
1. Transform — обработка и извлечение навыков
2. Load — сохранение в SQLite
3. Analyze — формирование отчётов

Сбор вакансий вынесен в отдельный веб-сборщик (официальный API hh.ru
заблокирован). Собирай данные так:
    python -m src.web_collector                       # демо-прогон
    # либо из своего скрипта: HHWebCollector(...).collect([...])
Собранный сырой JSON ложится в data/raw/ и подхватывается обработкой ниже.

Использование:
    python main.py              # process -> load -> analyze
    python main.py --process    # только обработка
    python main.py --load       # только загрузка в БД
    python main.py --analyze    # только анализ
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Добавляем корень проекта в path для импортов
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.config import settings
from src.utils import get_logger, ensure_dir
from src.processor import VacancyProcessor
from src.storage import VacancyStorage
from src.analyzer import VacancyAnalyzer
from src.advanced_analyzer import AdvancedAnalytics

# Инициализируем логгер
logger = get_logger(__name__)


def run_processing() -> dict:
    """
    Этап Transform: обработка вакансий из data/raw/.

    Returns:
        Словарь со статистикой обработки
    """
    logger.info("=" * 60)
    logger.info("ЭТАП: Обработка данных (Transform)")
    logger.info("=" * 60)

    # Создаём процессор
    processor = VacancyProcessor()

    # Обрабатываем все JSON файлы
    df = processor.process_all(
        save_csv=True,
        save_parquet=True
    )

    if df.empty:
        logger.warning(
            "Нет данных для обработки. Сначала собери вакансии: "
            "python -m src.web_collector"
        )
        return {"processed": 0}

    # Получаем статистику по навыкам
    skills_stats = processor.get_skills_statistics(df)

    logger.info(f"✅ Обработка завершена")
    logger.info(f"   Обработано вакансий: {len(df)}")
    logger.info(f"   Уникальных навыков: {len(skills_stats.get('top_hard_skills', {})) + len(skills_stats.get('top_tools', {}))}")

    return {
        "processed": len(df),
        "skills_stats": skills_stats
    }


def run_loading() -> dict:
    """
    Этап Load: загрузка обработанных данных в SQLite.

    Returns:
        Словарь со статистикой загрузки
    """
    logger.info("=" * 60)
    logger.info("ЭТАП: Загрузка в БД (Load)")
    logger.info("=" * 60)

    # Проверяем наличие CSV файла
    csv_path = settings.processed_data_dir / "vacancies_processed.csv"

    if not csv_path.exists():
        logger.warning("CSV файл не найден. Сначала запустите обработку (--process)")
        return {"loaded": 0}

    # Загружаем CSV
    import pandas as pd
    df = pd.read_csv(csv_path)

    # Создаём хранилище
    storage = VacancyStorage()

    try:
        # Сохраняем в БД
        count = storage.save_dataframe(df)

        logger.info(f"✅ Загрузка завершена")
        logger.info(f"   Сохранено записей: {count}")

        return {"loaded": count}

    except Exception as e:
        logger.error(f"Ошибка при загрузке: {type(e).__name__} - {e}")
        raise
    finally:
        storage.close()


def run_analysis() -> dict:
    """
    Этап Analyze: анализ и отчёты.

    Returns:
        Словарь с путями к отчётам
    """
    logger.info("=" * 60)
    logger.info("ЭТАП: Анализ и отчёты (Analyze)")
    logger.info("=" * 60)

    # Проверяем наличие CSV файла
    csv_path = settings.processed_data_dir / "vacancies_processed.csv"

    if not csv_path.exists():
        logger.warning("CSV файл не найден. Сначала запустите обработку (--process)")
        return {}

    # Загружаем данные
    import pandas as pd
    df = pd.read_csv(csv_path)

    # Создаём анализатор
    analyzer = VacancyAnalyzer(df)

    # Выводим сводку в консоль
    analyzer.print_summary()

    # Генерируем Excel-отчёт
    report_path = analyzer.generate_excel_report()
    logger.info(f"✅ Excel-отчёт: {report_path}")

    # Экспорт в CSV
    csv_stats_path = analyzer.export_to_csv()
    logger.info(f"✅ CSV-статистика: {csv_stats_path}")

    # === Расширенная аналитика ===
    logger.info("-" * 60)
    logger.info("📊 Расширенная аналитика")
    logger.info("-" * 60)

    advanced_analytics = AdvancedAnalytics(df)

    # Выводим расширенную сводку
    advanced_analytics.print_advanced_summary()

    # Генерируем детальный отчёт
    detailed_report_path = advanced_analytics.generate_detailed_excel_report()
    logger.info(f"✅ Детальный отчёт: {detailed_report_path}")

    return {
        "excel_report": str(report_path),
        "csv_stats": str(csv_stats_path),
        "advanced_report": str(detailed_report_path)
    }


def run_full_pipeline() -> dict:
    """
    Запуск пайплайна обработки: Transform -> Load -> Analyze.

    Сбор данных выполняется отдельно (src.web_collector) и должен быть
    завершён до запуска — сырой JSON берётся из data/raw/.

    Returns:
        Словарь с результатами всех этапов
    """
    logger.info("\n" + "=" * 60)
    logger.info("🚀 ЗАПУСК ПАЙПЛАЙНА ОБРАБОТКИ")
    logger.info("=" * 60)
    logger.info(f"Время запуска: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    start_time = datetime.now()

    results = {}

    # Transform
    results["processing"] = run_processing()

    # Load
    results["loading"] = run_loading()

    # Analyze
    results["analysis"] = run_analysis()

    # Итоговая статистика
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    logger.info("\n" + "=" * 60)
    logger.info("📊 ИТОГОВАЯ СТАТИСТИКА")
    logger.info("=" * 60)
    logger.info(f"Обработано вакансий: {results['processing'].get('processed', 0)}")
    logger.info(f"Загружено в БД: {results['loading'].get('loaded', 0)}")
    logger.info(f"Время выполнения: {duration:.2f} сек ({duration/60:.2f} мин)")
    logger.info("=" * 60)

    return results


def main() -> int:
    """
    Главная функция с разбором аргументов командной строки.

    Returns:
        Код выхода (0 — успех, 1 — ошибка)
    """
    # Парсер аргументов
    parser = argparse.ArgumentParser(
        description="HH.ru Analytics — обработка и анализ собранных вакансий",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Сбор данных (отдельно, официальный API заблокирован):
  python -m src.web_collector         # веб-сборщик

Обработка и анализ:
  python main.py                      # process -> load -> analyze
  python main.py --process            # только обработка
  python main.py --load               # только загрузка в БД
  python main.py --analyze            # только анализ
  python main.py --process --analyze  # обработка и анализ
        """
    )

    # Режимы работы
    parser.add_argument(
        "--process",
        action="store_true",
        help="Только обработка данных (Transform)"
    )
    parser.add_argument(
        "--load",
        action="store_true",
        help="Только загрузка в БД (Load)"
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Только анализ и отчёты (Analyze)"
    )

    args = parser.parse_args()

    # Если ни один режим не указан — запускаем весь пайплайн обработки
    if not any([args.process, args.load, args.analyze]):
        logger.info("Режим не указан — запускаем полный пайплайн обработки")
        run_full_pipeline()
    else:
        if args.process:
            run_processing()

        if args.load:
            run_loading()

        if args.analyze:
            run_analysis()

    return 0


if __name__ == "__main__":
    """Точка входа скрипта."""
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Прервано пользователем")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"❌ Критическая ошибка: {e}")
        sys.exit(1)
