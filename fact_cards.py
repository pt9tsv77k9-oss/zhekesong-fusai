"""本地事实卡：人工确认与模型写前审查并用，导出时重查有效期和停用状态。"""

from datetime import datetime, timezone, timedelta, date
from hashlib import sha256
from pathlib import Path
import json
import os
import uuid

from checks import EvidenceError, validate_facts
from config import ROOT
from evidence_utils import timestamp
from schemas import Evidence, Fact, FactExtraction, ProductFactCard, normalize_model


FACT_CARD_DIR = ROOT / "data" / "fact_cards"
DYNAMIC_CATEGORIES = {"软件兼容", "价格", "赠品", "门店服务"}


def local_today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def source_digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def card_path(card_id: str) -> Path:
    try:
        if str(uuid.UUID(card_id)) != card_id:
            raise ValueError()
    except ValueError as exc:
        raise EvidenceError("事实卡编号无效。") from exc
    return FACT_CARD_DIR / f"{card_id}.json"


def check_card(card: ProductFactCard, evidence: Evidence, today=None, require_active=True):
    today = today or local_today()
    if normalize_model(card.product_model) != normalize_model(evidence.product_model):
        raise EvidenceError("事实卡与当前产品型号不一致。")
    if require_active and (card.status != "已确认" or today < card.valid_from or (card.valid_until and today > card.valid_until)):
        raise EvidenceError("事实卡已停用、未生效或已过期，请重新确认资料。")
    if card.category in DYNAMIC_CATEGORIES and card.valid_until is None:
        raise EvidenceError("兼容性、价格、赠品和服务事实卡必须设置有效期。")
    if card.valid_until and card.valid_until < card.valid_from:
        raise EvidenceError("事实卡结束日期不能早于开始日期。")
    source = next((s for s in evidence.sources if s.title == card.source_title
                   and source_digest(s.text) == card.source_hash), None)
    if source is None or card.quote not in source.text:
        raise EvidenceError("事实卡原文已变化或引句不能连续匹配，请重新核对。")
    return source


def create_card(evidence, source_id, claim, quote, category, scope, valid_from, valid_until, confirmed_by):
    source = next((s for s in evidence.sources if s.source_id == source_id), None)
    if source is None:
        raise EvidenceError("请选择当前产品的原文来源。")
    card = ProductFactCard(card_id=str(uuid.uuid4()), product_model=evidence.product_model,
                           category=category, claim=claim.strip(), quote=quote.strip(), source_title=source.title,
                           source_hash=source_digest(source.text), scope=scope.strip(), valid_from=valid_from,
                           valid_until=valid_until, confirmed_by=confirmed_by.strip(), confirmed_at=timestamp())
    check_card(card, evidence, require_active=False)
    FACT_CARD_DIR.mkdir(parents=True, exist_ok=True)
    path = card_path(card.card_id)
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(card.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return card


def list_cards(product_model: str) -> list[ProductFactCard]:
    cards = []
    for path in sorted(FACT_CARD_DIR.glob("*.json")):
        try:
            card = ProductFactCard.model_validate_json(path.read_text(encoding="utf-8"))
            if normalize_model(card.product_model) == normalize_model(product_model):
                cards.append(card)
        except (ValueError, OSError):
            raise EvidenceError("事实卡文件读取失败，请检查本地data/fact_cards目录。") from None
    return cards


def disable_card(card_id: str):
    path = card_path(card_id)
    try:
        card = ProductFactCard.model_validate_json(path.read_text(encoding="utf-8"))
        card.status = "已停用"
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(card.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except (ValueError, OSError) as exc:
        raise EvidenceError("停用事实卡失败，请检查本地文件权限。") from exc


def facts_from_cards(evidence: Evidence, today=None) -> list[Fact]:
    if not evidence.approved_cards or len(evidence.approved_cards) > 8:
        raise EvidenceError("事实卡模式下请选择1到8条已确认且有效的事实卡。")
    if len({c.card_id for c in evidence.approved_cards}) != len(evidence.approved_cards):
        raise EvidenceError("事实卡重复，请重新选择。")
    facts = []
    for card in evidence.approved_cards:
        source = check_card(card, evidence, today)
        try:
            current = ProductFactCard.model_validate_json(card_path(card.card_id).read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise EvidenceError("事实卡已移除或不可读取，请重新选择。") from exc
        if current != card:
            raise EvidenceError("事实卡状态或内容已更新，请重新加载后生成。")
        # 适用范围与允许表述一起审查，避免把特定配置承诺推广到所有配置。
        facts.append(Fact(claim=f"{card.claim}（适用范围：{card.scope}）", source_id=source.source_id, quote=card.quote))
    return validate_facts(FactExtraction(product_model=evidence.product_model, facts=facts), evidence)


def cards_signature(enabled: bool, cards: list[ProductFactCard]) -> str:
    if not enabled:
        return ""
    payload = json.dumps([c.model_dump(mode="json") for c in cards], ensure_ascii=False, sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()
