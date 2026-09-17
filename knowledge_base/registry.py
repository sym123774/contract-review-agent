"""Knowledge-domain registry."""
from knowledge_base.company_data import get_records as get_company_records
from knowledge_base.industry_data import get_records as get_industry_records
from knowledge_base.legal_data import get_records as get_legal_records
from knowledge_base.models import KBType
from knowledge_base.template_data import get_records as get_template_records
DOMAIN_LOADERS = {KBType.LEGAL.value:get_legal_records, KBType.INDUSTRY.value:get_industry_records, KBType.COMPANY.value:get_company_records, KBType.TEMPLATE.value:get_template_records}
def get_domain_records(kb_type=None):
    if kb_type:
        loader = DOMAIN_LOADERS.get(kb_type)
        return loader() if loader else []
    records = []
    for loader in DOMAIN_LOADERS.values(): records.extend(loader())
    return records
def get_records_by_domain():
    return {domain: loader() for domain, loader in DOMAIN_LOADERS.items()}
