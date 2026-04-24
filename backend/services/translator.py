import os
import asyncio
import logging
import httpx
import re
from enum import Enum
from typing import Optional
from openai import AsyncOpenAI, APITimeoutError, APIConnectionError, APIStatusError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.models.models import Settings, Terminology

logger = logging.getLogger(__name__)

_openai_clients: dict[tuple[str, str, float], AsyncOpenAI] = {}
_google_clients: dict[float, httpx.AsyncClient] = {}
TRANSLATION_TEMPERATURE = float(os.getenv("TRANSLATION_TEMPERATURE", "0.2"))
TRANSLATION_MAX_TOKENS = int(os.getenv("TRANSLATION_MAX_TOKENS", "4096"))
SOURCE_ALPHA_PATTERN = re.compile(r"[A-Za-z]")
EN_WORD_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z\-']{2,}\b")

class APIProvider(Enum):
    """支持的 API 提供商"""
    OPENAI = "openai"
    SILICONFLOW = "siliconflow"
    GOOGLE = "google"
    OLLAMA = "ollama"
    CUSTOM = "custom"

class TranslatorConfig:
    """统一的翻译配置"""
    def __init__(
        self,
        provider: APIProvider,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        extra_params: Optional[dict] = None
    ):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.extra_params = extra_params or {}

# API 提供商配置映射
PROVIDER_CONFIGS = {
    APIProvider.OPENAI: {
        "base_url": "https://api.openai.com/v1",
        "default_models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
        "timeout": 60.0,
        "api_format": "openai"
    },
    APIProvider.SILICONFLOW: {
        "base_url": "https://api.siliconflow.cn/v1",
        "default_models": [
            "deepseek-ai/DeepSeek-V3.2",
            "Qwen/Qwen2.5-72B-Instruct",
            "THUDM/glm-4-9b-chat"
        ],
        "timeout": 90.0,
        "api_format": "openai"
    },
    APIProvider.GOOGLE: {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_models": ["gemini-2.0-flash", "gemini-2.5-pro", "gemini-1.5-pro"],
        "timeout": 120.0,
        "api_format": "google"
    },
    APIProvider.OLLAMA: {
        "base_url": "http://localhost:11434/v1",
        "default_models": ["llama3", "qwen2.5", "deepseek-r1"],
        "timeout": 120.0,
        "api_format": "openai"
    },
    APIProvider.CUSTOM: {
        "base_url": "",
        "default_models": [],
        "timeout": 90.0,
        "api_format": "openai"
    }
}

def detect_provider(base_url: str) -> APIProvider:
    """根据 base_url 自动检测 API 提供商"""
    if "siliconflow.cn" in base_url or "siliconflow.com" in base_url:
        return APIProvider.SILICONFLOW
    elif "googleapis.com" in base_url:
        return APIProvider.GOOGLE
    elif "localhost:11434" in base_url or "127.0.0.1:11434" in base_url:
        return APIProvider.OLLAMA
    elif "openai.com" in base_url:
        return APIProvider.OPENAI
    else:
        return APIProvider.CUSTOM

async def get_translator_config(db: AsyncSession) -> TranslatorConfig:
    """获取翻译配置"""
    stmt = select(Settings).where(Settings.id == "default")
    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()
    
    if not settings or not settings.openai_api_key:
        raise ValueError("API Key is not configured. Please set it in the settings page.")
    
    provider = detect_provider(settings.openai_base_url)
    provider_config = PROVIDER_CONFIGS.get(provider, PROVIDER_CONFIGS[APIProvider.CUSTOM])
    
    return TranslatorConfig(
        provider=provider,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.model_name,
        timeout=provider_config["timeout"]
    )

def get_openai_client(config: TranslatorConfig) -> AsyncOpenAI:
    key = (config.api_key, config.base_url, config.timeout)
    client = _openai_clients.get(key)
    if client is None:
        client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout
        )
        _openai_clients[key] = client
    return client

def get_google_client(config: TranslatorConfig) -> httpx.AsyncClient:
    client = _google_clients.get(config.timeout)
    if client is None:
        client = httpx.AsyncClient(timeout=config.timeout)
        _google_clients[config.timeout] = client
    return client

async def get_confirmed_term_dict(project_id: str, db: AsyncSession) -> dict[str, str]:
    stmt = select(Terminology).where(Terminology.project_id == project_id, Terminology.is_confirmed == True)
    result = await db.execute(stmt)
    terms = result.scalars().all()
    return {term.original_term: term.translated_term for term in terms if term.translated_term}

def build_quality_term_dict(term_dict: dict[str, str]) -> dict[str, str]:
    """扩展术语变体用于质检（不用于提示词），提升命中率。"""
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

async def translate_with_openai_format(config: TranslatorConfig, system_prompt: str, user_text: str, max_retries: int = 3):
    """使用 OpenAI 兼容格式调用 API"""
    client = get_openai_client(config)
    
    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}
                ],
                temperature=TRANSLATION_TEMPERATURE,
                max_tokens=TRANSLATION_MAX_TOKENS,
                **config.extra_params
            )
            
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

async def translate_with_google_api(config: TranslatorConfig, system_prompt: str, user_text: str, max_retries: int = 3):
    """使用 Google Gemini 原生 API"""
    url = f"{config.base_url}/models/{config.model}:generateContent?key={config.api_key}"
    
    for attempt in range(max_retries):
        try:
            client = get_google_client(config)
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [
                        {
                            "parts": [
                                {"text": f"{system_prompt}\n\n{user_text}"}
                            ]
                        }
                    ],
                    "generationConfig": {
                        "temperature": TRANSLATION_TEMPERATURE,
                        "maxOutputTokens": TRANSLATION_MAX_TOKENS,
                    }
                }
            )

            if response.status_code != 200:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "Unknown error")
                logger.error(f"Google API Error (attempt {attempt + 1}/{max_retries}): {response.status_code} - {error_msg}")

                if response.status_code == 429 and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt * 2)
                    continue
                elif 400 <= response.status_code < 500:
                    return None
                else:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return None

            result = response.json()

            if "candidates" in result and len(result["candidates"]) > 0:
                content = result["candidates"][0].get("content", {})
                parts = content.get("parts", [])
                if parts and "text" in parts[0]:
                    return parts[0]["text"].strip()

            logger.warning(f"Empty response from Google API on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return None
                
        except httpx.TimeoutException as e:
            logger.error(f"Google API Timeout (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt * 2)
            else:
                return None
                
        except httpx.ConnectError as e:
            logger.error(f"Google API Connection Error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt * 2)
            else:
                return None
                
        except Exception as e:
            logger.error(f"Google API Error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return None
    
    return None

async def translate_text(text: str, project_id: str, db: AsyncSession, context: str = "", max_retries: int = 3):
    """统一的翻译入口（两阶段可通过 stage 参数控制）"""
    return await translate_text_with_stage(
        text=text,
        project_id=project_id,
        db=db,
        context=context,
        stage="draft",
        draft_text="",
        max_retries=max_retries
    )

async def translate_text_with_stage(
    text: str,
    project_id: str,
    db: AsyncSession,
    context: str = "",
    stage: str = "draft",
    draft_text: str = "",
    max_retries: int = 3,
    translator_config: Optional[TranslatorConfig] = None,
    term_dict: Optional[dict[str, str]] = None
):
    """按阶段翻译：draft（草译）/polish（润色）"""
    config = translator_config or await get_translator_config(db)
    terms = term_dict if term_dict is not None else await get_confirmed_term_dict(project_id, db)
    term_prompt = ""
    if terms:
        term_list = "\n".join([f"- {k} -> {v}" for k, v in terms.items()])
        term_prompt = f"Please strictly follow these terminology translations (case-insensitive match):\n{term_list}\n"

    general_rules = """Rules:
1) Preserve full meaning, entities, numbers, units, and logical relations.
2) Keep paragraph boundaries and line breaks when present.
3) Keep punctuation natural in Chinese and avoid literal word-by-word translation.
4) Do not add explanations, notes, or markdown.
5) Preserve domain terms and proper nouns according to glossary."""

    if stage == "repair":
        system_prompt = f"""You are a senior translation QA editor for long-form sci-fi and academic content.
Your task: fix omissions, untranslated English fragments, and terminology inconsistencies while keeping style fluent.
{term_prompt}
{general_rules}
Context of surrounding text: {context}

Return ONLY the repaired Chinese text, without any explanations or markdown formatting."""
        user_text = f"Original text:\n{text}\n\nCurrent translation to repair:\n{draft_text}"
    elif stage == "polish":
        system_prompt = f"""You are a professional sci-fi novel translator and editor.
Polish the draft translation into fluent, natural Chinese while preserving full meaning and factual details.
Do not omit any sentence.
{term_prompt}
{general_rules}
Context of surrounding text: {context}

Return ONLY the polished Chinese text, without any explanations or markdown formatting."""
        user_text = f"Original text:\n{text}\n\nDraft translation:\n{draft_text}"
    else:
        system_prompt = f"""You are a professional sci-fi novel translator. Translate the given text from English to Chinese.
Focus on high accuracy, consistent tone, and smooth reading experience.
{term_prompt}
{general_rules}
Context of surrounding text: {context}

Return ONLY the translated text, without any explanations or markdown formatting."""
        user_text = text

    # 根据提供商选择调用方式
    if config.provider == APIProvider.GOOGLE:
        result = await translate_with_google_api(config, system_prompt, user_text, max_retries)
    else:
        # OpenAI 兼容格式（硅基流动、Ollama、自定义等）
        result = await translate_with_openai_format(config, system_prompt, user_text, max_retries)

    return cleanup_translated_text(result)

def cleanup_translated_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned.strip("`").strip()
    cleaned = cleaned.replace("\r\n", "\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

def _contains_excessive_untranslated_english(original_text: str, translated_text: str) -> bool:
    source_alpha_count = len(SOURCE_ALPHA_PATTERN.findall(original_text or ""))
    if source_alpha_count < 20:
        return False

    translated_english_words = EN_WORD_PATTERN.findall(translated_text or "")
    if not translated_english_words:
        return False

    translated_alpha_count = sum(len(word) for word in translated_english_words)
    ratio = translated_alpha_count / max(1, source_alpha_count)
    return ratio > 0.45

def basic_quality_check(original_text: str, translated_text: str, term_dict: dict[str, str]) -> bool:
    """基础质检：非空、长度合理、术语命中"""
    if not translated_text or not translated_text.strip():
        return False

    original_len = len(original_text.strip())
    translated_len = len(translated_text.strip())

    if original_len > 30 and translated_len < max(6, int(original_len * 0.2)):
        return False

    if _contains_excessive_untranslated_english(original_text, translated_text):
        return False

    for source_term, target_term in term_dict.items():
        if not source_term or not target_term:
            continue
        if source_term.lower() in original_text.lower() and target_term not in translated_text:
            return False

    return True
