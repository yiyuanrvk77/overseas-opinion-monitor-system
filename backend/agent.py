"""Qwen (DashScope) Agent with a local explainable fallback.

The agent can plan an investigation and explain evidence. Without a
DASHSCOPE_API_KEY it degrades to deterministic local templates so the whole
system keeps running and stays auditable.
"""
from __future__ import annotations

import json
import os
from typing import Any

import requests


class QwenGovernanceAgent:
    API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    def __init__(self) -> None:
        self.api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self.model = os.getenv("QWEN_CHAT_MODEL", "qwen-plus").strip() or "qwen-plus"

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY 未配置")
        response = requests.post(
            self.API_URL,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "messages": messages, "temperature": temperature},
            timeout=60,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return str(content).strip()

    def plan(self, task: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        task = (task or "").strip()
        context = context or {}
        if not self.available:
            return self._fallback_plan(task, context)
        prompt = (
            "你是社科院海外舆情监测系统的研判 Agent。基于给定任务与已有监测上下文，"
            "给出 3-4 条可执行的研判/核查步骤，说明每一步考察什么。"
            f"\n任务：{task}\n"
            f"上下文(JSON)：{json.dumps(context, ensure_ascii=False)[:2000]}"
        )
        try:
            text = self._chat([
                {"role": "system", "content": "你是严谨的海外舆情研判专家，输出中文，条理清晰。"},
                {"role": "user", "content": prompt},
            ])
            return {"task": task, "plan": text, "provider": "qwen", "model": self.model}
        except Exception as exc:  # noqa: BLE001
            result = self._fallback_plan(task, context)
            result["warning"] = f"Qwen 暂不可用：{exc}；已使用本地研判编排。"
            return result

    def explain(self, evidence: dict[str, Any] | None = None, *, use_llm: bool = True) -> dict[str, Any]:
        evidence = evidence or {}
        if not use_llm or not self.available:
            return self._fallback_explain(evidence)
        prompt = (
            "请基于以下可追溯证据，用中文给出简短的研判结论，说明证据性质、强度与不确定性，"
            "不要影射事实。\n证据(JSON)："
            + json.dumps(evidence, ensure_ascii=False)[:2500]
        )
        try:
            text = self._chat([
                {"role": "system", "content": "你是社科院海外舆情研判助手，保持客观。"},
                {"role": "user", "content": prompt},
            ])
            return {"explanation": text, "provider": "qwen", "model": self.model}
        except Exception as exc:  # noqa: BLE001
            result = self._fallback_explain(evidence)
            result["warning"] = f"Qwen 暂不可用：{exc}；已使用本地事实模板。"
            return result

    @staticmethod
    def _fallback_plan(task: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = context or {}
        lines = [
            "1. 定位对象与证据：确认人物/账号/事件，标注其敏感级与证据性质。",
            "2. 交叉核验来源：对比不同文件的指标、时间与口径冲突，标记缺口。",
            "3. 关联图谱分析：沿人际关系、账号矩阵与事件时间线检查关键节点。",
            "4. 形成研判边界：给出结论并明确标注哪些是转述、哪些是研究推断。",
        ]
        if ctx.get("subjects"):
            lines.insert(1, f"0. 重点对象：{'、'.join(str(s) for s in ctx['subjects'][:5])}。")
        return {
            "task": task,
            "plan": "\n".join(lines),
            "provider": "local",
            "model": "explainable-orchestration",
            "compact": True,
        }

    @staticmethod
    def _fallback_explain(evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        evidence = evidence or {}
        evidence_type = str(evidence.get("evidence_type") or "source_snapshot")
        labels = {
            "explicit_source_text": "源文件明示，可信度较高。",
            "direct_post_excerpt": "直接帖子摘录，可信度较高。",
            "reported_by_source_file": "来源报告转述，需回到原始来源复核。",
            "source_analysis": "源文件作者分析，含研究推断成分。",
            "inference_or_speculation": "研究推断，不应视为事实。",
            "source_conflict": "跨文件口径冲突，需进一步核验。",
            "production_gap": "生产字段缺口，暂缺完整证据。",
        }
        summary = str(evidence.get("summary") or evidence.get("title") or "该证据暂无可展示摘要。")
        text = f"证据性质：{labels.get(evidence_type, '待进一步判定')}\n内容概要：{summary}\n"
        text += "结论：可作为线索供人工研判，具体事实认定请以第一步可核查来源为准。"
        return {"explanation": text, "provider": "local", "model": "fact-template", "compact": True}


qwen_agent = QwenGovernanceAgent()
