#!/usr/bin/env python3
"""rule_engine.py — 规则操作流演化与生效判定工具（纯标准库，单文件）。

用法:
    python3 rule_engine.py 操作文件        # 从文件读取操作流
    python3 rule_engine.py -              # 从标准输入读取
    python3 rule_engine.py --demo         # 运行内置示例（覆盖全部错误类型）

操作流语法（每行一条，# 开头为注释，空行忽略）:
    ADD     <规则名> v<版本> p<优先级> when <条件表达式>
    MODIFY  <规则名> v<版本> p<优先级> when <条件表达式>
    ABOLISH <规则名>
    QUERY   <规则名|*> [变量=值 ...]
    HISTORY

语义:
    * ADD     新增规则，即时生效；同名同版本重复定义报错。
    * MODIFY  新增一个版本并废除该规则所有旧版本；规则不存在时报错。
    * ABOLISH 废除该规则所有生效版本；规则不存在或无生效版本时报错。
    * 同名多版本同时生效时，按最高版本生效（新旧共存取新）。
    * QUERY *        对所有当前生效规则求值，输出适用规则（按优先级降序）。
    * QUERY <规则名> 只查询指定规则；规则名不存在时报错。
    * 两条及以上不同名规则同时适用且最高优先级相同 -> 冲突错误。
    * HISTORY 输出每步操作前后状态快照，演化历史可追溯。

条件表达式: Python 表达式（无内建函数），变量来自 QUERY 的 变量=值 参数，
例如:  amount > 100 and days <= 7 and level in ("vip", "svip")
"""

import ast
import re
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------- 数据模型

@dataclass
class Rule:
    name: str
    version: int
    priority: int
    condition: str
    active: bool = True
    born_step: int = 0
    dead_step: int = None  # 被废除时的步号

    def label(self):
        return "%s(v%d,p%d)" % (self.name, self.version, self.priority)


@dataclass
class Step:
    seq: int
    op: str
    before: dict
    after: dict
    errors: list = field(default_factory=list)


# ---------------------------------------------------------------- 引擎

class Engine:
    def __init__(self):
        self.rules = {}          # name -> [Rule, ...] 按版本升序
        self.errors = []         # [(step, msg)]
        self.history = []        # [Step]
        self.step_no = 0
        self.query_results = []  # [(step, query_line, [Rule], [errmsg])]

    # ---- 状态快照 ----
    def _snapshot(self):
        return {
            name: [(r.version, r.priority, "active" if r.active else "dead")
                   for r in versions]
            for name, versions in sorted(self.rules.items())
        }

    def _error(self, msg):
        self.errors.append((self.step_no, msg))
        return msg

    def _record(self, op, before, step_errors):
        self.history.append(Step(self.step_no, op, before,
                                 self._snapshot(), list(step_errors)))

    # ---- 操作 ----
    def add(self, name, version, priority, condition):
        before = self._snapshot()
        errs = []
        versions = self.rules.setdefault(name, [])
        if any(r.version == version for r in versions):
            errs.append(self._error(
                "重复定义: 规则 '%s' 版本 v%d 已存在" % (name, version)))
        else:
            versions.append(Rule(name, version, priority, condition,
                                 born_step=self.step_no))
            versions.sort(key=lambda r: r.version)
        self._record("ADD %s v%d" % (name, version), before, errs)

    def modify(self, name, version, priority, condition):
        before = self._snapshot()
        errs = []
        versions = self.rules.get(name)
        if not versions or not any(r.active for r in versions):
            errs.append(self._error(
                "修改失败: 规则 '%s' 不存在或无生效版本" % name))
        elif any(r.version == version for r in versions):
            errs.append(self._error(
                "重复定义: 规则 '%s' 版本 v%d 已存在" % (name, version)))
        else:
            for r in versions:          # 旧版本全部废除
                if r.active:
                    r.active = False
                    r.dead_step = self.step_no
            versions.append(Rule(name, version, priority, condition,
                                 born_step=self.step_no))
            versions.sort(key=lambda r: r.version)
        self._record("MODIFY %s v%d" % (name, version), before, errs)

    def abolish(self, name):
        before = self._snapshot()
        errs = []
        versions = self.rules.get(name)
        actives = [r for r in versions or [] if r.active]
        if not actives:
            errs.append(self._error("废除失败: 规则 '%s' 不存在或无生效版本" % name))
        else:
            for r in actives:
                r.active = False
                r.dead_step = self.step_no
        self._record("ABOLISH %s" % name, before, errs)

    def effective_rules(self):
        """每个规则名只取最高版本的生效规则（新旧共存按版本生效）。"""
        result = []
        for versions in self.rules.values():
            actives = [r for r in versions if r.active]
            if actives:
                result.append(max(actives, key=lambda r: r.version))
        return result

    def query(self, target, context):
        before = self._snapshot()
        errs, matched = [], []
        if target == "*":
            candidates = self.effective_rules()
        else:
            versions = self.rules.get(target)
            actives = [r for r in versions if r.active] if versions else []
            if not actives:
                errs.append(self._error(
                    "查询失败: 规则 '%s' 不存在或已废除" % target))
                candidates = []
            else:
                candidates = [max(actives, key=lambda r: r.version)]

        for rule in candidates:
            try:
                ok = bool(eval(rule.condition, {"__builtins__": {}}, dict(context)))
            except Exception as exc:
                errs.append(self._error(
                    "条件求值失败: %s 条件 '%s': %s"
                    % (rule.label(), rule.condition, exc)))
                continue
            if ok:
                matched.append(rule)

        matched.sort(key=lambda r: (-r.priority, r.name))
        if len(matched) >= 2:
            top = matched[0].priority
            tied = [r for r in matched if r.priority == top]
            if len(tied) >= 2:
                errs.append(self._error(
                    "规则冲突: %s 同时适用且优先级相同(p%d)"
                    % (", ".join(r.label() for r in tied), top)))

        self.query_results.append((self.step_no, target, matched, errs))
        self._record("QUERY %s" % target, before, errs)
        return matched, errs


# ---------------------------------------------------------------- 解析

ADD_RE = re.compile(r"^(ADD|MODIFY)\s+(\S+)\s+v(\d+)\s+p(-?\d+)\s+when\s+(.+)$")
ABOLISH_RE = re.compile(r"^ABOLISH\s+(\S+)$")
QUERY_RE = re.compile(r"^QUERY\s+(\S+)(.*)$")


def parse_value(text):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def parse_context(rest):
    ctx = {}
    for token in rest.split():
        if "=" not in token:
            raise ValueError("查询参数 '%s' 不是 变量=值 形式" % token)
        key, _, val = token.partition("=")
        ctx[key] = parse_value(val)
    return ctx


def run(engine, lines, out=print):
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        engine.step_no += 1
        out("\n[步骤 %d] %s" % (engine.step_no, line))

        m = ADD_RE.match(line)
        if m:
            op, name, ver, prio, cond = m.groups()
            if op == "ADD":
                engine.add(name, int(ver), int(prio), cond)
            else:
                engine.modify(name, int(ver), int(prio), cond)
        elif ABOLISH_RE.match(line):
            engine.abolish(ABOLISH_RE.match(line).group(1))
        elif QUERY_RE.match(line):
            qm = QUERY_RE.match(line)
            target, rest = qm.group(1), qm.group(2)
            try:
                ctx = parse_context(rest)
            except ValueError as exc:
                engine._error("步骤 %d 解析失败: %s" % (engine.step_no, exc))
                engine._record("QUERY(parse-error)", engine._snapshot(),
                               [engine.errors[-1][1]])
                continue
            matched, _ = engine.query(target, ctx)
            if matched:
                out("  生效规则: " + ", ".join(
                    "%s when(%s)" % (r.label(), r.condition) for r in matched))
            else:
                out("  生效规则: <无>")
        elif line == "HISTORY":
            print_history(engine, out)
        else:
            engine._error("步骤 %d 无法解析: %s" % (engine.step_no, line))
            engine._record("UNPARSED", engine._snapshot(), [engine.errors[-1][1]])


# ---------------------------------------------------------------- 报告

def _fmt_state(state):
    if not state:
        return "  (空)"
    parts = []
    for name, versions in state.items():
        vs = ", ".join("v%d/p%d/%s" % (v, p, s) for v, p, s in versions)
        parts.append("  %s: %s" % (name, vs))
    return "\n".join(parts)


def print_history(engine, out=print):
    out("  ---- 演化历史 (%d 步) ----" % len(engine.history))
    for st in engine.history:
        out("  步骤 %d: %s" % (st.seq, st.op))
        out("    前:\n" + "\n".join("  " + l for l in _fmt_state(st.before).splitlines()))
        out("    后:\n" + "\n".join("  " + l for l in _fmt_state(st.after).splitlines()))
        for e in st.errors:
            out("    错误: " + e)


def print_summary(engine, out=print):
    out("\n" + "=" * 60)
    out("查询结果汇总:")
    if not engine.query_results:
        out("  (无查询)")
    for step, target, matched, errs in engine.query_results:
        rules = ", ".join(r.label() for r in matched) or "<无>"
        out("  步骤 %d QUERY %s -> %s" % (step, target, rules))
    out("\n错误清单 (%d 条):" % len(engine.errors))
    if not engine.errors:
        out("  (无错误)")
    for step, msg in engine.errors:
        out("  [步骤 %d] %s" % (step, msg))


# ---------------------------------------------------------------- 示例

DEMO = """\
# ---- 基础新增 ----
ADD 退货规则 v1 p10 when amount > 100 and days <= 7
ADD 会员折扣 v1 p20 when level in ("vip", "svip")
ADD 大额审批 v1 p20 when amount > 1000
QUERY * amount=1500 days=3 level="vip"
# ---- 修改: 旧版本废除, 新版本生效 ----
MODIFY 退货规则 v2 p10 when amount > 100 and days <= 15
QUERY 退货规则 amount=150 days=10
# ---- 新旧版本共存: 直接 ADD v3 而不 MODIFY, 按最高版本生效 ----
ADD 退货规则 v3 p10 when amount > 50 and days <= 30
QUERY 退货规则 amount=80 days=20
# ---- 冲突: 两条规则同时适用且优先级相同 ----
ADD 新人礼 v1 p30 when days <= 1
ADD 首单礼 v1 p30 when days <= 1
QUERY * amount=10 days=1 level="new"
# ---- 各类错误 ----
ADD 会员折扣 v1 p99 when level == "svip"
ABOLISH 不存在的规则
QUERY 不存在的规则 amount=1
MODIFY 不存在的规则 v1 p1 when amount > 0
ABOLISH 首单礼
QUERY 首单礼 days=1
# ---- 历史追溯 ----
HISTORY
"""


def main(argv):
    if len(argv) >= 2 and argv[1] == "--demo":
        lines = DEMO.splitlines()
    elif len(argv) >= 2 and argv[1] == "-":
        lines = sys.stdin.read().splitlines()
    elif len(argv) >= 2:
        with open(argv[1], encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        print(__doc__)
        return 0
    engine = Engine()
    run(engine, lines)
    print_summary(engine)
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
