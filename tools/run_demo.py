"""不联网的三平台演示。全部资料、图片和输出都是模拟，不能作为参赛真实验收。

运行：python -X utf8 tools/run_demo.py。无需密钥或 Node.js，不读取 .env。
这里保留同一套生产工作流与导出，只用 DemoEngine 替代收费模型。
"""

from io import BytesIO
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from checks import draft_sections
from evidence_utils import timestamp
from exporter import to_markdown
from schemas import (Block, ComplianceAudit, COMPLIANCE_CATEGORIES, Draft, Evidence, FactAudit,
                     FactProposal, ImageAsset, ImagePlacement, ModelReview, PLATFORM_RULES,
                     SegmentExtraction, Source, Strategy, Task, FactPlan, FactSupport)
from workflow import run_workflow


class DemoEngine:
    """按输入返回固定的合成数据，用于学习流程，不评价真实模型能力。"""
    def run(self, stage, schema, payload):
        if stage == "strategy":
            return Strategy(audience_problem="模拟学习任务", angle="模拟屏幕资料", priority_points=["屏幕"],
                            structure=["场景", "事实", "咨询"], tone="简洁", image_count=3, call_to_action="咨询")
        if stage == "extract":
            segment = payload["segments"][0]
            return SegmentExtraction(product_model=payload["task"]["product_model"], facts=[
                FactProposal(claim="模拟屏幕为14.2英寸", source_id=segment["source_id"], segment_id=segment["segment_id"])])
        if stage == "generate":
            return Draft(product_model=payload["task"]["product_model"],
                         title=Block(text="模拟演示：从学习场景了解电脑"),
                         intro=Block(text="全部为虚构资料，用来体验程序操作。"),
                         body=[Block(text="模拟屏幕为14.2英寸。", fact_ids=["F1"])],
                         cta=Block(text="请根据真实资料咨询选型。"), tags=["#模拟演示"],
                         images=[ImagePlacement(image_id=image["image_id"], after_paragraph=1,
                                                caption="模拟图片", reason="展示配图位置") for image in payload["images"][:3]])
        if stage == "ground":
            return FactPlan(checks=[FactSupport(fact_id="F1", status="支持", reason="模拟事实支持")],
                            selected_fact_ids=["F1"], angle="模拟屏幕", selection_reason="仅为固定模拟结论")
        draft = Draft.model_validate(payload["draft"])
        return ModelReview(fact_checks=[FactAudit(location=location, fact_ids=ids,
                                                  status="通过" if ids else "不涉及", reason="仅为固定模拟结论")
                                        for location, _, ids in draft_sections(draft)],
                           compliance_checks=[ComplianceAudit(category=category, status="不涉及", reason="模拟审核")
                                              for category in COMPLIANCE_CATEGORIES])


def main():
    model = "模拟学习电脑A"
    evidence = Evidence(product_model=model, folder_title="本地模拟资料", kb_name="模拟库（非 ima）", loaded_at=timestamp(),
                        sources=[Source(source_id="S1", title="虚构教学资料", text="模拟屏幕为14.2英寸。此信息仅用于教学。")])
    # 三张不同颜色的 PNG 避免同图重复凑数；它们不是任何真实产品图片。
    for index, color in enumerate(["red", "green", "blue"], 1):
        buffer = BytesIO()
        Image.new("RGB", (120, 80), color).save(buffer, format="PNG")
        evidence.images.append(ImageAsset(image_id=f"I{index}", name=f"模拟图片{index}.png", product_model=model,
                                          source_title="本地生成色块", description="模拟色块，非产品图",
                                          verified=True, verification_method="模拟夹具", data=buffer.getvalue()))
    destination = Path(__file__).resolve().parents[1] / "outputs"
    destination.mkdir(exist_ok=True)
    for platform in PLATFORM_RULES:
        task = Task(product_model=model, audience="大学新生", scenario="上课整理课业", platform=platform)
        result = run_workflow(task, evidence, DemoEngine())
        path = destination / f"模拟演示_{platform}.md"
        path.write_text("> 模拟演示：未调用真实模型或 ima，不代表产品事实或真实验收。\n\n" + to_markdown(result), encoding="utf-8")
        if not result.review.passed:
            raise RuntimeError("模拟工作流未通过，请先运行离线测试。")
        print(f"已完成模拟流程并导出：{path}")


if __name__ == "__main__":
    main()
