"""Legal knowledge domain adapter."""
import re
from knowledge_base.laws_data import get_all_chunks
from knowledge_base.models import KBType, normalize_record
from knowledge_base.privacy_data import get_records as get_privacy_records


def _applicability_scope(record):
    knowledge_id = record.get('knowledge_id') or record.get('id', '')
    if knowledge_id.startswith('mdr_'):
        return 'medical_device'
    if knowledge_id.startswith(('pipl_', 'dsl_', 'network_data_')):
        return 'data_privacy'
    if knowledge_id.startswith('sme_pay_'):
        return 'small_business_payment'
    if knowledge_id.startswith('interp_guarantee_'):
        return 'security'
    if knowledge_id.startswith('interp_sale_'):
        return 'sale'
    if knowledge_id.startswith('esign_'):
        return 'electronic_signature'
    if knowledge_id.startswith(('arbitration_', 'arb_law_', 'arb_interp_')):
        return 'arbitration'
    if knowledge_id.startswith('interp_tech_'):
        return 'technology_general'
    match = re.fullmatch(r'civil_(\d+)', knowledge_id)
    if match:
        article = int(match.group(1))
        # legal_kb 内仍需按法典分章做事实门控，避免只有词面相似就跨合同类型套用。
        if 809 <= article <= 842:
            return 'transport'
        if 843 <= article <= 850:
            return 'technology_general'
        if 851 <= article <= 861:
            return 'technology_development'
        if 862 <= article <= 877:
            return 'technology_transfer_license'
        if 878 <= article <= 887:
            return 'technology_consulting_service'
    return 'general'


def _with_scope(record):
    record['applicability_scope'] = _applicability_scope(record)
    if record['applicability_scope'] == 'medical_device':
        record['contract_types'] = ['medical_device_general', 'medical_device_commissioned_manufacturing']
    elif record['applicability_scope'] == 'transport':
        record['contract_types'] = ['transport']
    elif record['applicability_scope'] == 'technology_general':
        record['contract_types'] = ['technology_development', 'technology_transfer_license',
                                    'technology_consulting_service']
    elif record['applicability_scope'].startswith('technology_'):
        record['contract_types'] = [record['applicability_scope']]
    elif record['applicability_scope'] in {'security', 'sale', 'electronic_signature', 'arbitration'}:
        record['contract_types'] = ['general']
    return record


def _with_verified_metadata(record):
    """Fill stable identity/version fields only where the official source is known."""
    record = dict(record)
    knowledge_id = record.get('id', '')
    match = re.fullmatch(r'civil_(\d+)', knowledge_id)
    if match:
        article = match.group(1)
        record['source'] = '中华人民共和国民法典'
        record['article_or_clause'] = f'第{article}条'
        record['version'] = '2020年通过'
        record['effective_from'] = '2021-01-01'
        record['effective_date'] = '2021-01-01'
    else:
        match = re.fullmatch(r'interp_2023_13_(\d+)', knowledge_id)
        if match:
            article = match.group(1)
            record.setdefault('source', '最高人民法院关于适用《中华人民共和国民法典》合同编通则若干问题的解释')
            record.setdefault('article_or_clause', f'第{article}条')
            record.setdefault('version', '法释〔2023〕13号')
            record.setdefault('effective_from', '2023-12-05')
            record.setdefault('effective_date', '2023-12-05')
    # The remaining curated sources use stable ID prefixes tied to one official
    # promulgated version.  Recording those versions prevents an undated item from
    # being presented as verified current law.
    prefix_versions = (
        ('mdr_prod_', '2022-05-01', '国家市场监督管理总局令第53号'),
        ('mdr_ops_', '2022-05-01', '国家市场监督管理总局令第54号'),
        ('mdr_reg_', '2021-10-01', '国家市场监督管理总局令第47号'),
        ('mdr_', '2021-06-01', '国务院令第739号'),
        ('sme_pay_', '2025-06-01', '国务院令第802号（2025年修订）'),
        ('pql_', '2018-12-29', '2018年修正'),
        ('aucl_', '2025-10-15', '2025年修订'),
        ('dsl_', '2021-09-01', '2021年通过'),
        ('patent_', '2021-06-01', '2020年修正'),
        ('arb_law_', '2026-03-01', '2025年修订'),
        ('arb_interp_', '2006-09-08', '法释〔2006〕7号'),
    )
    for prefix, effective, version in prefix_versions:
        if knowledge_id.startswith(prefix):
            record.setdefault('effective_from', effective)
            record.setdefault('effective_date', effective)
            record.setdefault('version', version)
            break
    return record


def get_records():
    records = []
    for c in get_all_chunks():
        c = _with_verified_metadata(c)
        cid = c.get('id', '')
        # Replaced by the complete 2025 revised Arbitration Law dataset.
        if cid == 'arb_law_27':
            continue
        authority = ('judicial_interpretation' if cid.startswith(('interp_', 'arb_interp_'))
                     else 'regulation' if cid.startswith(('mdr_', 'sme_pay_', 'network_data_')) else 'statute')
        records.append(_with_scope(normalize_record(c, KBType.LEGAL.value, {
            'authority_level': authority, 'contract_types': [], 'review_status': 'published'})))
    return records + [_with_scope(r) for r in get_privacy_records()]
