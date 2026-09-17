"""Conservative claim-to-authority checks for model-produced legal findings.

This layer does not decide whether a legal opinion is correct. It prevents a merely
retrieved authority from being displayed as if it directly supported a materially
different assertion.
"""
import re


CATEGORICAL_LEGAL_CLAIM = re.compile(
    r"违法|违反|不合法|不合规|不符合|不符|不一致|无效|效力待定|"
    r"可能无效|无法生效|不发生效力|不生效|效力(?:不确定|存疑|有争议)|"
    r"法律效力.{0,8}(?:不确定|存疑)|"
    r"(?:民法典|法律|法规|第.{0,8}条).{0,32}(?:存在差异|解释空间|存在争议)|"
    r"强制性规定|法定(?:要求|义务)|"
    r"必须具备|应当具备|(?:通常|一般)?要求.{0,12}具备.{0,12}(?:资质|许可|备案)|"
    r"不具备.{0,12}(?:法定)?资质|行政处罚|不得|禁止|缺少.{0,12}(?:资质|许可|备案)"
)

# When a claim invokes one of these specialised legal subjects, the authority must
# contain the same subject. A broad confidentiality or contract rule is not enough.
SPECIAL_SUBJECTS = (
    ("国家秘密或涉密资质", re.compile(r"国家秘密|涉密(?:资质|许可|人员|载体|信息系统)|保密资质"),
     re.compile(r"国家秘密|保守国家秘密|涉密(?:资质|许可|人员|载体|信息系统)|保密资质")),
    ("个人信息保护", re.compile(r"个人信息|敏感个人信息|数据出境|个人数据"),
     re.compile(r"个人信息|敏感个人信息|数据出境|个人数据")),
    ("医疗器械监管", re.compile(r"医疗器械|注册证|医疗器械.{0,12}(?:许可|备案)"),
     re.compile(r"医疗器械|注册证|药品监督管理|医疗器械.{0,12}(?:许可|备案)")),
    ("中小企业付款", re.compile(r"中小企业|小微企业|保障中小企业款项支付"),
     re.compile(r"中小企业|小微企业|保障中小企业款项支付")),
    ("产品质量赔偿责任", re.compile(r"产品质量法|产品质量.{0,16}(?:赔偿|免责|责任)"),
     re.compile(r"(?=.*产品质量)(?=.*(?:赔偿责任|承担责任|修理|更换|退货|索赔))", re.S)),
)

RISK_THEME_PATTERNS = {
    "penalty_rate": re.compile(r"违约金|滞纳金|损失.{0,8}(?:增加|减少)|第五百八十五|第六十五条|第六十六条"),
    "dingjin": re.compile(r"定金|第五百八十六|第五百八十七"),
    "dispute_clause": re.compile(r"仲裁|管辖|诉讼|人民法院"),
    "acceptance_window": re.compile(r"验收|检验|异议期间|检验期限"),
    "payment_terms": re.compile(r"付款|支付|账期|款项"),
    "delivery_time": re.compile(r"交付|交货|履行期限|完成时间"),
    "invoice": re.compile(r"发票|开票|税率|税额"),
    "quality": re.compile(r"质量|质保|瑕疵|检验标准"),
    "ip_ownership": re.compile(r"知识产权|专利|著作权|技术成果|成果归属"),
    "confidentiality": re.compile(r"保密|商业秘密|秘密信息"),
    "force_majeure": re.compile(r"不可抗力"),
    "termination": re.compile(r"解除|终止"),
    "liability_exclusion": re.compile(r"免责条款无效|不承担赔偿责任|赔偿责任|责任限制|人身损害|重大过失|修理|更换|退货"),
}

# 主题相同不等于具体命题被法条支持。以下结论必须由依据原文明确写到
# 同一个细节，避免把解除通知、情势变更等一般规定扩大成模型自行推导的结论。
CLAIM_DETAIL_GUARDS = (
    (
        re.compile(r"逾期未答复|(?:逾期|未答复).{0,8}视为(?:已)?同意|沉默.{0,10}(?:同意|认可)|默认同意|视为已同意"),
        re.compile(r"逾期未答复|(?:逾期|未答复).{0,8}视为(?:已)?同意|沉默.{0,10}(?:同意|认可)|默认同意|视为已同意"),
    ),
    (
        re.compile(r"情势变更.{0,24}(?:仲裁|争议解决).{0,16}(?:混同|冲突|障碍)|"
                   r"(?:仲裁|争议解决).{0,24}情势变更.{0,16}(?:混同|冲突|障碍)"),
        re.compile(r"情势变更.{0,24}(?:仲裁|争议解决)|"
                   r"(?:仲裁|争议解决).{0,24}情势变更"),
    ),
)


def _evidence_text(evidence):
    values = [evidence.get(key) for key in (
        "title", "citation", "excerpt", "content", "category", "article_or_clause"
    )]
    values.extend(evidence.get("tags") or [])
    return "\n".join(str(value) for value in values if value)


def _generic_concepts(evidence):
    concepts = []
    for value in evidence.get("tags") or []:
        value = str(value).strip()
        if len(value) >= 2:
            concepts.append(value)
    title = str(evidence.get("title") or "")
    concepts.extend(re.findall(r"[\u4e00-\u9fff]{2,8}", title))
    return concepts


def split_claim_evidence(claim, quote, risk_tag, evidence_by_id, refs):
    """Return (supporting, related, status, issues).

    ``claim_aligned`` means a deterministic subject check succeeded. It remains a
    review signal rather than a legal correctness guarantee.
    """
    claim = str(claim or "")
    subject = claim + "\n" + str(quote or "")
    categorical = bool(CATEGORICAL_LEGAL_CLAIM.search(claim))
    resolved = [(ref, evidence_by_id[ref]) for ref in refs if ref in evidence_by_id]
    if not resolved:
        return [], [], "unsupported" if categorical else "unsubstantiated", [
            "没有可定位且与结论对齐的法规依据，当前仅作为待核查线索"
        ]

    specialised = next(((label, evidence_pattern) for label, claim_pattern, evidence_pattern
                        in SPECIAL_SUBJECTS if claim_pattern.search(claim)), None)
    theme = RISK_THEME_PATTERNS.get(risk_tag)
    supporting, related = [], []
    for ref, evidence in resolved:
        corpus = _evidence_text(evidence)
        if specialised:
            aligned = bool(specialised[1].search(corpus))
        elif theme:
            aligned = bool(theme.search(corpus))
        else:
            aligned = any(concept in subject for concept in _generic_concepts(evidence))
        # “条款无效/不得免责”比一般的责任主题更具体，只有依据本身写明
        # 无效或不可排除的责任时才算直接对齐。
        if aligned and re.search(r"(?:免责条款|约定|条款).{0,12}(?:无效|不得免责)|(?:无效|不得免责).{0,12}(?:免责条款|约定|条款)", claim):
            aligned = bool(re.search(r"免责条款无效|不得免除|不承担民事责任的约定无效|造成对方人身损害|故意或者重大过失", corpus))
        if aligned and re.search(r"效力待定|(?:合同|条款).{0,8}无效|影响.{0,8}法律效力", claim):
            aligned = bool(re.search(r"效力待定|无效|不发生效力", corpus))
        for claim_pattern, evidence_pattern in CLAIM_DETAIL_GUARDS:
            if aligned and claim_pattern.search(claim) and not evidence_pattern.search(corpus):
                aligned = False
                break
        (supporting if aligned else related).append(ref)

    issues = []
    if specialised and not supporting:
        issues.append(f"引用依据没有直接涉及{specialised[0]}，不能支撑该专项法律结论")
    elif not supporting:
        issues.append("引用依据与结论只达到主题相关，不能作为该具体结论的直接依据")
    if related:
        issues.append("已将仅主题相关的材料移至相关依据：" + "、".join(related))
    status = "claim_aligned" if supporting else "unsupported" if categorical else "topic_related"
    return supporting, related, status, issues
