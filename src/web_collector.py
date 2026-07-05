"""
Веб-сборщик вакансий с hh.ru (обход блокировки официального API).

Официальный api.hh.ru отдаёт 403 для датацентр-IP, но веб-сайт hh.ru
работает. HH встраивает все данные страницы в JSON внутри тега
<template id="HH-Lux-InitialState">, поэтому парсить HTML-теги не нужно —
достаём готовую структуру.

Двухступенчатый сбор (как и оригинальный collector через API):
  1. Страница поиска hh.ru/search/vacancy — список вакансий с метаданными
     (id, название, зарплата, компания, регион, формат, дата). Без описания.
  2. Карточка hh.ru/vacancy/{id} (объект vacancyView) — полное описание
     и keySkills, нужные для извлечения навыков.

Собранные вакансии приводятся к формату официального API (salary, employer,
snippet, key_skills, experience, area...), чтобы весь остальной пайплайн
(processor → storage → analyzer) работал без изменений.

Этика/лимиты: браузерный User-Agent, задержки между запросами, скромные
объёмы. Это публичные объявления, сбор для личного исследования рынка.
При появлении капчи — притормаживаем, а не обходим.
"""

import json
import re
import time
import html as html_lib
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests

from src.config import settings
from src.utils import get_logger

logger = get_logger(__name__)

# Регекс для вытаскивания встроенного JSON-стейта из HTML
_STATE_RE = re.compile(
    r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S
)
# Регекс для грубой очистки HTML-тегов из описания
_TAG_RE = re.compile(r"<[^>]+>")

# Браузерный User-Agent — веб-сайт отдаёт данные, в отличие от API
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Маппинги кодов hh.ru в человекочитаемые значения (как в API)
_EXPERIENCE_MAP = {
    "noExperience": "Нет опыта",
    "between1And3": "От 1 года до 3 лет",
    "between3And6": "От 3 до 6 лет",
    "moreThan6": "Более 6 лет",
}
_EMPLOYMENT_MAP = {
    "FULL": "Полная занятость",
    "PART": "Частичная занятость",
    "PROJECT": "Проектная работа",
    "PROBATION": "Стажировка",
    "VOLUNTEER": "Волонтёрство",
}
_WORKFORMAT_MAP = {
    "REMOTE": "Удалённо",
    "HYBRID": "Гибрид",
    "ON_SITE": "На месте работодателя",
    "FIELD_WORK": "Разъездной",
}

# Подсказки для фильтра sales-ролей по названию вакансии (регистронезависимо)
SALES_ROLE_HINTS = [
    "продаж", "sales", "account", "аккаунт", "business development",
    "развитие бизнеса", "по работе с клиентами", "bdm", "kam",
    "клиентск", "менеджер по развитию",
]


class HHWebCollector:
    """
    Сборщик вакансий через веб-интерфейс hh.ru.

    Пример:
        >>> collector = HHWebCollector(schedule="remote")
        >>> vacancies = collector.collect(["sales manager"], max_per_keyword=60)
        >>> print(len(vacancies))
    """

    BASE = "https://hh.ru"

    def __init__(
        self,
        delay: float = 1.5,
        schedule: Optional[str] = "remote",
        area: Optional[int] = None,
        output_dir: Optional[Path] = None,
    ) -> None:
        """
        Args:
            delay: Задержка между запросами (сек). Не опускай ниже ~1.0.
            schedule: Фильтр графика: "remote" — только удалёнка. None — все.
            area: Регион hh.ru (например, 1 — Москва, 2 — СПб). None — вся Россия.
            output_dir: Куда сохранять сырой JSON. По умолчанию data/raw/.
        """
        self.delay = delay
        self.schedule = schedule
        self.area = area
        self.output_dir = output_dir or settings.raw_data_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        })

        self._seen_ids: set = set()
        logger.info(
            f"HHWebCollector инициализирован. schedule={schedule}, area={area}, delay={delay}s"
        )

    # ------------------------------------------------------------------ #
    # Низкоуровневые запросы
    # ------------------------------------------------------------------ #
    def _get_html(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """GET-запрос страницы. Возвращает HTML или None при ошибке."""
        url = f"{self.BASE}{path}"
        try:
            time.sleep(self.delay)
            resp = self.session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            logger.error(f"Сетевая ошибка {url}: {type(e).__name__} - {e}")
            return None

        if resp.status_code == 200:
            # Признак капчи/челленджа — не пытаемся обходить, сигналим наверх
            if "captcha" in resp.url or "/account/login" in resp.url:
                logger.warning(f"Похоже на капчу/редирект: {resp.url}. Останавливаюсь.")
                return None
            return resp.text

        logger.warning(f"HTTP {resp.status_code} для {url}")
        return None

    @staticmethod
    def _extract_state(html: str) -> Optional[Dict[str, Any]]:
        """Достаёт встроенный JSON-стейт HH-Lux-InitialState из HTML."""
        m = _STATE_RE.search(html)
        if not m:
            return None
        raw = m.group(1)
        if "&quot;" in raw[:200]:  # на случай html-экранирования
            raw = html_lib.unescape(raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"Не удалось распарсить JSON-стейт: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Стадия 1: поиск
    # ------------------------------------------------------------------ #
    def _search_page(
        self,
        text: str,
        page: int,
        per_page: int,
        employer_id: Optional[int] = None,
        apply_schedule: bool = True,
    ) -> tuple[List[Dict], int]:
        """
        Одна страница поиска. Возвращает (список метаданных вакансий, found).

        Args:
            text: Поисковый запрос (может быть пустым при фильтре по работодателю).
            employer_id: Если задан — только вакансии этого работодателя.
            apply_schedule: Применять ли фильтр self.schedule (для сбора по
                компании обычно хотим все форматы, поэтому можно отключить).
        """
        params: Dict[str, Any] = {
            "page": page,
            "items_on_page": per_page,
        }
        if text:
            params["text"] = text
        if employer_id is not None:
            params["employer_id"] = employer_id
        if self.schedule and apply_schedule:
            params["schedule"] = self.schedule
        if self.area is not None:
            params["area"] = self.area

        html = self._get_html("/search/vacancy", params)
        if not html:
            return [], 0

        state = self._extract_state(html)
        if not state:
            return [], 0

        vr = state.get("vacancySearchResult", {}) or {}
        vacancies = vr.get("vacancies", []) or []
        # hh.ru кладёт общее число результатов в totalResults
        found = vr.get("totalResults", 0) or 0
        return vacancies, found

    # ------------------------------------------------------------------ #
    # Стадия 2: карточка вакансии
    # ------------------------------------------------------------------ #
    def _fetch_vacancy(self, vacancy_id: str) -> Optional[Dict[str, Any]]:
        """Загружает карточку вакансии и возвращает объект vacancyView."""
        html = self._get_html(f"/vacancy/{vacancy_id}")
        if not html:
            return None
        state = self._extract_state(html)
        if not state:
            return None
        return state.get("vacancyView")

    # ------------------------------------------------------------------ #
    # Маппинг веб-данных в формат официального API
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_html(text: str) -> str:
        """Убирает HTML-теги и разэкранирует сущности из описания."""
        if not text:
            return ""
        text = html_lib.unescape(text)          # &lt;p&gt; -> <p>
        text = _TAG_RE.sub(" ", text)           # выкидываем теги
        text = html_lib.unescape(text)          # &nbsp; и т.п.
        return re.sub(r"\s+", " ", text).strip()

    def _to_api_format(self, view: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """
        Приводит карточку (vacancyView) + метаданные поиска к формату API,
        который понимает processor._process_single_vacancy.
        """
        vid = str(view.get("vacancyId") or meta.get("vacancyId"))

        # Зарплата: compensation -> salary
        comp = view.get("compensation") or meta.get("compensation") or {}
        currency = comp.get("currencyCode") or "RUB"
        if currency == "RUR":
            currency = "RUB"
        salary = None
        if comp.get("from") or comp.get("to"):
            salary = {
                "from": comp.get("from"),
                "to": comp.get("to"),
                "currency": currency,
                "gross": comp.get("gross", False),
            }

        # Работодатель: company -> employer
        company = view.get("company") or meta.get("company") or {}

        # keySkills -> key_skills [{"name": ...}]
        ks = (view.get("keySkills") or {}).get("keySkill", []) or []
        key_skills = [{"name": s} for s in ks]

        # Формат работы -> schedule.name
        # workFormats бывает двух форм: плоский список кодов (карточка)
        # ['REMOTE', ...] или вложенный (поиск) [{"workFormatsElement": [...]}]
        formats = view.get("workFormats") or meta.get("workFormats") or []
        fmt_codes: List[str] = []
        for f in formats:
            if isinstance(f, dict):
                fmt_codes.extend(f.get("workFormatsElement", []))
            elif isinstance(f, str):
                fmt_codes.append(f)
        schedule_name = ", ".join(_WORKFORMAT_MAP.get(c, c) for c in fmt_codes) or ""

        # Дата публикации из метаданных поиска
        pub = meta.get("publicationTime") or {}
        published_at = pub.get("$") if isinstance(pub, dict) else None

        area = view.get("area") or meta.get("area") or {}

        return {
            "id": vid,
            "name": view.get("name") or meta.get("name", ""),
            "published_at": published_at,
            "created_at": published_at,
            # Полное описание кладём в description — processor его читает
            "description": self._clean_html(view.get("description", "")),
            "snippet": {},
            "key_skills": key_skills,
            "salary": salary,
            "employer": {
                "name": company.get("name") or company.get("visibleName", ""),
                "id": company.get("id"),
                "url": "",
            },
            "alternate_url": f"{self.BASE}/vacancy/{vid}",
            "experience": {"name": _EXPERIENCE_MAP.get(view.get("workExperience"), "")},
            "employment": {"name": _EMPLOYMENT_MAP.get(view.get("employmentForm"), "")},
            "schedule": {"name": schedule_name},
            "area": {"name": area.get("name", "")},
        }

    # ------------------------------------------------------------------ #
    # Публичный сбор
    # ------------------------------------------------------------------ #
    def collect(
        self,
        keywords: List[str],
        max_per_keyword: int = 100,
        per_page: int = 50,
        fetch_details: bool = True,
        save: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Сбор вакансий по списку ключевых слов.

        Args:
            keywords: Поисковые запросы (роли).
            max_per_keyword: Максимум вакансий на запрос (hh отдаёт до ~2000).
            per_page: Вакансий на странице поиска (20/50/100).
            fetch_details: Догружать карточки (описание+навыки). Если False —
                только метаданные поиска (быстро, но без навыков).
            save: Сохранять ли сырой JSON в data/raw/.

        Returns:
            Список вакансий в формате API (готов для processor).
        """
        all_vacancies: List[Dict[str, Any]] = []

        for kw_idx, keyword in enumerate(keywords, 1):
            logger.info(f"[{kw_idx}/{len(keywords)}] Запрос: '{keyword}'")
            collected_for_kw = 0
            page = 0
            found_total = None

            while collected_for_kw < max_per_keyword:
                metas, found = self._search_page(keyword, page, per_page)
                if found_total is None:
                    found_total = found
                    logger.info(f"  Найдено всего по запросу: {found}")
                if not metas:
                    logger.info(f"  Страница {page}: пусто, завершаю запрос")
                    break

                for meta in metas:
                    if collected_for_kw >= max_per_keyword:
                        break
                    vid = str(meta.get("vacancyId"))
                    if not vid or vid == "None" or vid in self._seen_ids:
                        continue
                    self._seen_ids.add(vid)

                    if fetch_details:
                        view = self._fetch_vacancy(vid)
                        if not view:
                            logger.warning(f"  Не удалось загрузить карточку {vid}")
                            continue
                        record = self._to_api_format(view, meta)
                    else:
                        record = self._to_api_format(meta, meta)

                    all_vacancies.append(record)
                    collected_for_kw += 1

                logger.info(f"  Собрано по '{keyword}': {collected_for_kw}")

                # hh.ru отдаёт максимум ~2000 результатов (page*per_page < 2000)
                if (page + 1) * per_page >= min(found_total or 0, 2000):
                    break
                page += 1

            logger.info(f"Итого по '{keyword}': {collected_for_kw} вакансий")

        # Дедупликация на всякий случай (по id)
        unique = {v["id"]: v for v in all_vacancies}
        result = list(unique.values())
        logger.info(f"Всего собрано уникальных вакансий: {len(result)}")

        if save and result:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.output_dir / f"all_vacancies_{timestamp}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            logger.info(f"Сохранено: {path}")

        return result

    # ------------------------------------------------------------------ #
    # Режим «по компании»
    # ------------------------------------------------------------------ #
    def resolve_employer(self, name: str, max_candidates: int = 8) -> List[Dict[str, Any]]:
        """
        Находит employer_id по названию компании через поиск вакансий.

        Возвращает список кандидатов [{id, name, matched_in_page}],
        отсортированный по релевантности (точное совпадение названия → частота).
        Если пусто — работодатель не найден.
        """
        from collections import Counter

        metas, _ = self._search_page(name, page=0, per_page=100, apply_schedule=False)
        counter: Counter = Counter()
        names: Dict[Any, str] = {}
        for v in metas:
            c = v.get("company") or {}
            cid = c.get("id")
            if cid is None:
                continue
            counter[cid] += 1
            names[cid] = c.get("name") or c.get("visibleName") or ""

        q = name.lower().strip()

        def score(cid: Any) -> tuple:
            cname = (names.get(cid) or "").lower()
            return (cname == q, cname.startswith(q), q in cname, counter[cid])

        ordered = sorted(counter, key=score, reverse=True)[:max_candidates]
        return [
            {"id": cid, "name": names[cid], "matched_in_page": counter[cid]}
            for cid in ordered
        ]

    def collect_by_company(
        self,
        company: Any,
        role_filter: Optional[List[str]] = None,
        fetch_details: bool = True,
        apply_schedule: bool = False,
        max_vacancies: int = 200,
        per_page: int = 100,
        save: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Сбор открытых вакансий конкретного работодателя.

        Args:
            company: Название компании (str) или готовый employer_id (int).
            role_filter: Список подстрок; оставляем вакансии, чьё название содержит
                любую из них. Напр. SALES_ROLE_HINTS — только продажи. None — все.
            fetch_details: Догружать карточки (описание + keySkills).
            apply_schedule: Применять ли фильтр удалёнки. По умолчанию False —
                у одной компании обычно хотим видеть все форматы.
            max_vacancies: Верхний предел на всякий случай.
            save: Сохранять ли результат в data/raw/company_*.json.

        Returns:
            Список вакансий в формате API (готов для processor).
        """
        # 1) Резолвим работодателя
        if isinstance(company, int):
            employer_id = company
            company_name = str(company)
        else:
            candidates = self.resolve_employer(company)
            if not candidates:
                logger.warning(f"Работодатель '{company}' не найден в выдаче hh.ru")
                return []
            employer_id = candidates[0]["id"]
            company_name = candidates[0]["name"]
            if len(candidates) > 1:
                others = ", ".join(f"{c['name']} (id={c['id']})" for c in candidates[1:])
                logger.info(
                    f"Кандидатов несколько. Выбран: {company_name} (id={employer_id}). "
                    f"Другие: {others}"
                )
        logger.info(f"Сбор вакансий работодателя '{company_name}' (employer_id={employer_id})")

        # 2) Пагинация по вакансиям работодателя
        collected: List[Dict[str, Any]] = []
        page = 0
        found_total: Optional[int] = None
        while len(collected) < max_vacancies:
            metas, found = self._search_page(
                "", page, per_page, employer_id=employer_id, apply_schedule=apply_schedule
            )
            if found_total is None:
                found_total = found
                logger.info(f"  Всего открытых вакансий у работодателя: {found}")
            if not metas:
                break

            for meta in metas:
                if len(collected) >= max_vacancies:
                    break
                vname = meta.get("name", "") or ""
                if role_filter and not any(rf.lower() in vname.lower() for rf in role_filter):
                    continue
                vid = str(meta.get("vacancyId"))
                if not vid or vid == "None" or vid in self._seen_ids:
                    continue
                self._seen_ids.add(vid)

                if fetch_details:
                    view = self._fetch_vacancy(vid)
                    record = self._to_api_format(view, meta) if view else self._to_api_format(meta, meta)
                else:
                    record = self._to_api_format(meta, meta)
                collected.append(record)

            if (page + 1) * per_page >= min(found_total or 0, 2000):
                break
            page += 1

        logger.info(f"Собрано вакансий '{company_name}': {len(collected)}")

        if save and collected:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = re.sub(r"[^\w\-]+", "_", company_name)[:40].strip("_")
            path = self.output_dir / f"company_{safe}_{ts}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(collected, f, ensure_ascii=False, indent=2)
            logger.info(f"Сохранено: {path}")

        return collected

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "HHWebCollector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


if __name__ == "__main__":
    import logging
    logging.getLogger("src").setLevel(logging.INFO)

    print("=" * 60)
    print("Тест HHWebCollector: 'sales manager', remote, 5 вакансий")
    print("=" * 60)

    with HHWebCollector(schedule="remote", delay=1.5) as collector:
        vacs = collector.collect(["sales manager"], max_per_keyword=5, per_page=20, save=False)

    print(f"\nСобрано: {len(vacs)}")
    for v in vacs[:5]:
        sal = v["salary"]
        sal_s = f"{sal['from']}-{sal['to']} {sal['currency']}" if sal else "з/п не указана"
        print(f"\n• {v['name']} | {v['employer']['name']} | {v['area']['name']} | {sal_s}")
        print(f"  опыт: {v['experience']['name']} | формат: {v['schedule']['name']}")
        print(f"  keySkills: {[s['name'] for s in v['key_skills']]}")
        print(f"  описание: {v['description'][:120]}...")
