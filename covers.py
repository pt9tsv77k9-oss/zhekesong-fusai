"""真实原图的完整等比排版；不生成或修补产品像素。"""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from checks import EvidenceError
from cover_titles import HEADLINES, clean_headline, review_headline, result_signature
from evidence_utils import timestamp
from exporter import require_publishable
from schemas import ImageAsset, Result, Task, normalize_model


PALETTES = {"白底红线": ("#FFFFFF", "#B92232", "#252525", "#F1F2F3"),
            "浅灰绿线": ("#F5F6F5", "#217458", "#252525", "#FFFFFF")}
SIZES = {"小红书": (1080, 1440), "公众号": (1200, 675), "朋友圈": (1080, 1080)}


@dataclass(frozen=True)
class Cover:
    png: bytes
    original: bytes
    manifest: dict

    def package(self) -> bytes:
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("封面.png", self.png)
            archive.writestr("原图" + self.manifest["original_extension"], self.original)
            archive.writestr("素材记录.json", json.dumps(self.manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        return output.getvalue()


def font_path() -> str:
    paths = [os.environ.get("COVER_FONT_PATH", ""), r"C:\Windows\Fonts\msyh.ttc",
             r"C:\Windows\Fonts\simhei.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]
    for path in paths:
        if path and Path(path).is_file():
            return path
    raise EvidenceError("未找到中文字体，请配置COVER_FONT_PATH后制作封面。")


def decode_image(data: bytes):
    if not data or len(data) > 15_000_000:
        raise EvidenceError("原图需小于15MB，且必须为可读取的真实图片。")
    try:
        with Image.open(BytesIO(data)) as original:
            if original.width * original.height > 24_000_000 or min(original.size) < 16:
                raise EvidenceError("原图尺寸需大于等于16像素，总像素不超过2400万。")
            extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(original.format)
            if not extension:
                raise EvidenceError("封面原图仅支持PNG、JPEG和WEBP静态图片。")
            if getattr(original, "n_frames", 1) != 1:
                raise EvidenceError("请使用静态产品图。")
            original.load()
            picture = ImageOps.exif_transpose(original).convert("RGBA")
            background = Image.new("RGBA", picture.size, "white")
            background.alpha_composite(picture)
            return background.convert("RGB"), extension
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise EvidenceError("无法读取原图，请重新选择完整的产品图片。") from exc


def wrapped_lines(draw, text, font, width):
    lines, line = [], ""
    for char in text:
        if char == "\n":
            lines.append(line)
            line = ""
        elif draw.textlength(line + char, font=font) > width:
            if line:
                lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    return lines


def draw_text_box(draw, text, box, font_file, size, fill, min_size=24):
    left, top, right, bottom = box
    for candidate in range(size, min_size - 1, -2):
        font = ImageFont.truetype(font_file, candidate)
        lines = wrapped_lines(draw, text, font, right - left)
        line_height = candidate + 14
        if len(lines) * line_height <= bottom - top:
            for index, line in enumerate(lines):
                draw.text((left, top + index * line_height), line, font=font, fill=fill)
            return
    raise EvidenceError("封面文字过长，请选择较短的标题或不展示参数。")


def make_cover(asset: ImageAsset, task: Task, headline: str, palette: str, rights_confirmed: bool,
               result: Result | None = None, fact_id: str = "", *, engine=None) -> Cover:
    if not rights_confirmed:
        raise EvidenceError("请先确认原图型号及使用权限。")
    if not asset.verified or normalize_model(asset.product_model) != normalize_model(task.product_model):
        raise EvidenceError("原图未确认型号或与当前产品不一致。")
    headline = clean_headline(headline)
    if palette not in PALETTES:
        raise EvidenceError("请选择已有的封面配色。")
    title_review = review_headline(headline, task, result, engine)
    fact_text = ""
    if fact_id:
        if result is None:
            raise EvidenceError("参数需来自当前已通过审核的稿件。")
        require_publishable(result, task)
        if fact_id not in result.fact_plan.selected_fact_ids:
            raise EvidenceError("封面参数不属于本次审核通过的选材范围。")
        fact_text = next(f.claim for f in result.facts if f.fact_id == fact_id)
    original, extension = decode_image(asset.data)
    width, height = SIZES[task.platform]
    background, accent, ink, panel = PALETTES[palette]
    canvas = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(canvas)
    font_file = font_path()
    draw.rectangle((48, 48, 56, 104), fill=accent)
    if task.platform == "公众号":
        text_right = 470
        title_box = (80, 56, text_right, 240)
        model_box = (64, 260, text_right, 410)
        fact_box = (64, 425, text_right, 585)
        image_box = (520, 64, width - 48, height - 64)
    else:
        title_box = (80, 56, width - 64, 165)
        model_box = (64, 180, width - 64, 310)
        image_box = (64, 338, width - 64, height - (285 if fact_text else 140))
        fact_box = (64, height - 260, width - 64, height - 105)
    draw_text_box(draw, headline, title_box, font_file, 64, ink, min_size=36)
    draw_text_box(draw, task.product_model, model_box, font_file, 36, ink)
    if fact_text:
        draw_text_box(draw, fact_text, fact_box, font_file, 28, ink)
    draw.rectangle(image_box, fill=panel)
    left, top, right, bottom = image_box
    # contain只缩放完整原图；不裁切、不抠图、不透视、不修复产品本体。
    placed = ImageOps.contain(original, (right - left - 32, bottom - top - 32), Image.Resampling.LANCZOS)
    x = left + (right - left - placed.width) // 2
    y = top + (bottom - top - placed.height) // 2
    canvas.paste(placed, (x, y))
    draw.line((64, height - 70, width - 64, height - 70), fill=accent, width=3)
    draw_text_box(draw, "具体配置与适用条件以产品资料为准", (64, height - 55, width - 64, height - 14),
                  font_file, 24, ink)
    output = BytesIO()
    canvas.save(output, "PNG")
    manifest = {"product_model": task.product_model, "platform": task.platform, "headline": headline,
                "headline_review": title_review.model_dump(),
                "task_signature": sha256(task.model_dump_json().encode("utf-8")).hexdigest(),
                "reviewed_result_signature": result_signature(result) if fact_id or title_review.fact_ids else "",
                "palette": palette, "created_at": timestamp(), "source_title": asset.source_title,
                "source_name": asset.name, "source_sha256": sha256(asset.data).hexdigest(),
                "original_extension": extension, "rights_confirmed": True,
                "model_confirmation": asset.verification_method,
                "canvas_size": [width, height], "original_size": list(original.size),
                "placed_box": [x, y, x + placed.width, y + placed.height],
                "placed_pixels_sha256": sha256(placed.tobytes()).hexdigest(),
                "operation": "full_image_contain_no_crop_no_redraw", "fact_id": fact_id, "fact_text": fact_text}
    return Cover(output.getvalue(), asset.data, manifest)
