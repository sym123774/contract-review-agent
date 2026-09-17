"""
规则引擎 - 七大维度确定性检查
不依赖LLM；规则命中为可复现检查结果，法律适用仍需复核。
支持我方立场（甲方/乙方）：立场感知的风险等级与建议调整
"""
import re
from typing import List, Dict
from dataclasses import dataclass, field
from enum import Enum
from config import (PENALTY_DAILY_CAP_PCT, PENALTY_ANNUAL_CAP_PCT, LPR_ANNUAL_RATE,
                    PENALTY_ANNUAL_CAP_RATIO, PENALTY_ANNUAL_WARNING_PCT, PARTY_AUTO_DETECT)

_FULLWIDTH_NUMBER_MAP = str.maketrans('０１２３４５６７８９．％', '0123456789.%')


def _simple_cn_number(value):
    if value.isdigit():
        return float(value)
    digits = {'零':0, '〇':0, '一':1, '二':2, '三':3, '四':4, '五':5,
              '六':6, '七':7, '八':8, '九':9}
    if value == '十':
        return 10.0
    if '十' in value:
        left, right = value.split('十', 1)
        return float((digits.get(left, 1) * 10) + digits.get(right, 0))
    if all(ch in digits for ch in value):
        return float(''.join(str(digits[ch]) for ch in value))
    return None


def normalize_rate_notation(text):
    """统一全角数字、百分号、千分号和常见中文比例写法，仅供计算匹配。"""
    normalized = str(text or '').translate(_FULLWIDTH_NUMBER_MAP)
    normalized = re.sub(r'(\d+(?:\.\d+)?)\s*‰',
                        lambda m: f'{float(m.group(1)) / 10:g}%', normalized)
    denominators = {'百': 1, '千': 10, '万': 100}
    def convert(match):
        value = _simple_cn_number(match.group(2))
        return match.group(0) if value is None else f'{value / denominators[match.group(1)]:g}%'
    return re.sub(r'([百千万])分之([零〇一二三四五六七八九十\d]+)', convert, normalized)


class RiskLevel(str, Enum):
    HIGH = "高风险"
    MEDIUM = "中风险"
    LOW = "低风险"
    INFO = "提示"


# P0-2：结构化风险类型标签（去重用枚举）。法律路与商业路对同一风险点给出同一标签，
# 结果整合阶段按此标签合并，而非按标题/原文文本相似度匹配（两路措辞往往不一致）。
RISK_TYPE_UNDEFINED = "undefined"


@dataclass
class RiskItem:
    risk_id: str
    level: RiskLevel
    dimension: str
    title: str
    description: str
    clause_ref: str = None
    original_text: str = None
    legal_basis: str = None
    suggestion: str = None
    source: str = "rule"
    confidence: float = 1.0
    risk_type: str = "unknown"
    evidence: list = None
    replacement_clause_id: str = None
    review_status: str = "auto"
    # P0-2：结构化风险类型标签，结果整合阶段据此去重/合并
    risk_type_tag: str = RISK_TYPE_UNDEFINED
    # P0-2：商业影响说明（合并后卡片上独立保留，与 legal_basis 分开展示）
    commercial_impact: str = None
    evidence_status: str = 'not_checked'
    validation_issues: list = field(default_factory=list)
    related_clause_ids: list = field(default_factory=list)
    related_evidence: list = field(default_factory=list)


# ============ 我方立场支持 ============

# 付款方角色关键词（按合同惯用表述）
ROLE_PAYER = {"委托方", "采购方", "买方", "买受人", "发包方", "订购方", "需方", "客户", "受让方", "被许可方", "建设单位"}
# 收款方角色关键词
ROLE_PAYEE = {"受托方", "供应商", "供货方", "卖方", "出卖人", "承包方", "承揽方", "开发方", "研究开发方",
              "研发方", "服务方", "转让方", "许可方", "供方"}


def detect_party_roles(text: str) -> Dict[str, list]:
    """识别合同文本中 甲方/乙方 被赋予的角色，如"甲方（委托方）"、"乙方（受托方）" """
    roles = {"甲方": set(), "乙方": set()}
    for label in roles:
        for m in re.finditer(re.escape(label) + r"\s*(?:[（(:：]\s*)?([^\n。；|）)]{0,30})", text):
            found = [kw for kw in ROLE_PAYER | ROLE_PAYEE if kw in m.group(1)]
            for kw in found:
                if not any(kw != other and kw in other for other in found):
                    roles[label].add(kw)
    return {k: sorted(v) for k, v in roles.items()}


def build_party_context(text: str, our_side: str = None, we_pay: bool = None,
                        our_roles: List[str] = None) -> Dict:
    """构造立场上下文。our_side: '甲'=我方是甲方 / '乙'=我方是乙方 / None=中立不区分。
    we_pay: 我方是否为付款方（True/False/None=未判断）。
    立场一律以手动选择为准：默认不从合同文本自动识别角色（见 config.PARTY_AUTO_DETECT），
    只有显式传入 we_pay / our_roles 才会填充，未填就是"未判断"，规则引擎不据此调级。"""
    detected = detect_party_roles(text) if PARTY_AUTO_DETECT else {"甲方": [], "乙方": []}
    ctx = {"our_side": our_side, "our_roles": [], "other_roles": [], "we_pay": None,
           "detected": detected, "auto_detected": PARTY_AUTO_DETECT}
    if our_side not in ("甲", "乙"):
        return ctx
    our_label = "甲方" if our_side == "甲" else "乙方"
    other_label = "乙方" if our_side == "甲" else "甲方"
    ctx["our_label"] = our_label
    ctx["other_label"] = other_label
    ctx["our_roles"] = list(our_roles) if our_roles else ctx["detected"].get(our_label, [])
    ctx["other_roles"] = ctx["detected"].get(other_label, [])
    if we_pay is not None:          # 手动指定优先
        ctx["we_pay"] = bool(we_pay)
        return ctx
    payer = any(r in ROLE_PAYER for r in ctx['our_roles'])
    payee = any(r in ROLE_PAYEE for r in ctx['our_roles'])
    if payer and not payee:
        ctx["we_pay"] = True
    elif payee and not payer:
        ctx["we_pay"] = False
    return ctx


def _adjust(risk: RiskItem, adverse: bool, note: str):
    """按立场调整：adverse=该条款对我方不利（保持/提升等级）；有利则降为提示"""
    if adverse:
        risk.description = f"{risk.description}（立场分析：{note}，对我方不利）"
    else:
        risk.level = RiskLevel.INFO
        risk.description = f"{risk.description}（立场分析：{note}，对我方有利）"
        risk.suggestion = f"该条款当前倾向我方，注意对方可能要求修改。原建议：{risk.suggestion or '—'}"


def apply_party_perspective(risks: List[RiskItem], ctx: Dict) -> List[RiskItem]:
    """按我方立场调整风险等级与建议。we_pay=None（角色未识别）时不调级"""
    if not ctx or ctx.get("our_side") not in ("甲", "乙"):
        return risks
    we_pay = ctx.get("we_pay")
    our_label = ctx.get("our_label", "我方")
    roles_txt = "/".join(ctx["our_roles"]) if ctx["our_roles"] else "未识别"
    for r in risks:
        if r.risk_id.startswith("r_comm_001"):       # 预付款比例过高
            if we_pay is True:
                r.title = "预付款比例过高（我方为付款方）"
                r.level = RiskLevel.HIGH
                r.description = (f"{r.description}（立场分析：我方为{our_label}（{roles_txt}），即付款方，"
                                 "大额预付款的资金占用与对方违约风险由我方承担，对我方不利）")
                r.suggestion = "建议降低预付款比例或与里程碑挂钩、预留验收尾款"
            elif we_pay is False:
                _adjust(r, adverse=False, note=f"我方为{our_label}（{roles_txt}），即收款方，高预付款保障我方现金流")
        elif r.risk_id.startswith("r_comm_002") and we_pay is not None:   # 验收期限模糊
            _adjust(r, adverse=True,
                    note="我方为付款方，模糊验收期限使尾款支付时点不确定" if we_pay
                         else "我方为收款方，验收拖延直接影响回款")
        elif r.risk_id.startswith("r_perf_001") and we_pay is not None:   # 未约定履约担保
            _adjust(r, adverse=True,
                    note="我方为付款方，可主动要求对方提供履约担保" if we_pay
                         else "我方为收款方，对方可能要求我方提供担保，注意担保成本")
        elif r.risk_id.startswith("r_obj_002") and we_pay is not None:    # 缺少知识产权归属
            _adjust(r, adverse=True,
                    note="我方为委托方，应争取研发成果归属我方" if we_pay
                         else "我方为受托方，成果归对方时应保留背景知识产权与使用改进权利")
    return risks


def check_subject(text: str) -> List[RiskItem]:
    """维度1：主体资格"""
    risks = []
    known_pairs = [('甲方', '乙方'), ('买方', '卖方'), ('买受人', '出卖人'), ('出租人', '承租人'),
                   ('披露方', '接收方'), ('委托方', '受托方'), ('采购方', '供应方')]
    if not any(a in text and b in text for a, b in known_pairs):
        missing = []
        if "甲方" not in text: missing.append("甲方")
        if "乙方" not in text: missing.append("乙方")
        risks.append(RiskItem(
            risk_id="r_sub_001", level=RiskLevel.HIGH, dimension="主体资格",
            title="合同当事人不完整",
            description=f"未找到{'和'.join(missing)}信息",
            suggestion="请补充完整的当事人信息（名称、法定代表人、地址、联系方式）"
        ))
    if re.search(r'【[^】]*待[^】]*】|待定|待填写|待补充', text):
        risks.append(RiskItem(
            risk_id="r_sub_002", level=RiskLevel.MEDIUM, dimension="主体资格",
            title="当事人信息存在未填写内容",
            description="合同中发现占位符或待定标记",
            suggestion="请确认并填写所有当事人信息"
        ))
    blank_names = []
    for label in ('甲方', '乙方'):
        values = re.findall(rf'(?:^|\n)[ \t]*{label}[ \t]*[:：][ \t]*([^\n]*)', text)
        if values and not any(value.strip() for value in values):
            blank_names.append(label)
    if blank_names:
        risks.append(RiskItem(
            risk_id="r_sub_003", level=RiskLevel.HIGH, dimension="主体资格",
            title="合同主体名称未填写",
            description=f"{'、'.join(blank_names)}名称字段为空，无法据此确定实际签约主体",
            original_text='\n'.join(f'{label}：' for label in blank_names),
            suggestion="签署前填写双方完整法定名称，并核对统一社会信用代码、签约权限和盖章主体",
            risk_type="commercial"
        ))
    return risks


def check_object(text: str) -> List[RiskItem]:
    """维度2：标的客体"""
    risks = []
    object_kws = ["标的", "委托内容", "服务内容", "服务范围", "采购内容", "产品名称", "产品或服务", "技术要求", "租赁物", "房屋坐落", "保密信息"]
    if not any(kw in text for kw in object_kws):
        risks.append(RiskItem(
            risk_id="r_obj_001", level=RiskLevel.HIGH, dimension="标的客体",
            title="缺少合同标的约定",
            description="合同中未明确约定标的/委托内容/服务范围",
            suggestion="建议补充标的条款，明确规格、数量、技术参数等"
        ))
    # 知识产权归属（研发类合同重点）
    if any(kw in text for kw in ["委托研发", "技术开发", "合作开发"]) and \
       not any(kw in text for kw in ["知识产权", "专利", "成果归属", "技术秘密"]):
        risks.append(RiskItem(
            risk_id="r_obj_002", level=RiskLevel.HIGH, dimension="标的客体",
            title="委托研发合同缺少知识产权归属约定",
            description="涉及技术研发的合同未明确研发成果的知识产权归属",
            suggestion="建议明确约定专利申请权、技术秘密所有权及后续改进成果归属",
            legal_basis="《民法典》第八百五十九条：委托开发完成的发明创造，除法律另有规定或者当事人另有约定外，申请专利的权利属于研究开发人"
        ))
    return risks


# ============ P1-4 金额大小写转换 ============
_CN_DIGITS = {"零": 0, "〇": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}
_CN_STEP = {"拾": 10, "佰": 100, "仟": 1000}
_CN_WAN = {"万": 10000, "亿": 100000000}


def cn_number_to_int(s: str) -> int:
    """中文大写金额转数字。'壹拾贰万伍仟元整' -> 125000。支持 万/亿 分段。"""
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_STEP:
            unit = _CN_STEP[ch]
            section += (num if num else 1) * unit
            num = 0
        elif ch in _CN_WAN:
            if num:
                section += num
            total += section * _CN_WAN[ch]
            section, num = 0, 0
        # 其余字符（元/圆/整/正/角/分及标点）忽略
    return total + section + num


def _extract_small_amounts(text: str):
    """抽取带货币符号(¥$￥)或带'元/圆'的小写金额。返回 [(数值, 位置)]"""
    out = []
    # 货币符号直连数字：¥118000.00
    for m in re.finditer(r'[￥¥$]\s*([\d][\d,]*(?:\.\d+)?)', text):
        out.append((float(m.group(1).replace(",", "")), m.start()))
    # 数字紧跟 元/圆/整：94400元
    for m in re.finditer(r'([\d][\d,]*(?:\.\d+)?)\s*[元圆]', text):
        out.append((float(m.group(1).replace(",", "")), m.start()))
    out.sort(key=lambda x: x[1])
    return out


def check_amount_mismatch(text: str) -> List[RiskItem]:
    from decimal import Decimal
    risks = []
    big_re = re.compile(r'([零〇壹贰叁肆伍陆柒捌玖拾佰仟万亿]+)[元圆](?:([零〇壹贰叁肆伍陆柒捌玖])角)?(?:([零〇壹贰叁肆伍陆柒捌玖])分)?')
    for sent in re.split(r'[；;。\n]', text):
        smalls = _extract_small_amounts(sent)
        for m in big_re.finditer(sent):
            if not smalls:
                continue
            value = Decimal(cn_number_to_int(m.group(1)))
            value += Decimal(_CN_DIGITS.get(m.group(2), 0)) / 10
            value += Decimal(_CN_DIGITS.get(m.group(3), 0)) / 100
            small, pos = min(smalls, key=lambda pair: abs(pair[1] - m.start()))
            diff = abs(value - Decimal(str(small)))
            if diff > Decimal('0.005'):
                risks.append(RiskItem('r_term_amount_mismatch', RiskLevel.HIGH, '合同条款',
                    f'大写与小写金额不一致（差 {diff:,.2f} 元）',
                    f'大写金额为{value:,.2f}元，邻近小写为{small:,.2f}元，请确认实际约定。',
                    original_text=sent.strip(), suggestion='核对价款并统一大小写，以双方确认后的唯一金额为准。'))
    return risks


def check_penalty_rates(text: str) -> List[RiskItem]:
    risks = []
    period = re.compile(r'(每逾期一日|每日|每天|按日|每周|每月|按月|每年|按年)[^\d%\n；。]{0,25}?(\d+(?:\.\d+)?)\s*%')
    for sent in re.split(r'[；;。\n]', text):
        if not any(k in sent for k in ('违约', '滞纳', '罚息', '利息', '加收', '保管费')):
            continue
        # Normalize common Chinese fractional rates without altering the quoted source.
        normalized = normalize_rate_notation(sent)
        for match in period.finditer(normalized):
            label, rate = match.group(1), float(match.group(2))
            factor = 365 if ('日' in label or '天' in label) else 52 if '周' in label else 12 if '月' in label else 1
            annual = rate * factor
            if annual > PENALTY_ANNUAL_WARNING_PCT or (factor == 365 and rate > PENALTY_DAILY_CAP_PCT):
                risks.append(RiskItem(f'r_term_pen_{rate:g}_{factor}',
                    RiskLevel.HIGH if annual > 100 else RiskLevel.MEDIUM, '合同条款',
                    f'周期费用或违约金偏高（单期{rate:g}%，简单年化约{annual:g}%）',
                    '触发内部预警阈值，不代表超过统一法定上限；须核实计费基数、累计上限、实际损失及交易类型。',
                    original_text=sent.strip(), legal_basis='《民法典》第五百八十五条；合同编通则司法解释第六十五条。',
                    suggestion='区分正常费用、借贷利息和违约金，结合损失约定合理比例与累计上限；借贷另核合同成立时适用利率。',
                    review_status='needs_review'))
    return risks


def check_dingjin(text: str) -> List[RiskItem]:
    risks = []
    for sent in re.split(r'[。；\n]', text):
        normalized = normalize_rate_notation(sent)
        if '定金' in normalized and '非定金' not in normalized and '不适用定金' not in normalized:
            for m in re.finditer(r'(?:定金[^\n%]{0,24}?(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s*%[^\n。]{0,10}?定金)', normalized):
                pct = float(m.group(1) or m.group(2))
                if pct > 20:
                    risks.append(RiskItem('r_term_deposit_cap', RiskLevel.HIGH, '合同条款',
                        '约定定金比例超过主合同标的额20%', f'文本约定比例为{pct:g}%；超过部分不产生定金效力，应另行明确款项性质。',
                        original_text=sent, legal_basis='《民法典》第五百八十六条。',
                        suggestion='将具有定金效力的部分控制在法定范围，并与预付款、质保金分别列示。', review_status='needs_review'))
        mixed = '定金' in sent and '订金' in sent and not re.search(r'非定金|不适用定金', sent)
        unclear = '订金' in sent and not re.search(r'抵扣|退还|退回|返还|非定金|预付款', sent)
        if mixed or unclear:
            risks.append(RiskItem('r_term_dingjin', RiskLevel.MEDIUM, '合同条款',
                '订金、定金或保证金性质需要明确', '订金不能仅凭名称套用定金罚则；应核实是否混用及退款、抵扣条件。',
                original_text=sent, suggestion='分别约定各笔款项的性质、金额、是否计入总价及返还条件。', review_status='needs_review'))
    return risks


def check_delivery_timing(text: str) -> List[RiskItem]:
    risks = []
    for sent in re.split(r'[。；\n]', text):
        if re.search(r'尽快|尽早|及时|按需|另行通知', sent) and re.search(r'交货|发货|交付|到货|供货', sent):
            if not re.search(r'\d+\s*(?:个工作日|工作日|日|天|周|个月|月)内|20\d{2}年\d+月\d+日', sent):
                risks.append(RiskItem('r_term_delivery_vague', RiskLevel.MEDIUM, '合同条款',
                    '交货或履约期限不明确', '本条仅有模糊交付时间，请核对合同其他条款和进度附件。',
                    original_text=sent, suggestion='明确起算事件、期限、交付地点及延期责任。'))
    return risks


def check_invoice_terms(text: str) -> List[RiskItem]:
    """P1-6：发票条款要素（税率/开具时间）是否齐全。'后期开具'等模糊表述报警。"""
    risks = []
    tax_rate = re.compile(r'(增值税|税率|税额|专票|普票)\s*[^；;。\n]{0,20}?\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*%\s*(?:税率|增值税)')
    time_ok = re.compile(r'(?:开具|开票|提供发票|到票)[^；;。\n]{0,20}?(?:\d+\s*日|\d+\s*个?工作日|月?(?:底|内)|\d+\s*年|随货|货到)|(?:\d+\s*日|\d+\s*个?工作日|随货|货到)[^；;。\n]{0,10}?(?:开具|开票|提供发票)')
    for sent in re.split(r'[；;。\n]', text):
        if '发票' not in sent:
            continue
        if not re.search(r'开具|开票|提供发票', sent):
            continue
        normalized = normalize_rate_notation(sent)
        if re.search(r'开具|开票|税收|发票', sent) and not (tax_rate.search(normalized) and time_ok.search(sent)):
            missing = []
            if not tax_rate.search(sent):
                missing.append("税率/税额")
            if not time_ok.search(sent):
                missing.append("开具时间")
            risks.append(RiskItem(
                risk_id="r_term_invoice_missing", level=RiskLevel.MEDIUM, dimension="合同条款",
                title=f"发票条款缺少必要要素（{'、'.join(missing)}）",
                description=f"发票约定未包含{'、'.join(missing)}，『后期开具』等模糊表述影响税务合规与付款节点锁定",
                original_text=sent.strip()[:120],
                suggestion="建议明确发票类型（专票/普票）、税率，并约定开具时间（如'收到预付款后X个工作日内开具增值税专用发票'）",
            ))
    return risks


def check_terms(text: str) -> List[RiskItem]:
    """维度3：合同条款"""
    risks = []
    # 金额大小写
    cn_amount = re.findall(r'[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]+元', text)
    num_amount = re.findall(r'[￥¥]?\s*[\d,]+\.?\d*\s*元', text)
    if num_amount and not cn_amount:
        risks.append(RiskItem(
            risk_id="r_term_001", level=RiskLevel.MEDIUM, dimension="合同条款",
            title="金额缺少大写中文",
            description=f"发现小写金额（{num_amount[0]}），未找到对应大写中文金额",
            suggestion="建议同时标注大写和小写金额，防止篡改"
        ))
    # P1-4：金额大小写不一致检测（本金『壹拾贰万伍仟』vs 小写『¥118000』）
    risks.extend(check_amount_mismatch(text))
    # 违约金畸高（P1-5：LPR 4 倍可配置阈值）+ 原有简单百分比告警
    risks.extend(check_penalty_rates(text))
    # 空白字段
    if re.search(r'_{3,}|（\s*）|\[\s*\]', text):
        risks.append(RiskItem(
            risk_id="r_term_004", level=RiskLevel.MEDIUM, dimension="合同条款",
            title="存在未填写的空白字段",
            description="合同中发现下划线或空括号，可能是未完成的模板字段",
            suggestion="请确认并填写所有空白内容"
        ))
    # 必备条款缺失
    required = {
        "价款/报酬": ["价款", "报酬", "费用", "金额", "付款"],
        "履行期限": ["期限", "时间", "日期", "交付", "完成"],
        "违约责任": ["违约", "赔偿", "违约金", "逾期利息", "滞纳金", "罚息", "逾期责任"],
    }
    if any(x in text[:150] for x in ('保密协议', '保密合同', 'NDA', '无偿')):
        required.pop('价款/报酬', None)
    for name, kws in required.items():
        present = any(kw in text for kw in kws)
        # “逾期付款利息”“逾期交付所产生的利息”等常在“逾期”和“利息”之间插入行为词，
        # 不能只依赖连续关键词“逾期利息”。
        if name == "违约责任" and not present:
            present = bool(re.search(r"逾期.{0,12}(?:利息|责任|费用|损失)|(?:利息|罚息|滞纳金).{0,12}(?:支付|承担)", text))
            present = present or ("逾期" in text and any(k in text for k in ("利息", "责任", "赔偿", "费用", "损失", "罚款")))
        if not present:
            risks.append(RiskItem(
                risk_id=f"r_term_missing_{name}", level=RiskLevel.HIGH, dimension="合同条款",
                title=f"缺少必备条款：{name}",
                description=f"合同中未发现「{name}」相关约定",
                suggestion=f"建议补充{name}条款"
            ))
    # P0-3：订金 / 定金 概念区分
    risks.extend(check_dingjin(text))
    # P1-6：关键条款完整性清单扩展（交货时间 / 发票要素）
    risks.extend(check_delivery_timing(text))
    risks.extend(check_invoice_terms(text))
    for sent in re.split(r'[。；;\n]', text):
        if re.search(r'(?:逾期|未在).{0,20}(?:未答复|不答复).{0,12}视为(?:已)?同意|'
                     r'(?:未答复|不答复|沉默).{0,12}视为(?:已)?同意', sent):
            risks.append(RiskItem(
                risk_id="r_term_silent_consent", level=RiskLevel.MEDIUM, dimension="合同条款",
                title="沉默视为同意的变更或解除机制需明确",
                description="未在约定期限答复会直接触发同意后果，双方都可能因通知遗漏而被动承担合同变化",
                original_text=sent.strip(), risk_type="commercial",
                suggestion="改为书面明确确认后生效，并约定通知地址、送达方式、签收证据和答复期限"
            ))
    return risks


def check_legality(text: str) -> List[RiskItem]:
    """维度4：合法性合规性"""
    risks = []
    if any(kw in text for kw in ["医疗器械", "医用", "临床", "注册证", "体外诊断"]):
        if not any(kw in text for kw in ["资质", "许可证", "注册证", "备案", "GMP", "生产许可"]):
            risks.append(RiskItem(
                risk_id="r_leg_001", level=RiskLevel.HIGH, dimension="合法性合规性",
                title="医疗器械合同缺少资质要求",
                description="涉及医疗器械但未约定对方需具备相应的生产/经营许可证或产品注册证",
                suggestion="建议增加资质保证条款，要求对方提供并维持有效的医疗器械相关资质",
                legal_basis="《医疗器械监督管理条例》"
            ))
    return risks


def check_commercial(text: str) -> List[RiskItem]:
    risks = []
    for sent in re.split(r'[。；;\n]', text):
        for fragment in re.split(r'[，,](?!\d{3}(?:\D|$))', sent):
            if '预付款' not in fragment:
                continue
            normalized = normalize_rate_notation(fragment)
            m = re.search(r'预付款[^%\n\d]{0,24}(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s*%[^\n，]{0,12}(?:作为|为)预付款', normalized)
            if m:
                pct = float(m.group(1) or m.group(2))
                if pct > 50:
                    risks.append(RiskItem('r_comm_001', RiskLevel.MEDIUM, '商业风险',
                        '预付款比例过高', f'预付款为{pct:g}%，触发内部商业风险预警；不属于法定比例限制。',
                        original_text=fragment, risk_type='commercial',
                        suggestion='付款方可争取降低预付、设置里程碑、退款机制和履约保障。'))
        if re.search(r'及时验收|尽快验收|无异议视为合格', sent) and not re.search(r'\d+\s*(?:个工作日|工作日|日|天)', sent):
            risks.append(RiskItem('r_comm_002', RiskLevel.MEDIUM, '商业风险', '验收期限不明确',
                '本条验收时限模糊，需核对其他验收约定；延期和过早默示验收可能影响双方。',
                original_text=sent, risk_type='commercial', suggestion='明确验收期限、测试项目、不合格处理与异议程序。'))
    return risks


def check_performance(text: str) -> List[RiskItem]:
    """维度6：履约能力"""
    risks = []
    if not any(kw in text for kw in ["担保", "保证", "履约保证金", "保函", "定金"]):
        risks.append(RiskItem(
            risk_id="r_perf_001", level=RiskLevel.INFO, dimension="履约能力",
            title="未约定履约担保",
            description="合同未约定履约保证金、保函或其他担保措施",
            suggestion="对于大额或高风险合同，建议要求对方提供履约担保"
        ))
    return risks


def check_dispute(text: str) -> List[RiskItem]:
    risks = []
    for sent in re.split(r'[。\n]', text):
        both = '仲裁' in sent and re.search(r'诉讼|起诉|人民法院', sent)
        choice = re.search(r'或者|同时可|亦可|也可|或向|或提起', sent)
        if both and choice and not re.search(r'保全|执行|撤销裁决', sent):
            risks.append(RiskItem('r_disp_001', RiskLevel.HIGH, '争议解决',
                '仲裁与诉讼约定可能冲突', '同一条款同时提供仲裁及起诉路径，仲裁约定效力需结合完整文本、适用法律及程序行为复核。',
                original_text=sent, legal_basis='《最高人民法院关于适用〈仲裁法〉若干问题的解释》第七条。',
                suggestion='明确主争议解决机制，区分仲裁、保全、执行及司法审查事项。', review_status='needs_review'))
    if not re.search(r'争议|管辖|仲裁|诉讼|法院', text):
        risks.append(RiskItem('r_disp_002', RiskLevel.MEDIUM, '争议解决', '未检出争议解决约定',
            '规则未识别到争议解决方式；缺少约定不等于合同无效。', suggestion='约定可执行的争议解决方式及相应机构。'))
    return risks


def run_rule_engine(parsed: Dict, party: Dict = None) -> List[RiskItem]:
    """运行全部规则检查。party=build_party_context() 的立场上下文，None 时保持中立"""
    text = parsed["full_text"]
    all_risks = []
    all_risks.extend(check_subject(text))
    all_risks.extend(check_object(text))
    all_risks.extend(check_terms(text))
    all_risks.extend(check_legality(text))
    all_risks.extend(check_commercial(text))
    all_risks.extend(check_performance(text))
    all_risks.extend(check_dispute(text))
    all_risks.extend(check_party_terms(text))
    all_risks.extend(check_template_selections(parsed))
    from core.clause_checks import check_structured_clauses
    all_risks.extend(check_structured_clauses(parsed))
    if party:
        all_risks = apply_party_perspective(all_risks, party)
    # Preserve traceability without inventing a clause when a rule only checks absence.
    for i, risk in enumerate(all_risks):
        if not risk.clause_ref and risk.original_text:
            needle = re.sub(r'\s+', '', risk.original_text)
            for clause in parsed.get('clauses', []):
                hay = re.sub(r'\s+', '', clause['title'] + '\n' + clause['content'])
                if needle and needle in hay:
                    risk.clause_ref = clause['clause_id']
                    break
        if risk.legal_basis:
            risk.review_status = 'needs_review'
        risk.risk_id = f'{risk.risk_id}_{risk.clause_ref or "document"}_{i:03d}'
    return all_risks


def check_template_selections(parsed: Dict) -> List[RiskItem]:
    """Turn visible unselected form choices into stable factual risks."""
    risks = []
    for clause in parsed.get('clauses', []):
        text = clause.get('title', '') + '\n' + clause.get('content', '')
        blank_index = re.search(r'(?:以下|下列|按).{0,20}第?[_＿]{2,}项', text)
        inline_choices = (re.search(r'(?:以下|下列).{0,20}(?:任选|选择).{0,8}(?:一种|一项)', text)
                          and re.search(r'方式一|选项一', text) and re.search(r'方式二|选项二', text))
        selected = re.search(r'[√✓☑☒]|(?:选择|采用|按)第?[一二12]项|(?:选择|采用)方式[一二12]', text)
        if not (blank_index or inline_choices) or selected:
            continue
        if re.search(r'专利|知识产权|成果归属|申请权', text):
            title, level, risk_type = '知识产权归属选项未选择', RiskLevel.HIGH, 'template_deviation'
        elif re.search(r'付款|支付', text):
            title, level, risk_type = '付款方式选项未选择', RiskLevel.MEDIUM, 'template_deviation'
        else:
            title, level, risk_type = '模板选项未选择', RiskLevel.MEDIUM, 'template_deviation'
        risks.append(RiskItem(
            'r_template_selection', level, '合同条款', title,
            '合同保留多个备选方案，但未见明确勾选或填写，签署前需确定唯一生效内容。',
            clause_ref=clause.get('clause_id'), original_text=text.strip(), risk_type=risk_type,
            suggestion='删除未采用方案，并明确填写或勾选唯一生效选项。'))
    return risks


def check_party_terms(text: str) -> List[RiskItem]:
    risks = []
    for sent in re.split(r'[。；\n]', text):
        for m in re.finditer(r'(?<!\d)(\d{2,4})\s*(?:个\s*)?(工作日|日|天)(?:之?内|以内|后)?\s*(?:支付|付清|结清|付款)', sent):
            days = int(m.group(1))
            if days > 60:
                risks.append(RiskItem(f'r_party_pay_{days}', RiskLevel.MEDIUM, '商业风险',
                    f'付款周期长达{days}{m.group(2)}',
                    '长账期可能影响收款方现金流。若属于机关、事业单位或大型企业向中小企业采购，需进一步核对条例第九条的适用条件、起算点及约定例外。',
                    original_text=sent, legal_basis='《保障中小企业款项支付条例》（2025年修订）第九条。',
                    suggestion='核实双方主体规模和采购性质，明确账期及逾期责任；工作日与自然日分别计算。',
                    risk_type='commercial', review_status='needs_review'))
    m = re.search(r'(?:知识产权|专利申请权|研究成果)[^。]{0,20}?归\s*(甲方|乙方)', text)
    if m:
        risks.append(RiskItem('r_party_ip_owner', RiskLevel.INFO, '标的客体',
            f'知识产权约定归{m.group(1)}', '应结合背景知识产权、开发成果、许可范围及第三方权利分别复核。',
            original_text=m.group(), suggestion='明确归属、使用许可、移交协助及第三方组件的责任。', risk_type='commercial'))
    return risks


def risk_to_dict(risk: RiskItem) -> Dict:
    from dataclasses import asdict
    d = asdict(risk)
    d["level"] = risk.level.value
    d["severity"] = risk.level.value
    d["clause_id"] = risk.clause_ref
    d["evidence"] = risk.evidence or []
    if d.get("risk_type") == "unknown":
        d["risk_type"] = "legal" if risk.legal_basis else "commercial"
    return d


# ============ P0-2：结构化风险类型标签（跨路去重依据）============
# LLM 法律路与商业路对同一风险点的措辞往往不同（如标题分别是"违约金过高"和"滞纳金畸高"），
# 靠文本相似度无法合并。这里用确定性关键词分类器把每条风险归到规范标签（枚举），
# 结果整合阶段对 (risk_type_tag, dimension) 相同的跨路风险项合并为一条。

def tag_by_risk(title, description, dimension="") -> str:
    """将风险标题+描述归类为规范标签；无法归类（未知/非典型）返回 RISK_TYPE_UNDEFINED。"""
    headline = title or ""
    text = f"{headline}\n{description or ''}"
    def has(*words):
        return any(w in text for w in words)
    # 金额专用：涉及金额表述矛盾/大小写不一致
    if has("大小写") or (has("金额", "价款", "总价", "对价") and has("不一致", "矛盾", "差异", "错误")):
        return "amount_mismatch"
    if has("空白字段", "未填写", "未填完整", "字段为空白", "留空") and has(
            "账号", "账户", "开户行", "签署", "盖章", "签字", "日期", "空白", "字段"):
        return "blank_fields"
    if has("合同标的", "委托内容", "服务范围", "项目内容") and has("缺少", "未明确", "不明", "未约定"):
        return "contract_scope"
    # 订金/定金（必须优先于"定金"泛匹配）
    if has("订金"):
        return "dingjin"
    # 违约金/滞纳金/罚息（含比例或畸高判词）。须在 dispute 之前——
    # 违约金描述的 legal_basis 常含"仲裁机构"字样，若先判 dispute 会误标。
    if has("违约金", "滞纳金", "罚息", "滞纳", "逾期付款利息", "逾期利息") and has("%", "比例", "畸高", "过高", "每天", "每日"):
        return "penalty_rate"
    if has("口头约定", "口头协商") and has("效力", "举证", "书面", "争议"):
        return "oral_agreement"
    if has("研发失败", "开发失败", "技术开发失败") and has("风险", "责任", "报酬", "损失", "解除"):
        return "development_failure"
    # 争议解决：需明确的纠份解决框架（争议解决/管辖/或裁或诉/仲裁+诉讼同现），
    # 仅出现"仲裁机构/法院"（可能只是依据或描述里的措辞）不触发。
    if has("争议解决", "或裁或诉", "管辖") or (has("仲裁") and has("诉讼")):
        return "dispute_clause"
    # 标题明确写交货/交付时间时，以标题事实为准，避免描述中的“影响验收”把它误分为验收风险。
    if any(word in headline for word in ("交货", "发货", "交付", "供货", "到货", "履约时间")) and any(
            word in headline for word in ("时间", "期限", "模糊", "明确", "尽快")):
        return "delivery_time"
    if has("付款方式", "支付方式") and has("未选择", "未明确", "选项未定", "自拟", "空白"):
        return "payment_selection"
    # 验收（须在 payment_terms 之前，避免"验收不合格后付款期限…"被下面吞掉）
    if has("验收") and has("期限", "不明确", "模糊", "未约定", "窗口", "满意"):
        return "acceptance_window"
    if has("默示验收", "视为验收合格", "视同验收合格"):
        return "acceptance_window"
    # 付款周期/账期（明确"超过/过长"判词；凡涉验收优先归验收）
    if has("付款", "账期") and has("过长", "超过", "60日", "周期") and not has("验收"):
        return "payment_terms"
    # 预付款
    if has("预付款") and has("过高", "比例"):
        return "prepay_ratio"
    # 知识产权必须先于交付判断；“交付成果侵权”不是交货时间风险。
    if has("知识产权", "专利", "著作权", "商标", "第三人权利", "侵权"):
        return "ip_ownership"
    # 交货/履约时间
    if has("交货", "发货", "交付", "供货", "到货", "履约时间") and has("时间", "期限", "模糊", "明确", "尽快"):
        return "delivery_time"
    # 发票
    if has("发票") and has("税率", "开具", "开票", "要素", "缺少", "专票", "普票"):
        return "invoice"
    if has("固定总价", "总金额不得", "总价不得") and has("变更", "调整", "调价", "价格"):
        return "price_adjustment"
    # 履约担保
    if has("履约担保", "履约保证金", "保函", "担保") and has("未约定", "缺少", "缺失"):
        return "performance_guarantee"
    # IPO/质量标准
    if has("质量", "质保", "验收标准", "合格") and has("标准", "缺少", "未约定", "不明确"):
        return "quality"
    # 必备条款缺失
    if has("必备条款", "缺少", "缺失", "未约定", "未发现") and has("价款", "履行期限", "违约责任", "争议解决"):
        return "missing_clause"
    # 保密 / 不可抗力 / 解除终止 / 责任限额（常见商业与法律双路同报项）
    if has("保密", "商业秘密", "国家秘密", "涉密"):
        return "confidentiality"
    if has("违约责任", "法律责任", "责任追究") and has("模糊", "不明", "缺乏", "未明确", "救济"):
        return "liability_terms"
    if has("解除", "终止", "变更") and has("权", "条件", "单方", "默示同意", "视为已同意", "通知"):
        return "termination"
    if has("不可抗力"):
        return "force_majeure"
    if (((has("免责", "免除", "不承担任何") and has("责任", "赔偿", "损失", "质量"))) or
            (has("责任", "赔偿", "限额") and has("限额", "上限", "限制"))):
        return "liability_exclusion"
    return RISK_TYPE_UNDEFINED



