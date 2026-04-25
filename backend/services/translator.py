import os
import asyncio
import json as _json
import logging
import httpx
import re
from enum import Enum
from typing import Optional, Tuple
from openai import AsyncOpenAI, APITimeoutError, APIConnectionError, APIStatusError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.models.models import Settings, Terminology, Chapter, Segment

logger = logging.getLogger(__name__)

# ==== Module-level config ====
TRANSLATION_TEMPERATURE = float(os.getenv("TRANSLATION_TEMPERATURE", "0.25"))
TRANSLATION_MAX_TOKENS = int(os.getenv("TRANSLATION_MAX_TOKENS", "4096"))
TRANSLATION_BATCH_SIZE = max(0, int(os.getenv("TRANSLATION_BATCH_SIZE", "4")))
TRANSLATION_BATCH_MAX_CHARS = int(os.getenv("TRANSLATION_BATCH_MAX_CHARS", "2400"))

SOURCE_ALPHA_PATTERN = re.compile(r"[A-Za-z]")
EN_WORD_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z\-']{2,}\b")
NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)*")
YEAR_PATTERN = re.compile(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b")
PREAMBLE_RE = re.compile(
    r"^\s*(?:here\s+is|sure[,!.]?|of course[,!.]?|好的|以下是|翻译如下)[^\n]{0,30}[:：]\s*\n+",
    re.IGNORECASE,
)
LABEL_RE = re.compile(r"^\s*(?:translation|译文|中文)\s*[:：]\s*", re.IGNORECASE)

# ==== Genre-aware roles ====
GENRE_ROLES = {
    "novel": "literary novel translator",
    "academic": "academic paper translator",
    "technical": "technical documentation translator",
    "general": "professional translator",
}
GENRE_TEMPERATURE = {"novel": 0.4, "academic": 0.2, "technical": 0.2, "general": 0.3}


def resolve_genre(project) -> Tuple[str, float]:
    g = (getattr(project, "genre", None) or "general").lower()
    return GENRE_ROLES.get(g, GENRE_ROLES["general"]), GENRE_TEMPERATURE.get(g, 0.3)


class APIProvider(Enum):
    OPENAI = "openai"
    SILICONFLOW = "siliconflow"
    GOOGLE = "google"
    OLLAMA = "ollama"
    CUSTOM = "custom"


class TranslatorConfig:
    def __init__(
        self,
        provider: APIProvider,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        extra_params: Optional[dict] = None,
    ):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.extra_params = extra_params or {}


PROVIDER_CONFIGS = {
    APIProvider.OPENAI: {
        "base_url": "https://api.openai.com/v1",
        "default_models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
        "timeout": 60.0, "api_format": "openai",
    },
    APIProvider.SILICONFLOW: {
        "base_url": "https://api.siliconflow.cn/v1",
        "default_models": ["deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen2.5-72B-Instruct", "THUDM/glm-4-9b-chat"],
        "timeout": 90.0, "api_format": "openai",
    },
    APIProvider.GOOGLE: {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_models": ["gemini-2.0-flash", "gemini-2.5-pro", "gemini-1.5-pro"],
        "timeout": 120.0, "api_format": "google",
    },
    APIProvider.OLLAMA: {
        "base_url": "http://localhost:11434/v1",
        "default_models": ["llama3", "qwen2.5", "deepseek-r1"],
        "timeout": 120.0, "api_format": "openai",
    },
    APIProvider.CUSTOM: {
        "base_url": "", "default_models": [], "timeout": 90.0, "api_format": "openai",
    },
}

# ==== Client cache (修复：键含 provider/api_key/base_url/timeout) ====
_openai_clients: dict[Tuple[str, str, str, float], AsyncOpenAI] = {}
_google_clients: dict[Tuple[str, str, float], httpx.AsyncClient] = {}


def detect_provider(base_url: str) -> APIProvider:
    if "siliconflow.cn" in base_url or "siliconflow.com" in base_url:
        return APIProvider.SILICONFLOW
    if "googleapis.com" in base_url:
        return APIProvider.GOOGLE
    if "localhost:11434" in base_url or "127.0.0.1:11434" in base_url:
        return APIProvider.OLLAMA
    if "openai.com" in base_url:
        return APIProvider.OPENAI
    return APIProvider.CUSTOM


async def get_translator_config(db: AsyncSession) -> TranslatorConfig:
    settings = (await db.execute(select(Settings).where(Settings.id == "default"))).scalar_one_or_none()
    if not settings or not settings.openai_api_key:
        raise ValueError("API Key is not configured. Please set it in the settings page.")
    provider = detect_provider(settings.openai_base_url)
    pc = PROVIDER_CONFIGS.get(provider, PROVIDER_CONFIGS[APIProvider.CUSTOM])
    return TranslatorConfig(
        provider=provider,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.model_name,
        timeout=pc["timeout"],
    )


def get_openai_client(config: TranslatorConfig) -> AsyncOpenAI:
    key = (config.provider.value, config.api_key or "", config.base_url or "", float(config.timeout or 60))
    cli = _openai_clients.get(key)
    if cli is None:
        cli = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url or None,
            timeout=config.timeout,
        )
        _openai_clients[key] = cli
    return cli


def get_google_client(config: TranslatorConfig) -> httpx.AsyncClient:
    key = (config.api_key or "", config.base_url or "", float(config.timeout or 60))
    cli = _google_clients.get(key)
    if cli is None:
        cli = httpx.AsyncClient(timeout=config.timeout)
        _google_clients[key] = cli
    return cli


async def get_confirmed_term_dict(project_id: str, db: AsyncSession) -> dict[str, str]:
    stmt = select(Terminology).where(Terminology.project_id == project_id, Terminology.is_confirmed == True)
    result = await db.execute(stmt)
    terms = result.scalars().all()
    return {term.original_term: term.translated_term for term in terms if term.translated_term}


def build_quality_term_dict(term_dict: dict[str, str]) -> dict[str, str]:
    expanded: dict[str, str] = {}
    for source_term, target_term in term_dict.items():
        source = (source_term or "").strip()
        target = (target_term or "").strip()
        if not source or not target:
            continue
        variants = {source}
        if SOURCE_ALPHA_PATTERN.search(source):
            variants.add(source.lower())
            variants.add(source.title())
        if len(source) >= 3:
            variants.add(f"{source}'s")
            variants.add(f"{source.lower()}'s")
            variants.add(f"{source.title()}'s")
        if " " not in source and source[-1:].isalpha():
            if not source.lower().endswith("s"):
                variants.add(f"{source}s")
                variants.add(f"{source.lower()}s")
            if source.lower().endswith(("x", "ch", "sh")):
                variants.add(f"{source}es")
                variants.add(f"{source.lower()}es")
        for variant in variants:
            key = variant.strip()
            if key and key not in expanded:
                expanded[key] = target
    return expanded


def pick_relevant_terms(text: str, term_dict: dict[str, str]) -> dict[str, str]:
    """只把出现在当前段里的术语注入 prompt，避免长 glossary 拖垮 latency / token。"""
    if not term_dict:
        return {}
    lower_text = (text or "").lower()
    picked: dict[str, str] = {}
    for source, target in term_dict.items():
        if not source or not target:
            continue
        if source.lower() in lower_text:
            picked[source] = target
    return picked


def cleanup_translated_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned.strip("`").strip()
    cleaned = PREAMBLE_RE.sub("", cleaned)
    cleaned = LABEL_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _contains_excessive_untranslated_english(original_text: str, translated_text: str) -> bool:
    source_alpha_count = len(SOURCE_ALPHA_PATTERN.findall(original_text or ""))
    if source_alpha_count < 20:
        return False
    translated_words = EN_WORD_PATTERN.findall(translated_text or "")
    if not translated_words:
        return False
    translated_alpha_count = sum(len(word) for word in translated_words)
    return translated_alpha_count / max(1, source_alpha_count) > 0.45


def qa_diagnose(original: str, translated: str, term_dict: dict[str, str]) -> list[str]:
    """返回当前译文的具体质检问题列表，空列表代表通过。"""
    issues: list[str] = []
    if not translated or not translated.strip():
        return ["empty_translation"]

    o_len = len(original.strip())
    t_len = len(translated.strip())
    if o_len > 30 and t_len < max(6, int(o_len * 0.2)):
        issues.append(f"too_short: original={o_len}, translated={t_len}")

    if _contains_excessive_untranslated_english(original, translated):
        issues.append("excessive_untranslated_english")

    missing_terms = []
    for s, t in term_dict.items():
        if not s or not t:
            continue
        if s.lower() in original.lower() and t not in translated:
            missing_terms.append(f"{s}->{t}")
    if missing_terms:
        issues.append("missing_terms: " + ", ".join(missing_terms[:5]))

    for n in NUMBER_PATTERN.findall(original):
        if len(n) >= 2 and n not in translated:
            issues.append(f"number_missing: {n}")
            break

    for y in YEAR_PATTERN.findall(original):
        if y not in translated:
            issues.append(f"year_missing: {y}")
            break

    return issues


def basic_quality_check(original_text: str, translated_text: str, term_dict: dict[str, str]) -> bool:
    return not qa_diagnose(original_text, translated_text, term_dict)


async def translate_with_openai_format(
    config: TranslatorConfig,
    system_prompt: str,
    user_text: str,
    max_retries: int = 3,
    max_tokens: Optional[int] = None,
    response_format: Optional[dict] = None,
):
    """OpenAI-compat 调用，支持动态 max_tokens 和 response_format。"""
    client = get_openai_client(config)
    effective_max = int(max_tokens) if max_tokens else TRANSLATION_MAX_TOKENS
    for attempt in range(max_retries):
        try:
            kwargs = dict(
                model=config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=TRANSLATION_TEMPERATURE,
                max_tokens=effective_max,
                **config.extra_params,
            )
            if response_format:
                kwargs["response_format"] = response_format
            response = await client.chat.completions.create(**kwargs)
            if not response.choices or not response.choices[0].message.content:
                logger.warning(f"Empty response on attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None
            return response.choices[0].message.content.strip()
        except APITimeoutError as e:
            logger.error(f"API Timeout (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt * 2)
            else:
                return None
        except APIConnectionError as e:
            logger.error(f"API Connection Error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt * 2)
            else:
                return None
        except APIStatusError as e:
            logger.error(f"API Status Error (attempt {attempt + 1}/{max_retries}): {e.status_code} - {e.message}")
            if 400 <= e.status_code < 500 and e.status_code != 429:
                return None
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return None
        except Exception as e:
            logger.error(f"API Error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return None
    return None


async def translate_with_google_api(
    config: TranslatorConfig,
    system_prompt: str,
    user_text: str,
    max_retries: int = 3,
    max_tokens: Optional[int] = None,
):
    url = f"{config.base_url}/models/{config.model}:generateContent?key={config.api_key}"
    effective_max = int(max_tokens) if max_tokens else TRANSLATION_MAX_TOKENS
    for attempt in range(max_retries):
        try:
            client = get_google_client(config)
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_text}"}]}],
                    "generationConfig": {
                        "temperature": TRANSLATION_TEMPERATURE,
                        "maxOutputTokens": effective_max,
                    },
                },
            )
            if response.status_code != 200:
                err = response.json().get("error", {}).get("message", "Unknown error")
                logger.error(f"Google API Error (attempt {attempt + 1}): {response.status_code} - {err}")
                if response.status_code == 429 and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt * 2); continue
                if 400 <= response.status_code < 500:
                    return None
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt); continue
                return None

            result = response.json()
            candidates = result.get("candidates") or []
            if candidates:
                parts = (candidates[0].get("content") or {}).get("parts") or []
                if parts and "text" in parts[0]:
                    return parts[0]["text"].strip()

            logger.warning(f"Empty response from Google API on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt); continue
            return None
        except httpx.TimeoutException as e:
            logger.error(f"Google API Timeout (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt * 2)
            else:
                return None
        except httpx.ConnectError as e:
            logger.error(f"Google API Connection Error (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt * 2)
            else:
                return None
        except Exception as e:
            logger.error(f"Google API Error (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return None
    return None


async def sample_few_shots(project_id: str, db: AsyncSession, n: int = 2) -> str:
    """从项目里已完成的 20-200 字段落采样，提升风格一致性。"""
    try:
        stmt = (
            select(Segment.original_text, Segment.translated_text)
            .join(Chapter, Segment.chapter_id == Chapter.id)
            .where(Chapter.project_id == project_id, Segment.status == "completed")
            .limit(50)
        )
        rows = (await db.execute(stmt)).all()
        candidates = [(o, t) for o, t in rows if o and t and 20 <= len(o) <= 200]
        picks = candidates[:n]
        if not picks:
            return ""
        return "\n\n".join(f"EN: {o}\nZH: {t}" for o, t in picks)
    except Exception:
        return ""


async def translate_text_with_stage(
    text: str,
    project_id: str,
    db: AsyncSession,
    context: str = "",
    stage: str = "one_pass",
    draft_text: str = "",
    max_retries: int = 3,
    translator_config: Optional[TranslatorConfig] = None,
    term_dict: Optional[dict[str, str]] = None,
    genre_role: str = "professional translator",
    qa_issues: Optional[list[str]] = None,
):
    """默认 one_pass（一次直出 = draft+polish 合一）；质检不过才走 repair。"""
    config = translator_config or await get_translator_config(db)
    full_terms = term_dict if term_dict is not None else await get_confirmed_term_dict(project_id, db)
    relevant_terms = pick_relevant_terms(text, full_terms)
    if relevant_terms:
        term_list = "\n".join(f"- {k} -> {v}" for k, v in relevant_terms.items())
        term_prompt = f"Glossary for this segment (case-insensitive):\n{term_list}\n"
    else:
        term_prompt = ""

    general_rules = (
        "Rules:\n"
        "1) Preserve every sentence, number, unit, date, name; do not omit or paraphrase away facts.\n"
        "2) Keep paragraph boundaries and line breaks when present.\n"
        "3) Use natural Chinese punctuation and word order; avoid literal word-by-word translation.\n"
        "4) Do not add explanations, notes, or markdown fences.\n"
        "5) Strictly follow the glossary above for any term it covers."
    )

    if stage == "repair":
        issue_hint = ""
        if qa_issues:
            issue_hint = "\nKnown issues to fix:\n- " + "\n- ".join(qa_issues)
        system_prompt = (
            f"You are a senior bilingual QA editor for {genre_role}.\n"
            "Fix omissions, untranslated English fragments, terminology violations, and missing numbers. "
            "Keep the style fluent and natural."
            f"{issue_hint}\n"
            f"{term_prompt}"
            f"{general_rules}\n"
            f"Context of surrounding text:\n{context}\n\n"
            "Return ONLY the repaired Chinese text, no preamble, no markdown."
        )
        user_text = f"Original text:\n{text}\n\nCurrent translation to repair:\n{draft_text}"
    elif stage == "polish":
        # 兼容老调用：等价于 one_pass 但带 draft
        system_prompt = (
            f"You are a {genre_role} and bilingual editor.\n"
            "Polish the draft into FINAL Chinese: faithful AND fluent. Do not omit any sentence.\n"
            f"{term_prompt}"
            f"{general_rules}\n"
            f"Context of surrounding text:\n{context}\n\n"
            "Return ONLY the polished Chinese text, no preamble, no markdown."
        )
        user_text = f"Original text:\n{text}\n\nDraft translation:\n{draft_text}"
    elif stage == "draft":
        # 兼容老调用：直接当 one_pass 跑
        stage = "one_pass"

    if stage == "one_pass":
        few_shots = await sample_few_shots(project_id, db)
        examples = f"\n\nReference examples (maintain this style):\n{few_shots}" if few_shots else ""
        system_prompt = (
            f"You are a professional {genre_role} and bilingual editor.\n"
            "Produce a FINAL, publishable Chinese translation in ONE pass that achieves BOTH:\n"
            "(A) Faithful meaning: preserve every sentence, number, unit, name; no omission; no paraphrase of facts.\n"
            "(B) Native fluency: natural Chinese word order, punctuation, measure words; avoid literal translation.\n"
            f"{term_prompt}"
            f"{general_rules}\n"
            f"Context of surrounding text:\n{context}"
            f"{examples}\n\n"
            "Return ONLY the final Chinese translation, no preamble, no markdown."
        )
        user_text = text

    dynamic_max = max(512, min(TRANSLATION_MAX_TOKENS, int(len(text) * 1.5) + 200))

    if config.provider == APIProvider.GOOGLE:
        result = await translate_with_google_api(config, system_prompt, user_text, max_retries, max_tokens=dynamic_max)
    else:
        result = await translate_with_openai_format(config, system_prompt, user_text, max_retries, max_tokens=dynamic_max)

    return cleanup_translated_text(result)


async def translate_text(text: str, project_id: str, db: AsyncSession, context: str = "", max_retries: int = 3):
    """统一入口（保留向后兼容）。"""
    return await translate_text_with_stage(
        text=text, project_id=project_id, db=db, context=context,
        stage="one_pass", draft_text="", max_retries=max_retries,
    )


async def translate_batch_one_pass(
    segments: list[dict],          # [{"id": int, "text": str, "context": str}]
    project_id: str,
    db: AsyncSession,
    translator_config: Optional[TranslatorConfig] = None,
    term_dict: Optional[dict[str, str]] = None,
    genre_role: str = "professional translator",
    max_retries: int = 2,
) -> dict[int, Optional[str]]:
    """把多个短段拼一次 LLM 调用。失败 / 长度不一致 → 全部返回 None，让上层退到逐段。"""
    if not segments:
        return {}
    config = translator_config or await get_translator_config(db)
    if config.provider == APIProvider.GOOGLE:
        return {seg["id"]: None for seg in segments}

    full_terms = term_dict if term_dict is not None else await get_confirmed_term_dict(project_id, db)
    merged_text = "\n".join(seg["text"] for seg in segments)
    relevant_terms = pick_relevant_terms(merged_text, full_terms)
    term_prompt = ""
    if relevant_terms:
        term_list = "\n".join(f"- {k} -> {v}" for k, v in relevant_terms.items())
        term_prompt = f"Glossary (case-insensitive):\n{term_list}\n"

    payload = [{"id": seg["id"], "text": seg["text"]} for seg in segments]
    contexts = "\n\n".join(
        f"(context for id={seg['id']})\n{seg.get('context', '')}"
        for seg in segments if seg.get("context")
    )

    system_prompt = (
        f"You are a professional {genre_role} and bilingual editor.\n"
        "You will receive a JSON array of segments. For EACH input object with id and text, "
        "produce a FINAL, publishable Chinese translation that is faithful AND fluent.\n"
        f"{term_prompt}"
        "Rules:\n"
        "1) Output MUST be a JSON object {\"results\": [...]} with the SAME length and SAME ids.\n"
        "2) Each item: {\"id\": <same id>, \"translation\": \"<Chinese>\"}.\n"
        "3) No markdown fences, no preamble, no extra keys.\n"
        "4) Preserve all numbers, units, names; follow glossary strictly.\n"
        f"{contexts}"
    )

    client = get_openai_client(config)
    raw = None
    dynamic_max = max(1024, min(TRANSLATION_MAX_TOKENS, int(len(merged_text) * 1.8) + 400))
    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=TRANSLATION_TEMPERATURE,
                max_tokens=dynamic_max,
                response_format={"type": "json_object"},
                **config.extra_params,
            )
            raw = response.choices[0].message.content if response.choices else None
            if raw:
                break
        except APIStatusError as e:
            if 400 <= e.status_code < 500 and e.status_code != 429:
                return {seg["id"]: None for seg in segments}
            await asyncio.sleep(2 ** attempt)
        except Exception:
            await asyncio.sleep(2 ** attempt)

    if not raw:
        return {seg["id"]: None for seg in segments}

    try:
        parsed = _json.loads(raw)
        if isinstance(parsed, dict):
            for k in ("results", "translations", "data"):
                if isinstance(parsed.get(k), list):
                    parsed = parsed[k]
                    break
        if not isinstance(parsed, list) or len(parsed) != len(segments):
            return {seg["id"]: None for seg in segments}

        out: dict[int, Optional[str]] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            sid = item.get("id")
            t = item.get("translation") or item.get("text")
            if isinstance(sid, int) and isinstance(t, str):
                out[sid] = cleanup_translated_text(t)
        for seg in segments:
            out.setdefault(seg["id"], None)
        return out
    except Exception:
        return {seg["id"]: None for seg in segments}