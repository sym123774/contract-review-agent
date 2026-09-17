"""Company-policy knowledge domain (seeded by users).

The bundled distribution ships with no company records; override this file
with your own policy records to enable the company_kb retrieval domain
(see registry.py). company_kb returns zero records by default and stays
disabled.
"""
from knowledge_base.models import KBType, normalize_record

COMPANY_RECORDS = []


def get_records():
    return [normalize_record(r, KBType.COMPANY.value, {"review_status": "pending", "status": "draft", "source": "company policy"}) for r in COMPANY_RECORDS]
