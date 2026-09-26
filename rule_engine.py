#!/usr/bin/env python3
"""rule_engine.py — 规则操作流演化与生效判定工具（纯标准库，单文件）。

输入（JSON 文件或 stdin）：
{
  "rules": [                          # 初始规则定义（可选）
    {"name": "R1", "version": 1, "condition": "age >= 18", "priority": 10}
  ],
  "operations": [                     # 操作流，按顺序执行
    {"op": "add",     "rule": {"name": "R2", "condition": "vip", "priority": 5}},
    {"op": "modify",  "name": "R1", "rule": {"condition": "age >= 21", "priority": 20}},
    {"op": "abolish", "name": "R2"},
    {"op": "query",   "facts": {"age": 30, "vip": true}, "names": ["R1", "R9"]}
  ]
}

说明：
- condition 为 Python 表达式，按 query 携带的 facts 求值（仅信任场景使用）。
- version 省略时：add 默认 1；modify 自动在旧版本号上 +1。
- add 同名同版本 => 报 DUPLICATE_DEFINITION；同名不同版本 => 旧版本废除、新版本生效。
- modify 后旧版本即时废除；abolish 后该规则所有版本不再生效。
- query 的 "names" 可选，用于点名核查规则是否存在（不存在则报 UNKNOWN_RULE）。

用法：
  python3 rule_engine.py input.json      # 从文件读取
  python3 rule_engine.py - < input.json  # 从 stdin 读取
  python3 rule_engine.py --demo          # 运行内置示例
  python3 rule_engine.py --demo --json   # JSON 格式输出
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass


# ---------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class Rule:
    name: str
    version: int
    condition: str
    priority: int

    @property
    def key(self) -> str:
        return f"{self.name}@v{self.version}"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "condition": self.condition,
            "priority": self.priority,
            "key": self.key,
        }


# ---------------------------------------------------------------- 引擎

class RuleEngine:
    def __init__(self) -> None:
        self.versions: dict[str, dict[int, Rule]] = {}   # name -> {version: Rule}
        self.abolished: set[tuple[str, int]] = set()     # 已废除的 (name, version)
        self.errors: list[dict] = []                     # 全局错误清单
        self.history: list[dict] = []                    # 每步前后状态
        self.queries: list[dict] = []                    # 查询结果
        self.step = 0

    # ---- 状态辅助 ----

    def active_rules(self) -> list[Rule]:
        """当前生效规则：每个规则名的最高未废除版本。"""
        result = []
        for name, vers in self.versions.items():
            alive = [v for v in vers.values() if (name, v.version) not in self.abolished]
            if alive:
                result.append(max(alive, key=lambda r: r.version))
        return sorted(result, key=lambda r: (-r.priority, r.name))

    def _snapshot(self) -> list[str]:
        return [r.key for r in self.active_rules()]

    def _error(self, code: str, message: str, op: dict) -> dict:
        err = {"step": self.step, "op": op.get("op"), "code": code, "message": message}
        self.errors.append(err)
        return err

    def _record(self, op: dict, before: list[str], extra: dict | None = None) -> None:
        entry = {
            "step": self.step,
            "op": op.get("op"),
            "detail": {k: v for k, v in op.items() if k != "op"},
            "before": before,
            "after": self._snapshot(),
        }
        if extra:
            entry.update(extra)
        self.history.append(entry)

    # ---- 操作 ----

    def define(self, spec: dict, initial: bool = False) -> list[dict]:
        """新增规则定义。返回本步产生的错误。"""
        errs = []
        name = spec.get("name")
        if not name:
            errs.append(self._error("INVALID_RULE", "规则缺少 name 字段", {"op": "add", "rule": spec}))
            return errs
        version = int(spec.get("version", 1))
        rule = Rule(name=name, version=version,
                    condition=str(spec.get("condition", "True")),
                    priority=int(spec.get("priority", 0)))
        known = self.versions.setdefault(name, {})
        if version in known and (name, version) not in self.abolished:
            errs.append(self._error(
                "DUPLICATE_DEFINITION",
                f"规则 {rule.key} 重复定义（同名同版本）",
                {"op": "add", "rule": spec}))
            return errs
        # 同名不同版本：旧版本全部废除，新版本生效
        for old_v in list(known):
            self.abolished.add((name, old_v))
        known[version] = rule
        self.abolished.discard((name, version))
        return errs

    def modify(self, name: str, spec: dict) -> list[dict]:
        """修改规则：生成新版本，旧版本废除。"""
        op = {"op": "modify", "name": name, "rule": spec}
        known = self.versions.get(name)
        alive = [v for v in (known or {}) if (name, v) not in self.abolished]
        if not known or not alive:
            return [self._error("MODIFY_MISSING", f"修改的规则 {name} 不存在或已废除", op)]
        new_version = int(spec.get("version", max(alive) + 1))
        if new_version in known and (name, new_version) not in self.abolished:
            return [self._error(
                "DUPLICATE_DEFINITION",
                f"修改产生重复版本 {name}@v{new_version}", op)]
        merged = dict(spec)
        merged.setdefault("condition", known[max(alive)].condition)
        merged.setdefault("priority", known[max(alive)].priority)
        for old_v in list(known):
            self.abolished.add((name, old_v))
        known[new_version] = Rule(name=name, version=new_version,
                                  condition=str(merged["condition"]),
                                  priority=int(merged["priority"]))
        return []

    def abolish(self, name: str) -> list[dict]:
        op = {"op": "abolish", "name": name}
        known = self.versions.get(name)
        alive = [v for v in (known or {}) if (name, v) not in self.abolished]
        if not known or not alive:
            return [self._error("ABOLISH_MISSING", f"废除的规则 {name} 不存在或已废除", op)]
        for v in alive:
            self.abolished.add((name, v))
        return []

    def query(self, facts: dict, names: list[str] | None) -> dict:
        """查询当前生效规则：返回适用规则、生效结果、冲突与本步错误。"""
        op = {"op": "query", "facts": facts, "names": names or []}
        errs = []
        # 点名核查
        for n in names or []:
            known = self.versions.get(n)
            alive = [v for v in (known or {}) if (n, v) not in self.abolished]
            if not known:
                errs.append(self._error("UNKNOWN_RULE", f"查询引用了不存在的规则 {n}", op))
            elif not alive:
                errs.append(self._error("UNKNOWN_RULE", f"查询引用的规则 {n} 已被废除", op))
        # 条件求值
        applicable, eval_errors = [], []
        for rule in self.active_rules():
            try:
                hit = bool(eval(rule.condition, {"__builtins__": {}}, dict(facts)))
            except Exception as exc:  # 条件中变量缺失等
                eval_errors.append(f"{rule.key}: {exc}")
                hit = False
            if hit:
                applicable.append(rule)
        for msg in eval_errors:
            errs.append(self._error("CONDITION_ERROR", f"条件求值失败 {msg}", op))
        # 冲突检测：同级优先级存在多条适用规则
        by_prio: dict[int, list[Rule]] = {}
        for r in applicable:
            by_prio.setdefault(r.priority, []).append(r)
        conflicts = [
            {"priority": p, "rules": [r.key for r in rs]}
            for p, rs in sorted(by_prio.items(), reverse=True) if len(rs) > 1
        ]
        for c in conflicts:
            errs.append(self._error(
                "PRIORITY_CONFLICT",
                f"优先级 {c['priority']} 冲突：{', '.join(c['rules'])} 同时适用", op))
        top = max(by_prio) if by_prio else None
        effective = [r.key for r in by_prio[top]] if top is not None else []
        result = {
            "step": self.step,
            "facts": facts,
            "applicable": [r.key for r in applicable],
            "effective": effective,
            "conflicts": conflicts,
            "errors": [e["message"] for e in errs],
        }
        self.queries.append(result)
        return result

    # ---- 驱动 ----

    def run(self, doc: dict) -> None:
        for spec in doc.get("rules", []):
            self.step += 1
            before = self._snapshot()
            errs = self.define(spec, initial=True)
            self._record({"op": "define", "rule": spec}, before,
                         {"errors": [e["message"] for e in errs]} if errs else None)
        for op in doc.get("operations", []):
            self.step += 1
            before = self._snapshot()
            kind = op.get("op")
            errs: list[dict] = []
            extra = None
            if kind == "add":
                errs = self.define(op.get("rule", {}))
            elif kind == "modify":
                errs = self.modify(op.get("name", ""), op.get("rule", {}))
            elif kind == "abolish":
                errs = self.abolish(op.get("name", ""))
            elif kind == "query":
                result = self.query(op.get("facts", {}), op.get("names"))
                extra = {"query_index": len(self.queries)}
            else:
                errs = [self._error("UNKNOWN_OP", f"未知操作类型 {kind!r}", op)]
            if errs:
                extra = dict(extra or {}, errors=[e["message"] for e in errs])
            self._record(op, before, extra)


# ---------------------------------------------------------------- 输出

def render_text(engine: RuleEngine) -> str:
    out = []
    out.append("=" * 60)
    out.append("查询结果")
    out.append("=" * 60)
    if not engine.queries:
        out.append("（操作流中没有查询）")
    for i, q in enumerate(engine.queries, 1):
        out.append(f"\n[查询 #{i}] step={q['step']} facts={json.dumps(q['facts'], ensure_ascii=False)}")
        out.append(f"  适用规则: {', '.join(q['applicable']) or '（无）'}")
        out.append(f"  生效规则: {', '.join(q['effective']) or '（无）'}")
        for c in q["conflicts"]:
            out.append(f"  冲突: 优先级 {c['priority']} -> {', '.join(c['rules'])}")
        for e in q["errors"]:
            out.append(f"  错误: {e}")
    out.append("")
    out.append("=" * 60)
    out.append(f"错误清单（共 {len(engine.errors)} 条）")
    out.append("=" * 60)
    for e in engine.errors:
        out.append(f"  step={e['step']:<3} [{e['code']}] {e['message']}")
    if not engine.errors:
        out.append("  （无错误）")
    out.append("")
    out.append("=" * 60)
    out.append("演化历史（每步前后生效规则集）")
    out.append("=" * 60)
    for h in engine.history:
        detail = json.dumps(h["detail"], ensure_ascii=False)
        out.append(f"step {h['step']:<3} {h['op']:<8} {detail}")
        out.append(f"    before: {', '.join(h['before']) or '（空）'}")
        out.append(f"    after : {', '.join(h['after']) or '（空）'}")
        for e in h.get("errors", []):
            out.append(f"    error : {e}")
    return "\n".join(out)


def render_json(engine: RuleEngine) -> str:
    return json.dumps({
        "queries": engine.queries,
        "errors": engine.errors,
        "history": engine.history,
        "final_active": [r.as_dict() for r in engine.active_rules()],
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 内置示例

DEMO = {
    "rules": [
        {"name": "成人校验", "version": 1, "condition": "age >= 18", "priority": 10},
        {"name": "会员折扣", "version": 1, "condition": "vip", "priority": 5},
    ],
    "operations": [
        {"op": "add", "rule": {"name": "会员折扣", "version": 1,
                               "condition": "vip and age >= 18", "priority": 5}},  # 重复定义
        {"op": "query", "facts": {"age": 20, "vip": True}},
        {"op": "modify", "name": "成人校验",
         "rule": {"condition": "age >= 21", "priority": 10}},                      # -> v2，v1 废除
        {"op": "query", "facts": {"age": 20, "vip": True}, "names": ["成人校验", "幽灵规则"]},
        {"op": "add", "rule": {"name": "地区限制", "condition": "region == 'CN'", "priority": 10}},
        {"op": "query", "facts": {"age": 30, "vip": False, "region": "CN"}},        # 优先级冲突
        {"op": "abolish", "name": "地区限制"},
        {"op": "abolish", "name": "不存在的规则"},                                   # 废除不存在
        {"op": "query", "facts": {"age": 30, "vip": True}, "names": ["地区限制"]},
    ],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规则操作流演化与生效判定工具")
    parser.add_argument("input", nargs="?", help="输入 JSON 文件路径，'-' 表示 stdin")
    parser.add_argument("--demo", action="store_true", help="运行内置示例")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args(argv)

    if args.demo:
        doc = DEMO
    elif args.input == "-":
        doc = json.load(sys.stdin)
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            doc = json.load(f)
    else:
        parser.print_help()
        return 2

    engine = RuleEngine()
    engine.run(doc)
    print(render_json(engine) if args.json else render_text(engine))
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main())
