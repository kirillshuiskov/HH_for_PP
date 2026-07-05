"""
Модуль для обработки вакансий (ETL Transform).

Содержит класс VacancyProcessor, который:
- Загружает сырые данные из JSON
- Извлекает навыки из описаний вакансий
- Классифицирует навыки по категориям (Hard/Soft/Tools)
- Нормализует текст (lowercase, лемматизация)
- Сохраняет обработанные данные в CSV/Parquet

Важно: Качество извлечения навыков зависит от полноты словарей.
Периодически обновляйте config.yaml для улучшения результатов.
"""

import json
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set

import pandas as pd
import pymorphy3

from src.config import settings, config_loader
from src.utils import get_logger, ensure_dir

# Инициализируем логгер для модуля
logger = get_logger(__name__)


class VacancyProcessor:
    """
    Процессор для обработки вакансий.

    Реализует логику извлечения и классификации навыков:
    1. Загрузка сырых данных из JSON
    2. Извлечение навыков из описания и ключевых навыков
    3. Классификация по категориям (Hard Skills, Soft Skills, Tools)
    4. Нормализация (лемматизация через pymorphy3)
    5. Сохранение в CSV/Parquet

    Атрибуты:
        morph (pymorphy3.MorphAnalyzer): Лемматизатор для русского языка
        skills_dict (Dict): Словарь всех навыков по категориям
        output_dir (Path): Директория для сохранения

    Пример использования:
        >>> processor = VacancyProcessor()
        >>> df = processor.process_all()
        >>> print(f"Обработано вакансий: {len(df)}")
    """

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        min_skill_length: int = 2
    ) -> None:
        """
        Инициализация процессора.

        Args:
            output_dir: Директория для сохранения. Если None, берётся из конфига
            min_skill_length: Минимальная длина навыка (символов)
        """
        # Директория для обработанных данных
        self.output_dir = output_dir or settings.processed_data_dir
        ensure_dir(self.output_dir)

        # Минимальная длина навыка для фильтрации
        self.min_skill_length = min_skill_length or settings.processing.get("min_skill_length", 2)

        # Инициализируем лемматизатор для русского языка
        # pymorphy3 — современный форк pymorphy2 с улучшениями
        self.morph = pymorphy3.MorphAnalyzer()

        # Загружаем словари навыков из конфига
        self.skills_dict = config_loader.get_all_skills()

        # Кэш лемматизации (слово -> лемма), чтобы не гонять pymorphy повторно
        self._lemma_cache: Dict[str, str] = {}

        # Создаём обратный индекс: навык -> категория
        # Это позволяет быстро определять категорию найденного навыка
        self._skill_to_category: Dict[str, str] = {}
        # Лемматизированный индекс для навыков с кириллицей:
        # [(лемма-фраза, исходный навык, категория)] — ловит падежи
        self._skill_lemmas: List[Tuple[str, str, str]] = []
        self._build_skill_index()

        logger.info(
            f"VacancyProcessor инициализирован. "
            f"Навыков в словаре: {sum(len(v) for v in self.skills_dict.values())}"
        )

    def _build_skill_index(self) -> None:
        """
        Построение обратного индекса навыков.

        Создаёт словарь {навык: категория} для быстрого поиска.
        Все навыки приводятся к нижнему регистру для нечувствительного поиска.

        Note:
            Если навык встречается в нескольких категориях,
            используется последняя (приоритет Tools > Soft > Hard)
        """
        self._skill_to_category = {}
        self._skill_lemmas = []

        for category, skills in self.skills_dict.items():
            for skill in skills:
                # Нормализуем навык: lowercase + strip
                normalized = skill.lower().strip()
                if len(normalized) >= self.min_skill_length:
                    self._skill_to_category[normalized] = category

                    # Для навыков с кириллицей строим лемматизированную форму,
                    # чтобы ловить падежи ("холодные звонки" ~ "холодных звонков").
                    # Термины без кириллицы (c++, b2b, python) не трогаем —
                    # их надёжно ловит точный поиск, а лемматизация их ломает.
                    if re.search('[а-яё]', normalized):
                        lemma_phrase = self._lemmatize_phrase(normalized)
                        if lemma_phrase:
                            self._skill_lemmas.append((lemma_phrase, normalized, category))

        logger.debug(
            f"Построен индекс: {len(self._skill_to_category)} навыков, "
            f"{len(self._skill_lemmas)} лемматизированных"
        )

    def _normalize_text(self, text: str) -> str:
        """
        Нормализация текста.

        Args:
            text: Исходный текст

        Returns:
            Нормализованный текст (lowercase, без лишних пробелов)

        Note:
            Базовая нормализация. Лемматизация применяется отдельно.
        """
        if not text:
            return ""

        # Приводим к нижнему регистру
        text = text.lower()

        # Удаляем лишние пробелы
        text = re.sub(r'\s+', ' ', text).strip()

        return text

    def _lemmatize(self, word: str) -> str:
        """
        Лемматизация слова (приведение к начальной форме).

        Args:
            word: Слово для лемматизации

        Returns:
            Лемма (начальная форма слова)

        Пример:
            >>> processor._lemmatize("программистами")
            'программист'

        Note:
            Используется pymorphy3 для русского языка.
            Для английских слов возвращается как есть.
        """
        if not word:
            return ""

        cached = self._lemma_cache.get(word)
        if cached is not None:
            return cached

        # pymorphy3 лучше работает с кириллицей
        # Для латиницы просто возвращаем слово
        if not re.search('[а-яА-ЯёЁ]', word):
            result = word.lower()
        else:
            try:
                parses = self.morph.parse(word)
                # Нам важна не лингвистическая точность, а КОНСИСТЕНТНОСТЬ:
                # навык и текст должны давать одну лемму. Прилагательные pymorphy
                # разбирает нестабильно ("холодные" -> сущ. "холодное", но
                # "холодных" -> прил.). Поэтому если среди разборов есть
                # прилагательное/причастие — берём его и приводим к муж.р. ед.ч.
                adj = next(
                    (p for p in parses if p.tag.POS in ("ADJF", "ADJS", "PRTF", "PRTS")),
                    None,
                )
                if adj is not None:
                    canon = adj.inflect({"masc", "sing", "nomn"})
                    result = (canon.word if canon else adj.normal_form).lower()
                else:
                    result = parses[0].normal_form.lower()
            except Exception:
                result = word.lower()

        self._lemma_cache[word] = result
        return result

    def _lemmatize_phrase(self, text: str) -> str:
        """
        Лемматизация всех слов в строке с сохранением пунктуации/пробелов.

        Каждое слово заменяется на свою лемму; разделители остаются на месте.
        Используется и для навыков-фраз, и для текста вакансии — тогда падежные
        формы совпадают: "воронкой продаж" -> "воронка продажа".

        Пример:
            >>> processor._lemmatize_phrase("холодных звонков")
            'холодный звонок'
        """
        if not text:
            return ""
        return re.sub(r'\w+', lambda m: self._lemmatize(m.group()), text)

    def _extract_skills_from_text(
        self,
        text: str,
        include_category: bool = True
    ) -> Tuple[List[str], Dict[str, List[str]]]:
        """
        Извлечение навыков из текста.

        Args:
            text: Текст для анализа (описание вакансии)
            include_category: Включать ли информацию о категории

        Returns:
            Кортеж (all_skills, skills_by_category):
            - all_skills: плоский список всех найденных навыков
            - skills_by_category: словарь {категория: [навыки]}

        Note:
            Поиск нечувствителен к регистру.
            Один навык может быть найден только один раз.
        """
        if not text:
            return [], {}

        # Нормализуем текст
        normalized_text = self._normalize_text(text)

        found_skills: Set[str] = set()
        skills_by_category: Dict[str, Set[str]] = {
            "hard_skills": set(),
            "soft_skills": set(),
            "tools": set()
        }

        # Проход 1 — точное совпадение по исходному словарю.
        # Ловит термины с символами/латиницей (c++, c#, b2b, python).
        for skill, category in self._skill_to_category.items():
            # Используем regex для поиска целых слов
            # \b — граница слова, чтобы не находить "python" в "cpython"
            pattern = r'\b' + re.escape(skill) + r'\b'

            if re.search(pattern, normalized_text, re.IGNORECASE):
                found_skills.add(skill)
                skills_by_category[category].add(skill)

        # Проход 2 — лемматизированное совпадение (падежи).
        # Текст приводим к леммам и ищем в нём лемма-формы навыков.
        # Найденный навык репортим в исходной словарной форме.
        lemmatized_text = self._lemmatize_phrase(normalized_text)
        for lemma_phrase, original, category in self._skill_lemmas:
            if original in found_skills:
                continue  # уже найден точным проходом
            pattern = r'\b' + re.escape(lemma_phrase) + r'\b'
            if re.search(pattern, lemmatized_text):
                found_skills.add(original)
                skills_by_category[category].add(original)

        # Конвертируем множества в списки
        skills_by_category = {
            k: sorted(list(v)) for k, v in skills_by_category.items()
        }

        logger.debug(f"Найдено навыков: {len(found_skills)}")

        return sorted(list(found_skills)), skills_by_category

    def _extract_salary(self, vacancy: Dict[str, Any]) -> Dict[str, Any]:
        """
        Извлечение информации о зарплате.

        Args:
            vacancy: Словарь с данными вакансии

        Returns:
            Словарь с полями:
            - salary_from: от
            - salary_to: до
            - salary_currency: валюта
            - salary_gross: gross (до вычета налогов)
        """
        salary = vacancy.get("salary", {}) or {}

        return {
            "salary_from": salary.get("from"),
            "salary_to": salary.get("to"),
            "salary_currency": salary.get("currency", "RUB"),
            "salary_gross": salary.get("gross", False)
        }

    def _extract_employer(self, vacancy: Dict[str, Any]) -> Dict[str, Any]:
        """
        Извлечение информации о работодателе.

        Args:
            vacancy: Словарь с данными вакансии

        Returns:
            Словарь с полями:
            - employer_name: название компании
            - employer_id: ID работодателя
            - employer_url: ссылка на hh.ru
        """
        employer = vacancy.get("employer", {}) or {}

        return {
            "employer_name": employer.get("name", ""),
            "employer_id": employer.get("id"),
            "employer_url": employer.get("url", "")
        }

    def _process_single_vacancy(self, vacancy: Dict[str, Any]) -> Dict[str, Any]:
        """
        Обработка одной вакансии.

        Args:
            vacancy: Словарь с данными вакансии из API

        Returns:
            Словарь с обработанными данными

        Note:
            Извлекает:
            - Навыки (hard, soft, tools)
            - Зарплату
            - Работодателя
            - Метаданные
        """
        # Используем snippet из API (description не возвращается в поиске)
        snippet = vacancy.get("snippet", {}) or {}
        requirement = snippet.get("requirement", "") or ""
        responsibility = snippet.get("responsibility", "") or ""
        
        # Также проверяем поле description (если вдруг есть)
        description = vacancy.get("description", "") or ""
        
        # Ключевые навыки из API
        key_skills = vacancy.get("key_skills", []) or []
        key_skills_text = " ".join([s.get("name", "") for s in key_skills])

        # Полный текст для анализа навыков
        full_text = f"{description} {requirement} {responsibility} {key_skills_text}"

        # Извлекаем навыки
        all_skills, skills_by_category = self._extract_skills_from_text(full_text)

        # Извлекаем зарплату
        salary_info = self._extract_salary(vacancy)

        # Извлекаем работодателя
        employer_info = self._extract_employer(vacancy)

        # Формируем итоговый словарь
        processed = {
            # ID и метаданные
            "vacancy_id": vacancy.get("id"),
            "vacancy_name": vacancy.get("name", ""),
            "published_at": vacancy.get("published_at"),
            "applied_at": vacancy.get("created_at"),

            # Навыки
            "all_skills": ", ".join(all_skills),
            "hard_skills": ", ".join(skills_by_category.get("hard_skills", [])),
            "soft_skills": ", ".join(skills_by_category.get("soft_skills", [])),
            "tools": ", ".join(skills_by_category.get("tools", [])),

            # Количество навыков для аналитики
            "skill_count": len(all_skills),
            "hard_skill_count": len(skills_by_category.get("hard_skills", [])),
            "soft_skill_count": len(skills_by_category.get("soft_skills", [])),
            "tools_count": len(skills_by_category.get("tools", [])),

            # Зарплата
            **salary_info,

            # Работодатель
            **employer_info,

            # Ссылки
            "vacancy_url": vacancy.get("alternate_url", ""),

            # Опыт работы и график
            "experience": vacancy.get("experience", {}).get("name", ""),
            "employment": vacancy.get("employment", {}).get("name", ""),
            "schedule": vacancy.get("schedule", {}).get("name", ""),

            # Регион
            "area": vacancy.get("area", {}).get("name", ""),
        }

        return processed

    def process_json_file(self, filepath: Path) -> pd.DataFrame:
        """
        Обработка JSON файла с вакансиями.

        Args:
            filepath: Путь к JSON файлу

        Returns:
            DataFrame с обработанными данными

        Note:
            Каждая строка — одна вакансия со всеми извлечёнными полями.
        """
        logger.info(f"Обработка файла: {filepath}")

        # Загружаем JSON
        with open(filepath, "r", encoding="utf-8") as f:
            vacancies = json.load(f)

        if not isinstance(vacancies, list):
            logger.warning(f"Ожидался список вакансий, получено: {type(vacancies)}")
            return pd.DataFrame()

        logger.info(f"Загружено {len(vacancies)} вакансий")

        # Обрабатываем каждую вакансию
        processed_data = []
        for i, vacancy in enumerate(vacancies, 1):
            try:
                processed = self._process_single_vacancy(vacancy)
                processed_data.append(processed)

                # Логгируем прогресс каждые 50 вакансий
                if i % 50 == 0:
                    logger.debug(f"Обработано {i}/{len(vacancies)}")

            except Exception as e:
                logger.warning(f"Ошибка обработки вакансии {vacancy.get('id')}: {e}")
                continue

        # Создаём DataFrame
        df = pd.DataFrame(processed_data)

        logger.info(f"Создан DataFrame: {len(df)} строк, {len(df.columns)} колонок")

        return df

    def process_all(
        self,
        input_dir: Optional[Path] = None,
        save_csv: bool = True,
        save_parquet: bool = True
    ) -> pd.DataFrame:
        """
        Обработка всех JSON файлов в директории.

        Args:
            input_dir: Директория с JSON файлами. Если None, берётся data/raw/
            save_csv: Сохранять ли в CSV
            save_parquet: Сохранять ли в Parquet

        Returns:
            Объединённый DataFrame со всеми вакансиями

        Note:
            Обрабатывает все файлы вида *_vacancies_*.json
        """
        input_dir = input_dir or settings.raw_data_dir

        if not input_dir.exists():
            logger.warning(f"Директория не найдена: {input_dir}")
            return pd.DataFrame()

        # Находим все JSON файлы с вакансиями
        # Исключаем временные файлы и уже обработанные
        json_files = list(input_dir.glob("*.json"))
        json_files = [f for f in json_files if "all_vacancies" in f.name]

        if not json_files:
            logger.warning("JSON файлы с вакансиями не найдены")
            return pd.DataFrame()

        logger.info(f"Найдено файлов для обработки: {len(json_files)}")

        # Обрабатываем каждый файл
        all_dfs = []
        for filepath in json_files:
            df = self.process_json_file(filepath)
            if not df.empty:
                all_dfs.append(df)

        if not all_dfs:
            logger.warning("Нет данных для обработки")
            return pd.DataFrame()

        # Объединяем все DataFrame
        combined_df = pd.concat(all_dfs, ignore_index=True)

        # Удаляем полные дубликаты
        before_dedup = len(combined_df)
        combined_df = combined_df.drop_duplicates(subset=["vacancy_id"])
        after_dedup = len(combined_df)
        logger.info(f"Удалено дубликатов: {before_dedup - after_dedup}")

        # Сохраняем результаты
        if save_csv:
            csv_path = self.output_dir / "vacancies_processed.csv"
            combined_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            logger.info(f"Сохранено CSV: {csv_path}")

        if save_parquet:
            parquet_path = self.output_dir / "vacancies_processed.parquet"
            combined_df.to_parquet(parquet_path, index=False)
            logger.info(f"Сохранено Parquet: {parquet_path}")

        return combined_df

    def get_skills_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Подсчёт статистики по навыкам.

        Args:
            df: DataFrame с обработанными вакансиями

        Returns:
            Словарь со статистикой:
            - top_hard_skills: топ Hard Skills
            - top_soft_skills: топ Soft Skills
            - top_tools: топ Tools
            - avg_skills_per_vacancy: среднее количество навыков
        """
        if df.empty:
            return {}

        # Функция для подсчёта частоты навыков
        def count_skills(column: pd.Series) -> Dict[str, int]:
            skill_counts: Dict[str, int] = {}
            for skills_str in column.dropna():
                if isinstance(skills_str, str):
                    skills = [s.strip() for s in skills_str.split(",")]
                    for skill in skills:
                        if skill:
                            skill_counts[skill] = skill_counts.get(skill, 0) + 1
            return skill_counts

        # Считаем частоту по категориям
        hard_skills_counts = count_skills(df["hard_skills"])
        soft_skills_counts = count_skills(df["soft_skills"])
        tools_counts = count_skills(df["tools"])

        # Сортируем по частоте
        top_n = config_loader.reporting.get("top_n_skills", 30)

        stats = {
            "top_hard_skills": dict(sorted(
                hard_skills_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )[:top_n]),
            "top_soft_skills": dict(sorted(
                soft_skills_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )[:top_n]),
            "top_tools": dict(sorted(
                tools_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )[:top_n]),
            "avg_skills_per_vacancy": df["skill_count"].mean(),
            "total_vacancies": len(df)
        }

        return stats


# =============================================================================
# Блок для тестирования модуля
# =============================================================================

if __name__ == "__main__":
    """
    Пример использования VacancyProcessor для тестирования.

    Перед запуском убедитесь:
    1. Есть обработанные JSON файлы в data/raw/
    2. Установлен pymorphy3

    Запуск: python -m src.processor
    """

    import logging
    logging.getLogger("src").setLevel(logging.INFO)

    print("=" * 60)
    print("Тестирование VacancyProcessor")
    print("=" * 60)

    # Создаём процессор
    processor = VacancyProcessor()

    try:
        # Обрабатываем все файлы
        print("\n⚙️  Обработка вакансий...")
        print("-" * 60)

        df = processor.process_all(
            save_csv=True,
            save_parquet=True
        )

        if df.empty:
            print("❌ Нет данных для обработки")
        else:
            # Выводим статистику
            print("\n" + "=" * 60)
            print("📊 Статистика обработки")
            print("=" * 60)
            print(f"✅ Обработано вакансий: {len(df)}")
            print(f"📋 Колонок: {len(df.columns)}")

            # Статистика по навыкам
            stats = processor.get_skills_statistics(df)

            print("\n📈 Топ Hard Skills:")
            for skill, count in list(stats.get("top_hard_skills", {}).items())[:10]:
                print(f"  • {skill}: {count}")

            print("\n📈 Топ Tools:")
            for skill, count in list(stats.get("top_tools", {}).items())[:10]:
                print(f"  • {skill}: {count}")

            print(f"\n📊 Среднее навыков на вакансию: {stats.get('avg_skills_per_vacancy', 0):.2f}")

            # Информация о файлах
            print("\n💾 Сохранённые файлы:")
            print(f"  📄 {settings.processed_data_dir}/vacancies_processed.csv")
            print(f"  📄 {settings.processed_data_dir}/vacancies_processed.parquet")

    except KeyboardInterrupt:
        print("\n\n⚠️  Обработка прервана пользователем")
    except Exception as e:
        print(f"\n❌ Ошибка: {type(e).__name__} - {e}")
        logger.exception("Детальная информация об ошибке")
    finally:
        print("\n" + "=" * 60)
        print("Тестирование завершено!")
        print("=" * 60)
