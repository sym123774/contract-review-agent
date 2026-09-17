"""Arithmetic and clause-level warning checks. Legal interpretation remains reviewable."""
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from core.rules import RiskItem, RiskLevel, normalize_rate_notation


def number(text):
    m = re.search(r'-?\d[\d,]*(?:\.\d+)?', str(text))
    return Decimal(m.group().replace(',', '')) if m else None


def cn_int(text):
    if text.isdigit():
        return int(text)
    digits = {'零':0,'〇':0,'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'两':2}
    total, n = 0, 0
    for ch in text:
        if ch in digits:
            n = digits[ch]
        elif ch in {'十':10,'百':100,'千':1000}:
            total += (n or 1) * {'十':10,'百':100,'千':1000}[ch]
            n = 0
    return total + n


def check_structured_clauses(parsed):
    risks = []
    def add(code, clause, title, detail, suggestion, level=RiskLevel.MEDIUM, basis=None, related=None):
        risks.append(RiskItem('r_' + code, level, '合同条款', title, detail,
            clause_ref=clause['clause_id'], original_text=clause['title']+'\n'+clause['content'],
            suggestion=suggestion, legal_basis=basis, review_status='needs_review' if basis else 'auto',
            risk_type='legal' if basis else 'commercial', related_clause_ids=related or []))

    total = None
    for c in parsed['clauses']:
        for row in c.get('table_rows', []):
            if row and re.search(r'合同总价|合同总额', row[0]):
                # Currency values after a label, not years or percentages.
                values = re.findall(r'[¥￥]?\s*(\d[\d,]*(?:\.\d+)?)', ' '.join(row[1:]))
                if values:
                    total = Decimal(values[-1].replace(',', ''))
                    break
        if total is not None:
            break

    for c in parsed['clauses']:
        rows = c.get('table_rows', [])
        if len(rows) < 2:
            continue
        header = rows[0]
        def col(pattern):
            return next((i for i,h in enumerate(header) if re.search(pattern,h)), None)
        qty, unit, amount, ratio = col('数量'), col('单价'), col('金额|小计'), col('比例')
        actual_lines, declared_totals, percentages = [], [], []
        for row in rows[1:]:
            is_total = any(re.fullmatch(r'\s*(?:合计|总计|总额)\s*', v) for v in row)
            if amount is not None and amount < len(row):
                amt = number(row[amount])
                if amt is not None:
                    (declared_totals if is_total else actual_lines).append(amt)
                if qty is not None and unit is not None and max(qty,unit) < len(row) and not is_total:
                    q, price = number(row[qty]), number(row[unit])
                    if q is not None and price is not None and amt is not None and abs(q*price-amt) > Decimal('.01'):
                        add('table_line_amount', c, '数量乘单价与行金额不一致',
                            f'行“{row[0]}”：{q} × {price} = {q*price:,.2f}元，列示{amt:,.2f}元。',
                            '核对计价单位、优惠、税额及单价，不能直接认定应改哪一个金额。', RiskLevel.HIGH)
            if ratio is not None and ratio < len(row) and not is_total and any(s in row[ratio] for s in ('%', '％')):
                pct = number(normalize_rate_notation(row[ratio]))
                if pct is not None:
                    percentages.append(pct)
                    if total and amount is not None and amount < len(row):
                        amt = number(row[amount])
                        expected = (total*pct/100).quantize(Decimal('.01'))
                        if amt is not None and abs(expected-amt)>Decimal('.01'):
                            add('payment_amount_ratio', c, '付款节点金额与比例不一致',
                                f'以首页合同总价{total:,.2f}元为基数，{row[0]}的{pct}%应为{expected:,.2f}元，表列{amt:,.2f}元。',
                                '先确认计费基数，再统一付款比例和节点金额。', RiskLevel.HIGH)
        if qty is not None and unit is not None and declared_totals and actual_lines:
            subtotal = sum(actual_lines)
            for declared in declared_totals:
                if abs(subtotal-declared)>Decimal('.01'):
                    add('table_total_amount', c, '明细金额合计与列示总额不一致',
                        f'行金额合计{subtotal:,.2f}元，列示合计{declared:,.2f}元，差额{subtotal-declared:,.2f}元。',
                        '核实折扣、税额是否单列，复算明细和合同总价。', RiskLevel.HIGH)
        if percentages and re.search(r'付款|节点|预付|到货款|验收款', ' '.join(v for r in rows for v in r)):
            pct_total = sum(percentages)
            if pct_total != 100:
                add('payment_ratio_total', c, '付款节点比例合计不等于100%',
                    f'本付款表比例合计{pct_total}%。若包含替代节点、扣留款或不同计费基数，应明确避免重复计算。',
                    '明确互斥或累计关系；通常以同一总价为基数的全套付款节点合计应为100%。', RiskLevel.HIGH)

    headings = []
    for c in parsed.get('heading_clauses', parsed['clauses']):
        m = re.match(r'^第([一二三四五六七八九十百\d]+)条', c['title'])
        if m:
            headings.append((cn_int(m.group(1)), c))
    seen = {}
    for n,c in headings:
        if n in seen:
            add('duplicate_clause_number', c, f'条款编号重复：第{n}条',
                '同一编号对应不同条款，交叉引用可能指向错误内容。', '重新编号并同步更新全文引用。',
                related=[seen[n]['clause_id']])
        else:
            seen[n] = c
    if headings and max(seen) < 100:
        gaps = sorted(set(range(1,max(seen)+1))-set(seen))
        if gaps:
            add('clause_number_gap', headings[-1][1], '条款主编号存在跳号',
                f'未发现主编号：{gaps}。跳号不等同于内容缺失，需要核对目录及完整版本。', '核对是否遗漏页面或仅编号错误。', RiskLevel.LOW)

    for c in parsed['clauses']:
        text = c['title']+'\n'+c['content']
        m = re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日.*?(?:至|到)(20\d{2})年(\d{1,2})月(\d{1,2})日.*?共([一二三四五六七八九十\d]+)年', text)
        if m:
            try:
                start, end = date(*map(int,m.groups()[:3])), date(*map(int,m.groups()[3:6]))
                years = cn_int(m.group(7))
                if end < start or abs((end-start).days+1-years*365.25)>3:
                    add('term_duration', c, '服务起止日期与约定年限不一致',
                        f'起止日期为{start}至{end}，文本称共{years}年。', '确认实际服务年限、起算日期和到期日期。')
            except ValueError:
                add('invalid_date', c, '存在无效日历日期', '条款中日期不能构成有效日历日期。', '核对年月日。')
        m = re.search(r'一式([一二三四五六七八九十\d]+)份.*?甲方持([一二三四五六七八九十\d]+)份.*?乙方持([一二三四五六七八九十\d]+)份', text)
        if m and cn_int(m[1]) != cn_int(m[2])+cn_int(m[3]):
            add('copies_count', c, '合同份数与双方持有数量不一致', '总份数与甲乙双方所持份数之和不相等。', '统一总份数和各方留存数量。', RiskLevel.LOW)

    # Each check describes the observed wording, rather than claiming a legal conclusion is settled.
    patterns = [
        ('license_late', r'(?:注册证|许可证|资质|相关证明).{0,55}(?:履行完成后|交付后|补交|日前取得)|不得.{0,20}未取得注册证.{0,15}拒收', '资质取得或提交时间晚于履约要求', '需核实器械类别、实际注册/备案及经营资质，不能只凭合同声明确认合法交付。', '在采购、交付和服务开展前核验适用资质；明确持续有效义务及不满足时的处理。', '《医疗器械监督管理条例》及相应注册、生产、经营规定。'),
        ('liability_exemption', r'(?:故意|重大过失|人身伤亡|人身损害).{0,70}(?:不承担|免责)|(?:误诊|漏诊).{0,45}全部责任', '免责或责任转移范围需要审查', '文字涉及故意、重大过失、人身损害或产品服务责任转移，不能仅以双方约定排除法定责任。', '区分产品、服务、使用行为责任，为不可排除的责任设置例外并核对保险安排。', '《民法典》第五百零六条。'),
        ('liability_scope_broad', r'(?:任何情况下).{0,100}(?:不承担任何赔偿责任|全部免责)|(?:质量问题|产品缺陷|设备故障).{0,80}(?:不承担任何赔偿责任|全部免责)', '绝对免责范围过宽，需要逐项收窄', '“任何情况下均不承担赔偿责任”的绝对表述可能覆盖人身损害、故意或重大过失以及依法应承担的产品责任；具体无效范围仍需按损害类型和责任主体复核。', '删除绝对免责表述，分别约定质量修理、更换、退货和损失赔偿，并明确人身损害、故意或重大过失等不可免责情形。', '《民法典》第五百零六条；《产品质量法》第四十条、第四十一条、第四十三条。'),
        ('data_crossborder', r'(?:原始病历|个人信息|患者数据).{0,40}(?:境外|美国)|境外.{0,60}(?:无需|不需)', '患者或个人信息跨境安排缺少前置核实', '需核实处理者角色、数据类别、人数、合法性基础及适用跨境机制；不能把合同中的豁免声明直接当作有效豁免。', '出境前完成适用机制、告知同意及影响评估核查，必要时先停止该项数据传输。', '《个人信息保护法》第十三条、第三十八条、第三十九条、第五十五条。'),
        ('data_rights', r'无需.{0,25}(?:响应|核验同意)|任何形式的数据利用', '个人信息使用范围或个人权利安排过宽', '合同不能替代对实际处理目的、合法性基础和个人权利响应机制的核实。', '列明用途与数据范围，区分履约、训练和营销，建立查阅、更正、删除及撤回同意的处理程序。', '《个人信息保护法》第十三条、第四十四条至第四十七条。'),
        ('data_security', r'共享管理员密码|关闭访问日志|个人网盘复制生产数据', '数据安全措施含明显薄弱安排', '共享权限、关闭审计或个人网盘复制会削弱访问控制、追溯和数据保护。', '使用个人账户、最小权限、访问审计和受控传输，按职责约定安全义务。', '《个人信息保护法》第五十一条、第五十九条。'),
        ('data_incident', r'(?:泄漏|泄露).{0,50}(?:30个工作日|全权决定)|是否通知监管.{0,30}全权决定', '数据事件通知条款可能延误法定义务', '固定长等待期或由受托方单方决定是否通知，不能替代依法及时补救、通知及例外判断。', '约定立即报告和协同处置，分别判断对监管部门、个人的通知义务及适用例外。', '《个人信息保护法》第五十七条。'),
        ('data_retention_conflict', r'删除.{0,40}(?:同时|但).{0,30}(?:永久保留|保留数据)|永久保存.{0,30}立即删除', '数据删除与保留约定冲突', '同一数据存在删除和继续保留的相反安排，需区分受托数据、依法留存资料和匿名化成果。', '列出留存法定依据、期限、用途限制及删除验证，终止后的受托数据依法返还或删除。', '《个人信息保护法》第十九条、第二十一条、第四十七条。'),
        ('data_publicity', r'(?:宣传|案例展示|投标).{0,70}(?:患者数据|个人信息).{0,40}(?:无需|不需)', '宣传展示涉及个人信息授权', '合同中的概括授权不能代替个人信息公开所需条件。', '患者信息公开与公司名称宣传分别处理；优先使用不可识别个人的展示材料。', '《个人信息保护法》第二十五条。'),
        ('subcontract_release', r'(?:转包|分包|转委托).{0,70}(?:无关|不承担)|未获.{0,15}同意.{0,40}数据处理', '转委托及第三方责任安排过宽', '第三方处理资质、授权和原合同责任缺少约束。', '约定事先授权、资质审查、等效保护和原履约方责任，数据转委托单独核实。', '《个人信息保护法》第二十一条；《民法典》第五百九十三条。'),
        ('unilateral_change', r'(?:随时|单方).{0,20}(?:调价|修改合同)|(?:邮件|公告|口头通知).{0,20}单方修改', '单方调价或变更机制需要约束', '合同价格或内容可能未经双方明确同意发生变化。', '明确触发条件、证据、协商程序、书面确认及无法达成一致时的退出安排。', '《民法典》第五百四十三条。'),
        ('termination_exclusion', r'根本违约.{0,15}不得解除|随时无理由解除.{0,20}不退还', '解除权与退款约定明显失衡', '一方保留任意解除权且排除另一方救济，须结合合同性质和法定解除条件判断。', '明确重大违约、催告、解除、结算和已付款退还机制。', '《民法典》第五百六十三条、第五百六十六条。'),
        ('acceptance_hidden', r'(?:\d+小时|投入试用).{0,70}(?:隐蔽缺陷|放弃任何质量异议)|视为.{0,20}(?:性能|软件安全).{0,15}验收合格', '默示验收可能过早覆盖性能及隐蔽缺陷', '短时外观验收不能自然替代性能、安全及潜在缺陷的检验。', '区分到货、安装、试运行和最终验收，保留隐蔽缺陷与质保救济。', '《民法典》第六百二十一条、第六百二十二条。'),
        ('force_majeure_scope', r'不可抗力包括.{0,100}(?:资金不足|人员离职|原材料涨价|服务器容量)|迟延履行后.{0,40}免除', '不可抗力范围或免责时间需要复核', '经营成本、资源不足等不能仅凭列入合同就当然构成不可抗力，迟延后的免责也有适用限制。', '依事件是否不能预见、避免和克服及实际因果关系判断，明确通知、证明和减损义务。', '《民法典》第一百八十条、第五百九十条。'),
        ('ip_thirdparty', r'(?:开源|第三方).{0,70}(?:无权了解|独立承担)|开源组件.{0,40}(?:排他|转让)', '第三方及开源知识产权安排需核实', '提供方可能无权转让或排他许可第三方组件，许可证义务和侵权责任分配不清。', '提供组件清单、许可证与授权链，区分自有、定制、第三方权利及侵权协助。', None),
        ('account_personal', r'(?:账户名称|收款人|收款账户)[：:\s]+[\u4e00-\u9fff]{2,4}(?:[；。\n]|\s*[；。])', '收款账户可能不是合同相对方账户', '收款人疑似个人名称，须核对授权、资金去向、发票主体与清偿效力；不能据此断言欺诈。', '优先核验对公账户；确需第三方代收时取得有效书面授权并复核税务处理。', None),
        ('deposit_doublecharge', r'定金.{0,100}不计入.{0,20}付款比例', '定金与付款节点可能重复计收', '定金另行计收且不计入付款表，可能增加实际支付总额。', '列出全合同付款总表，明确每笔款项抵扣、返还及是否包含在合同总价内。', '《民法典》第五百八十六条。'),
        ('obsolete_law', r'依据.{0,80}(?:中华人民共和国合同法|民法总则)', '合同仍引用已被民法典替代的旧法名称', '需要结合合同订立、履行时间核对法律适用，不能仅替换法名而忽略时间效力。', '核实适用时间及过渡规则，更新现行合同的依据表述。', '《民法典》第一千二百六十条。'),
    ]
    for c in parsed['clauses']:
        text = c['title']+'\n'+c['content']
        for code, pattern, title, detail, suggestion, basis in patterns:
            if re.search(pattern, text, re.S):
                add(code, c, title, detail, suggestion, RiskLevel.HIGH if code in {
                    'license_late','liability_exemption','liability_scope_broad','data_crossborder','data_security','data_incident',
                    'data_retention_conflict','data_publicity','termination_exclusion','deposit_doublecharge'} else RiskLevel.MEDIUM, basis)
    return risks
