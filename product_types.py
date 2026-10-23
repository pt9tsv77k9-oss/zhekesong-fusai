"""仅用于选择写作场景；品类和型号名称不授权任何产品能力。"""

import re


def is_phone(product_model: str) -> bool:
    return bool(re.search(r"(?:HUAWEI|华为)\s*(?:Mate\s*\d|Pura\s*\d|nova\s*\d)", product_model, re.I))


def product_label(product_model: str) -> str:
    return "手机" if is_phone(product_model) else "电脑"
